"""QboAdapter — QuickBooks Online connector (accounting records as system-of-
record rows), the second connector family filling in the hardened
SourceAdapter template. No LLM anywhere in this path: every record routes to
the structured track (native_metadata declares data_track='sor').

Auth: OAuth 2.0 authorization-code — Intuit offers NO daemon flow, so the
long-lived material is a ROTATING refresh token obtained once by a human
consent step (runbook in CONNECTOR_NOTES.md). OpenBao holds, at
tenants/<tenant>/sources/<credential_ref>:

    {"client_id", "client_secret", "refresh_token", "realm_id",
     "environment": "production"|"sandbox"}   (environment optional)

Access tokens (~60 min) are minted from the refresh token, cached IN MEMORY
ONLY, and re-minted on expiry skew or once on a mid-pull 401. Intuit re-issues
the refresh token itself (~every 24h of use): the fresh token is PERSISTED
FIRST via the CredentialRotator seam, then used — a provider that cannot
rotate is refused at prepare(), because running without write-back would lock
the connector out within a day (self-destruction is not a degraded mode).

Cursor: opaque JSON —

    {"v": 1, "since": <ISO ts | null>, "entities": [...],
     "cur": {"entity": ..., "pos": ...} | null}

Backfill pages each entity type through the query endpoint (ORDERBY Id,
STARTPOSITION pagination); every yielded item's cursor re-fetches its OWN
page, so a resume replays at most one page (at-least-once + content-hash
idempotency make replay free). `since` is captured from the FIRST response's
server-side `time` (minus an overlap pad), so the first incremental run rides
changes from before the backfill even started. Incremental sweeps ride the
Change Data Capture endpoint (one changedSince across all entities), which
reports deletions EXPLICITLY — the authoritative §8.1g tombstone signal.
List entities (Customer/Vendor/Item...) are never hard-deleted in QBO, only
flagged Active=false; that arrives as a normal changed record, which is
correct — it is a state change, not a delete.

Deterministic resync triggers (CursorInvalid -> the capture flow re-backfills,
safe end to end): a `since` older than CDC's 30-day lookback, a CDC response
at the per-entity cap (possible truncation — resync beats silent loss), a
changed entity set in config, or an unparseable stored state.

ACL: QBO has no per-record permissions — access is company-wide — so every
item carries one company-scope grant (qbo.company.v1, principal = the realm).
"""
from __future__ import annotations

import json
import logging
import random
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator, Optional

import requests

from knowledge_hub.interfaces import (
    AclGrant,
    CredentialRotator,
    CursorInvalid,
    OutboundRequest,
    SecretNotFound,
    SecretsError,
    SecretsProvider,
    SourceAcl,
    SourceAdapter,
    SourceItem,
)

logger = logging.getLogger(__name__)

OAUTH_TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
BASE_URLS = {
    "production": "https://quickbooks.api.intuit.com",
    "sandbox": "https://sandbox-quickbooks.api.intuit.com",
}
MINOR_VERSION = "75"          # Intuit-mandated floor since 2025-08
CDC_LOOKBACK_DAYS = 30        # Intuit's hard changedSince limit
CDC_SAFETY_DAYS = 1           # resync this far BEFORE the hard edge
OVERLAP_SECONDS = 300         # since-pad: replays are free, gaps are not

#: CDC-supported record types worth landing by default; config narrows or
#: widens per engagement. Sorted — sweep order is part of cursor determinism.
DEFAULT_ENTITIES = (
    "Account", "Bill", "BillPayment", "CreditMemo", "Customer", "Deposit",
    "Employee", "Estimate", "Invoice", "Item", "JournalEntry", "Payment",
    "Purchase", "PurchaseOrder", "Vendor",
)


class QboAuth:
    """Refresh-token minting with an in-memory access-token cache and
    persist-before-use rotation write-back. The rotating refresh token lives
    on the masked OutboundRequest and in the vault; access tokens live only
    in this object and never reach logs or exceptions."""

    SKEW = 300  # re-mint this many seconds before expiry

    def __init__(self, request: OutboundRequest, session: Any,
                 rotator: CredentialRotator, tenant_id: str,
                 source_ref: str, credential_ref: str):
        self._request = request
        self._session = session
        self._rotator = rotator
        self._where = (tenant_id, source_ref)
        self._credential_ref = credential_ref
        self._token: Optional[str] = None
        self._expires_at = 0.0

    def token(self, force_refresh: bool = False) -> str:
        if (force_refresh or self._token is None
                or time.monotonic() >= self._expires_at):
            self._mint()
        return self._token

    def _mint(self) -> None:
        p = self._request.params
        try:
            resp = self._session.post(
                OAUTH_TOKEN_URL,
                auth=(p["client_id"], p["client_secret"]),
                data={"grant_type": "refresh_token",
                      "refresh_token": p["refresh_token"]},
                headers={"Accept": "application/json"},
                timeout=30)
        except requests.RequestException as e:
            raise SecretsError(*self._where,
                               f"token endpoint unreachable: {type(e).__name__}"
                               ) from e
        if resp.status_code != 200:
            # Intuit error bodies can echo request parameters; surface only
            # the status and the (non-secret) OAuth error code.
            code = ""
            try:
                code = (resp.json() or {}).get("error") or ""
            except ValueError:
                pass
            hint = (" — refresh token dead; a human must re-consent"
                    " (runbook: CONNECTOR_NOTES.md)"
                    if code == "invalid_grant" else "")
            raise SecretsError(*self._where,
                               f"token refresh refused (HTTP"
                               f" {resp.status_code}"
                               + (f", {code}" if code else "") + f"){hint}")
        body = resp.json()
        new_refresh = body.get("refresh_token")
        if new_refresh and new_refresh != p["refresh_token"]:
            # Intuit rotated the refresh token. PERSIST FIRST, use after —
            # a crash between here and first use must never strand the only
            # valid token in memory. A vault failure raises SecretsError and
            # the capture flow degrades this source (never proceed
            # unpersisted).
            self._rotator.rotate_credential(
                self._where[0], self._credential_ref,
                {"refresh_token": new_refresh})
            self._request.attach_secret("refresh_token", new_refresh)
        self._token = body["access_token"]
        self._expires_at = (time.monotonic()
                            + float(body.get("expires_in", 3600)) - self.SKEW)


class QboTransport:
    """GETs that survive Intuit throttling (the msgraph transport's
    discipline, QBO-flavored): 429/503/504 honor Retry-After (exponential
    backoff + jitter when absent), transient 5xx retry, and a mid-pull 401
    re-mints the access token exactly once (a second 401 means the credential
    is genuinely dead -> SecretsError, and the capture flow degrades the
    source). Counts what it endured for the run's stats. Interpreting
    terminal statuses (400/403) is the caller's job."""

    MAX_TRIES = 8
    BACKOFF_CAP = 120.0
    RETRY_AFTER_CAP = 300.0

    def __init__(self, auth: QboAuth, tenant_id: str, source_ref: str,
                 session: Any = None,
                 sleep: Callable[[float], None] = time.sleep):
        self._auth = auth
        self._session = session or requests.Session()
        self._where = (tenant_id, source_ref)
        self._sleep = sleep
        self.throttled = 0
        self.retried = 0

    def get(self, url: str) -> requests.Response:
        refreshed = False
        resp = None
        for attempt in range(self.MAX_TRIES):
            resp = self._session.get(
                url,
                headers={"Authorization": f"Bearer {self._auth.token()}",
                         "Accept": "application/json"},
                timeout=120)
            if resp.status_code == 401:
                if refreshed:
                    raise SecretsError(*self._where,
                                       "QBO rejected the credential twice"
                                       " (401 after token refresh)")
                refreshed = True
                self._auth.token(force_refresh=True)
                continue
            if resp.status_code in (429, 503, 504):
                self.throttled += 1
                self._sleep(self._retry_after(resp, attempt))
                continue
            if 500 <= resp.status_code < 600:
                self.retried += 1
                self._sleep(min(2.0 ** attempt + random.random(),
                                self.BACKOFF_CAP))
                continue
            return resp
        raise RuntimeError(
            f"QBO still failing after {self.MAX_TRIES} tries"
            f" (HTTP {resp.status_code}) for source {self._where[1]!r}")

    @classmethod
    def _retry_after(cls, resp: requests.Response, attempt: int) -> float:
        ra = resp.headers.get("Retry-After")
        if ra:
            try:
                return min(float(ra), cls.RETRY_AFTER_CAP)
            except ValueError:
                pass  # HTTP-date form: fall through to backoff
        return min(2.0 ** attempt + random.random(), cls.BACKOFF_CAP)


class QboAdapter(SourceAdapter):
    """QuickBooks Online records via the query (backfill) and Change Data
    Capture (incremental) endpoints. `entities` narrows/widens the record
    types swept (default: DEFAULT_ENTITIES); changing the set later
    deliberately forces a resync so new types get their backfill. `page_size`
    is Intuit's 1000-row query cap (injectable for tests)."""

    source_system = "qbo"
    cursor_ordering = "opaque"

    def __init__(self, source_ref: str,
                 entities: Optional[list[str]] = None,
                 page_size: int = 1000,
                 credential_ref: Optional[str] = None,
                 session: Any = None):
        super().__init__(source_ref, credential_ref=credential_ref)
        self.entities = sorted(entities or DEFAULT_ENTITIES)
        for name in self.entities:
            if not name.isalpha():
                raise ValueError(f"invalid QBO entity name {name!r}")
        self.page_size = page_size
        self._session = session  # injectable for tests; None = real HTTP
        self._transport: Optional[QboTransport] = None
        self._realm: Optional[str] = None
        self._base: Optional[str] = None
        self._final: Optional[str] = None

    # -------------------------------------------------------------- prepare --
    def _prepare(self, tenant_id: str,
                 secrets: Optional[SecretsProvider]) -> None:
        if secrets is None:
            raise SecretNotFound(tenant_id, self.source_ref,
                                 "QBO adapter needs a SecretsProvider")
        if not isinstance(secrets, CredentialRotator):
            # Refuse loudly NOW rather than lose a rotated refresh token
            # within ~24h of running: no write-back path = guaranteed
            # future lockout, and that is not a degraded mode we accept.
            raise SecretsError(
                tenant_id, self.source_ref,
                "QBO refresh tokens rotate; the secrets provider must"
                " implement CredentialRotator (OpenBaoSecretsProvider does)")
        request = OutboundRequest()
        secrets.inject_credential(tenant_id, self.credential_ref, request)
        missing = ({"client_id", "client_secret", "refresh_token", "realm_id"}
                   - set(request.params))
        if missing:
            raise SecretNotFound(
                tenant_id, self.source_ref,
                f"credential at {self.credential_ref!r} missing fields"
                f" {sorted(missing)}")
        environment = request.params.get("environment", "production")
        if environment not in BASE_URLS:
            raise SecretNotFound(
                tenant_id, self.source_ref,
                f"credential field 'environment' must be one of"
                f" {sorted(BASE_URLS)}")
        self._realm = str(request.params["realm_id"])
        self._base = BASE_URLS[environment]
        session = self._session or requests.Session()
        auth = QboAuth(request, session, secrets, tenant_id,
                       self.source_ref, self.credential_ref)
        self._transport = QboTransport(auth, tenant_id, self.source_ref,
                                       session=session)

    # ------------------------------------------------------------ iterators --
    def backfill(self, tenant_id: str,
                 resume_after: Optional[str] = None) -> Iterator[SourceItem]:
        self.require_prepared(tenant_id)
        return self._query_sweep(tenant_id, resume_after)

    def incremental(self, tenant_id: str,
                    cursor: Optional[str]) -> Iterator[SourceItem]:
        self.require_prepared(tenant_id)
        if cursor is None:
            # Contract: cursor=None means everything. The full query sweep
            # yields it all AND establishes a fresh `since`.
            return self._query_sweep(tenant_id, None)
        return self._cdc_sweep(tenant_id, cursor)

    def final_cursor(self) -> Optional[str]:
        return self._final

    def stats(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self._transport is not None:
            out["throttled"] = self._transport.throttled
            out["retried"] = self._transport.retried
        return out

    # ------------------------------------------------------- backfill sweep --
    def _query_sweep(self, tenant_id: str,
                     cursor_token: Optional[str]) -> Iterator[SourceItem]:
        self._final = None
        state = self._parse_cursor(tenant_id, cursor_token)
        cur = state.get("cur")
        resume_entity = cur["entity"] if cur else None
        resume_pos = int(cur["pos"]) if cur else 1

        for entity in state["entities"]:
            if resume_entity is not None:
                if entity != resume_entity:
                    continue  # already fully swept before the interruption
                pos, resume_entity = resume_pos, None
            else:
                pos = 1
            while True:
                state["cur"] = {"entity": entity, "pos": pos}
                page_cursor = self._serialize(state)
                page = self._get_json(self._query_url(entity, pos))
                if state["since"] is None:
                    # Sweep start, server-authoritative: the first response's
                    # own timestamp (minus overlap pad) is where the first
                    # incremental run will pick up — nothing changed during
                    # or before this sweep can fall in a gap.
                    state["since"] = self._since_from_response(page)
                rows = (page.get("QueryResponse") or {}).get(entity) or []
                for obj in rows:
                    yield self._to_item(entity, obj, page_cursor)
                if len(rows) < self.page_size:
                    break
                pos += self.page_size

        state["cur"] = None
        if state["since"] is None:  # zero entities configured — still honest
            state["since"] = self._utc_iso(
                datetime.now(tz=timezone.utc)
                - timedelta(seconds=OVERLAP_SECONDS))
        self._final = self._serialize(state)

    def _query_url(self, entity: str, pos: int) -> str:
        q = (f"SELECT * FROM {entity} ORDERBY Id"
             f" STARTPOSITION {pos} MAXRESULTS {self.page_size}")
        return (f"{self._base}/v3/company/{self._realm}/query?"
                + urllib.parse.urlencode({"query": q,
                                          "minorversion": MINOR_VERSION}))

    # ---------------------------------------------------------- CDC sweep ----
    def _cdc_sweep(self, tenant_id: str,
                   cursor_token: str) -> Iterator[SourceItem]:
        self._final = None
        state = self._parse_cursor(tenant_id, cursor_token)
        if state["since"] is None:
            raise CursorInvalid(tenant_id, self.source_ref,
                                "cursor carries no changedSince mark")
        if state["entities"] != self.entities:
            # A widened set has never been backfilled; a narrowed one leaves
            # the cursor claiming coverage it no longer has. Either way the
            # deterministic move is a resync, not a guess.
            raise CursorInvalid(tenant_id, self.source_ref,
                                "configured entity set changed — resync")
        since = self._parse_ts(state["since"])
        if since is None:
            raise CursorInvalid(tenant_id, self.source_ref,
                                "unparseable changedSince in cursor")
        horizon = (datetime.now(tz=timezone.utc)
                   - timedelta(days=CDC_LOOKBACK_DAYS - CDC_SAFETY_DAYS))
        if since < horizon:
            raise CursorInvalid(
                tenant_id, self.source_ref,
                f"changedSince {state['since']} is beyond CDC's"
                f" {CDC_LOOKBACK_DAYS}-day lookback")

        url = (f"{self._base}/v3/company/{self._realm}/cdc?"
               + urllib.parse.urlencode({
                   "entities": ",".join(self.entities),
                   "changedSince": state["since"],
                   "minorversion": MINOR_VERSION}))
        resp = self._transport.get(url)
        if resp.status_code == 400 and b"changedSince" in resp.content:
            raise CursorInvalid(tenant_id, self.source_ref,
                                "CDC rejected changedSince (HTTP 400)")
        self._expect_ok(resp)
        body = resp.json()

        # Item cursors keep the OLD since: a mid-sweep resume replays the
        # whole (cheap) CDC sweep — at-least-once, idempotent, no gaps.
        page_cursor = self._serialize(state)
        for block in body.get("CDCResponse") or []:
            for qr in block.get("QueryResponse") or []:
                for entity, rows in qr.items():
                    if entity in ("startPosition", "maxResults",
                                  "totalCount"):
                        continue
                    if not isinstance(rows, list):
                        continue
                    if len(rows) >= self.page_size:
                        # At the per-entity cap: Intuit may have truncated.
                        # Resync costs a re-walk; silence costs lost records.
                        raise CursorInvalid(
                            tenant_id, self.source_ref,
                            f"CDC returned {len(rows)} {entity} rows (cap"
                            f" {self.page_size}) — possible truncation,"
                            " resyncing")
                    for obj in rows:
                        yield self._to_item(entity, obj, page_cursor)

        state["since"] = self._since_from_response(body)
        state["cur"] = None
        self._final = self._serialize(state)

    # ---------------------------------------------------------------- items --
    def _to_item(self, entity: str, obj: dict, cursor: str) -> SourceItem:
        native_id = f"{entity}/{obj['Id']}"
        meta = obj.get("MetaData") or {}
        mtime = (self._parse_ts(meta.get("LastUpdatedTime"))
                 or datetime.now(tz=timezone.utc))
        if obj.get("status") == "Deleted":
            # CDC's explicit delete signal (§8.1g) — transaction entities
            # only; list entities arrive as Active=false upserts instead.
            return SourceItem(
                native_id=native_id,
                change="tombstone",
                mtime=mtime,
                native_metadata={
                    "source_ref": self.source_ref,
                    "entity": entity,
                    "qbo_id": obj["Id"],
                    "realm_id": self._realm,
                },
                cursor=cursor)
        # Canonical bytes: identical records hash identically across pulls,
        # so re-landing stays a content-hash no-op.
        content = json.dumps(obj, sort_keys=True,
                             separators=(",", ":")).encode()
        return SourceItem(
            native_id=native_id,
            content=content,
            mime_type="application/json",
            size=len(content),
            mtime=mtime,
            source_acl=self._company_acl(),
            native_metadata=self._native_metadata(entity, obj, meta),
            cursor=cursor)

    def _company_acl(self) -> SourceAcl:
        # QBO has no per-record ACL; access is company-wide. One grant at
        # company scope, principal = the realm (the company's stable id).
        return SourceAcl(
            model="qbo.company.v1",
            owner=self._realm,
            grants=[AclGrant(principal_type="domain",
                             principal_id=self._realm,
                             display="QuickBooks company (realm)",
                             roles=["read"], via="direct",
                             detail={"scope": "company"})],
            raw={"realm_id": self._realm})

    def _native_metadata(self, entity: str, obj: dict,
                         meta: dict) -> dict[str, Any]:
        out = {
            "source_ref": self.source_ref,
            "entity": entity,
            "qbo_id": obj.get("Id"),
            "realm_id": self._realm,
            "sync_token": obj.get("SyncToken"),
            "created": meta.get("CreateTime"),
            "modified": meta.get("LastUpdatedTime"),
            "display": (obj.get("DisplayName") or obj.get("Name")
                        or obj.get("DocNumber")),
            "txn_date": obj.get("TxnDate"),
            "active": obj.get("Active"),
            # Declarations (§8.1a: claims, arbitrated downstream): QBO rows
            # are system-of-record data — the structured track, no LLM.
            "data_track": "sor",
            "doc_type": f"qbo.{entity.lower()}",
        }
        return {k: v for k, v in out.items() if v is not None}

    # -------------------------------------------------------------- plumbing --
    def _get_json(self, url: str) -> dict:
        resp = self._transport.get(url)
        self._expect_ok(resp)
        return resp.json()

    @staticmethod
    def _expect_ok(resp: requests.Response) -> None:
        if resp.status_code != 200:
            raise RuntimeError(f"unexpected QBO response"
                               f" (HTTP {resp.status_code})")

    def _parse_cursor(self, tenant_id: str,
                      token: Optional[str]) -> dict[str, Any]:
        if not token:
            return {"v": 1, "since": None,
                    "entities": list(self.entities), "cur": None}
        try:
            state = json.loads(token)
            if not isinstance(state, dict) or state.get("v") != 1:
                raise ValueError("unknown cursor version")
            return {"v": 1, "since": state.get("since"),
                    "entities": list(state.get("entities") or []),
                    "cur": state.get("cur")}
        except (ValueError, TypeError) as e:
            # Corrupt/foreign state is indistinguishable from expired —
            # resync rather than guess (self-healing, idempotent).
            raise CursorInvalid(tenant_id, self.source_ref,
                                f"unparseable cursor ({type(e).__name__})"
                                ) from e

    def _since_from_response(self, body: dict) -> str:
        server_time = self._parse_ts(body.get("time"))
        if server_time is None:
            server_time = datetime.now(tz=timezone.utc)
        return self._utc_iso(server_time
                             - timedelta(seconds=OVERLAP_SECONDS))

    @staticmethod
    def _serialize(state: dict[str, Any]) -> str:
        return json.dumps(state, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _utc_iso(dt: datetime) -> str:
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds")

    @staticmethod
    def _parse_ts(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
