"""MsGraphFilesAdapter — Microsoft Graph connector, files-first (SharePoint
document libraries + optional OneDrives), the forcing function that hardened
the SourceAdapter contract. The mail adapter (Outlook) is the next template
fill-in behind the same auth + transport.

Auth: client credentials (application permissions + tenant-admin consent) —
this is a daemon, so there is no user, no refresh token, and nothing to write
back to the vault. The LONG-LIVED material lives in OpenBao at
tenants/<tenant>/sources/<credential_ref> as

    {"directory_id": <Entra tenant GUID>, "client_id": ..., "client_secret": ...}

and reaches the adapter only on a masked OutboundRequest. Short-lived access
tokens (~60-90 min) are minted on demand, cached IN MEMORY ONLY, never
logged, and re-minted ~5 min before expiry or once on a mid-pull 401.

Cursor: opaque JSON over per-drive Graph delta state —

    {"v": 1, "done": {<driveId>: <deltaLink>}, "cur": {"drive": ..., "link": ...}}

Drives are swept in sorted-id order. Every yielded item carries the state
whose cur.link re-fetches the item's OWN page, so a resume replays at most
one page (safe: landing is content-hash idempotent). A drive's fresh
deltaLink moves to `done` when its sweep completes; `final_cursor()` returns
the all-drives done map — the incremental high-water mark. Backfill IS the
initial delta enumeration (Graph's delta-from-scratch yields everything and
ends with a deltaLink), so the first incremental run rides changes
immediately. A new drive appearing in the tenant simply has no `done` entry
and gets enumerated from scratch; a vanished drive is logged and dropped,
never mass-tombstoned (§8.1g: absence is not a delete signal). Delta entries
with `@removed` — the explicit signal — become tombstone items. HTTP 410 on
a delta call raises CursorInvalid and the capture flow resyncs.

ACL: per-item permissions normalized to msgraph.driveItem.v1 SourceAcl.
Groups stay BY REFERENCE (the Entra group id — §2 #9: membership resolves at
serving time; a membership change never re-ingests). Sharing links become a
'link'/'anyone' grant carrying scope/type/expiry, plus one grant per
already-redeemed identity. The faithful permissions payload rides in raw.
"""
from __future__ import annotations

import json
import logging
import mimetypes
import random
import time
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, Optional

import requests

from knowledge_hub.interfaces import (
    AclGrant,
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

GRAPH = "https://graph.microsoft.com/v1.0"
LOGIN = "https://login.microsoftonline.com"


class GraphAuth:
    """Client-credentials token minting with an in-memory cache. The
    long-lived credential stays on the masked OutboundRequest; access tokens
    live only in this object and never reach logs or exceptions."""

    SKEW = 300  # re-mint this many seconds before expiry

    def __init__(self, request: OutboundRequest, session: Any,
                 tenant_id: str, source_ref: str):
        self._request = request
        self._session = session
        self._where = (tenant_id, source_ref)
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
                f"{LOGIN}/{p['directory_id']}/oauth2/v2.0/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": p["client_id"],
                    "client_secret": p["client_secret"],
                    "scope": "https://graph.microsoft.com/.default",
                },
                timeout=30)
        except requests.RequestException as e:
            raise SecretsError(*self._where,
                               f"token endpoint unreachable: {type(e).__name__}"
                               ) from e
        if resp.status_code != 200:
            # Entra error bodies can echo request parameters; status only.
            raise SecretsError(*self._where,
                               f"token mint refused (HTTP {resp.status_code})")
        body = resp.json()
        self._token = body["access_token"]
        self._expires_at = (time.monotonic()
                            + float(body.get("expires_in", 3600)) - self.SKEW)


class ThrottledTransport:
    """GETs that survive Graph throttling: 429/503/504 honor Retry-After
    (exponential backoff + jitter when absent), transient 5xx retry, and a
    mid-pull 401 re-mints the token exactly once (a second 401 means the
    credential is genuinely dead -> SecretsError, and the capture flow
    degrades the source). Counts what it endured for the run's stats.
    Interpreting terminal statuses (403/404/410) is the caller's job."""

    MAX_TRIES = 8
    BACKOFF_CAP = 120.0
    RETRY_AFTER_CAP = 300.0

    def __init__(self, auth: GraphAuth, tenant_id: str, source_ref: str,
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
                headers={"Authorization": f"Bearer {self._auth.token()}"},
                timeout=120)
            if resp.status_code == 401:
                if refreshed:
                    raise SecretsError(*self._where,
                                       "Graph rejected the credential twice"
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
            f"Graph still failing after {self.MAX_TRIES} tries"
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


class MsGraphFilesAdapter(SourceAdapter):
    """SharePoint/OneDrive documents via Graph delta queries.

    `sites` is either "all" (tenant-wide via /sites/getAllSites — needs
    Sites.Read.All application permission) or an allowlist of site refs
    (site ids or "host:/sites/name" paths — pair with Sites.Selected for a
    least-privilege posture). `include_onedrive` walks user OneDrives too.
    Oversized files are DEFERRED per §8.1e (skipped + counted, cursor still
    advances), not streamed — revisit when a real corpus demands it."""

    source_system = "msgraph-files"
    cursor_ordering = "opaque"

    #: Graph/SharePoint role vocabulary -> normalized read|write|owner.
    ROLE_MAP = {
        "read": "read", "view": "read",
        "write": "write", "edit": "write", "contribute": "write",
        "owner": "owner", "fullcontrol": "owner", "sp.full control": "owner",
        "manage": "owner",
    }
    IDENTITY_KINDS = (("user", "user"), ("group", "group"),
                      ("siteGroup", "site_group"),
                      ("application", "application"), ("device", "device"))

    def __init__(self, source_ref: str, sites: Any = "all",
                 include_onedrive: bool = False,
                 max_content_bytes: int = 256 * 1024 * 1024,
                 credential_ref: Optional[str] = None,
                 session: Any = None):
        super().__init__(source_ref, credential_ref=credential_ref)
        self.sites = sites
        self.include_onedrive = include_onedrive
        self.max_content_bytes = max_content_bytes
        self._session = session  # injectable for tests; None = real HTTP
        self._transport: Optional[ThrottledTransport] = None
        self._final: Optional[str] = None
        # Per-run diagnostics (mirrors the filesystem adapter's discipline).
        self.skipped_unreadable: list[str] = []
        self.skipped_oversized: list[str] = []

    # -------------------------------------------------------------- prepare --
    def _prepare(self, tenant_id: str,
                 secrets: Optional[SecretsProvider]) -> None:
        if secrets is None:
            raise SecretNotFound(tenant_id, self.source_ref,
                                 "Graph adapter needs a SecretsProvider")
        request = OutboundRequest()
        secrets.inject_credential(tenant_id, self.credential_ref, request)
        missing = ({"directory_id", "client_id", "client_secret"}
                   - set(request.params))
        if missing:
            raise SecretNotFound(
                tenant_id, self.source_ref,
                f"credential at {self.credential_ref!r} missing fields"
                f" {sorted(missing)}")
        session = self._session or requests.Session()
        auth = GraphAuth(request, session, tenant_id, self.source_ref)
        self._transport = ThrottledTransport(auth, tenant_id, self.source_ref,
                                             session=session)

    # ------------------------------------------------------------ iterators --
    def backfill(self, tenant_id: str,
                 resume_after: Optional[str] = None) -> Iterator[SourceItem]:
        self.require_prepared(tenant_id)
        return self._sweep(tenant_id, resume_after)

    def incremental(self, tenant_id: str,
                    cursor: Optional[str]) -> Iterator[SourceItem]:
        self.require_prepared(tenant_id)
        return self._sweep(tenant_id, cursor)

    def final_cursor(self) -> Optional[str]:
        return self._final

    def stats(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "skipped_unreadable": list(self.skipped_unreadable),
            "skipped_oversized": list(self.skipped_oversized),
        }
        if self._transport is not None:
            out["throttled"] = self._transport.throttled
            out["retried"] = self._transport.retried
        return out

    # ---------------------------------------------------------------- sweep --
    def _sweep(self, tenant_id: str,
               cursor_token: Optional[str]) -> Iterator[SourceItem]:
        self.skipped_unreadable = []
        self.skipped_oversized = []
        self._final = None
        state = self._parse_cursor(tenant_id, cursor_token)

        drives = self._list_drives()
        known = {d["id"] for d in drives}
        vanished = sorted(set(state["done"]) - known)
        if vanished:
            # §8.1g: absence is NOT a delete signal — a vanished drive (site
            # deleted? access revoked?) is logged and its state dropped,
            # never mass-tombstoned.
            logger.warning(
                "msgraph %s: %d drive(s) no longer visible (%s...) — state"
                " dropped, items NOT tombstoned",
                self.source_ref, len(vanished), vanished[:3])
            for drive_id in vanished:
                state["done"].pop(drive_id, None)

        cur = state.get("cur")
        for drive in drives:
            if cur and cur.get("drive") == drive["id"]:
                link = cur["link"]  # resume the in-flight page
            elif drive["id"] in state["done"]:
                link = state["done"][drive["id"]]  # changes since last sweep
            else:
                link = f"{GRAPH}/drives/{drive['id']}/root/delta"  # scratch
            yield from self._sweep_drive(tenant_id, state, drive, link)

        state["cur"] = None
        self._final = self._serialize(state)

    def _sweep_drive(self, tenant_id: str, state: dict, drive: dict,
                     link: str) -> Iterator[SourceItem]:
        drive_id = drive["id"]
        while True:
            state["cur"] = {"drive": drive_id, "link": link}
            page_cursor = self._serialize(state)
            resp = self._transport.get(link)
            if resp.status_code == 410:
                raise CursorInvalid(tenant_id, self.source_ref,
                                    f"delta token expired for drive"
                                    f" {drive_id} (HTTP 410)")
            if resp.status_code == 404:
                logger.warning("msgraph %s: drive %s vanished mid-sweep —"
                               " state dropped, items NOT tombstoned",
                               self.source_ref, drive_id)
                state["done"].pop(drive_id, None)
                state["cur"] = None
                return
            self._expect_ok(resp)
            page = resp.json()
            for entry in page.get("value", []):
                item = self._to_item(drive, entry, page_cursor)
                if item is not None:
                    yield item
            if "@odata.nextLink" in page:
                link = page["@odata.nextLink"]
                continue
            delta_link = page.get("@odata.deltaLink")
            if not delta_link:
                raise RuntimeError(
                    f"Graph delta page for drive {drive_id} carried neither"
                    " nextLink nor deltaLink")
            state["done"][drive_id] = delta_link
            state["cur"] = None
            return

    # ---------------------------------------------------------------- items --
    def _to_item(self, drive: dict, entry: dict,
                 cursor: str) -> Optional[SourceItem]:
        native_id = f"drives/{drive['id']}/items/{entry['id']}"
        if "@removed" in entry:
            return SourceItem(
                native_id=native_id,
                change="tombstone",
                mtime=datetime.now(tz=timezone.utc),  # observation time
                native_metadata={
                    "source_ref": self.source_ref,
                    "drive_id": drive["id"],
                    "removed_reason": (entry.get("@removed") or {}).get("reason"),
                },
                cursor=cursor)
        if "file" not in entry:
            return None  # folders/packages are containers, not documents

        size = int(entry.get("size") or 0)
        if size > self.max_content_bytes:
            self.skipped_oversized.append(native_id)
            logger.warning(
                "msgraph %s: %r is %d bytes (cap %d) — deferred (§8.1e)",
                self.source_ref, native_id, size, self.max_content_bytes)
            return None
        content = self._download(drive["id"], entry["id"], native_id)
        if content is None:
            return None
        acl = self._capture_acl(drive, entry)
        mtime = (self._parse_ts(entry.get("lastModifiedDateTime"))
                 or datetime.now(tz=timezone.utc))
        return SourceItem(
            native_id=native_id,
            content=content,
            mime_type=((entry.get("file") or {}).get("mimeType")
                       or mimetypes.guess_type(entry.get("name") or "")[0]),
            size=len(content),
            mtime=mtime,
            source_acl=acl,
            native_metadata=self._native_metadata(drive, entry),
            cursor=cursor)

    def _download(self, drive_id: str, item_id: str,
                  native_id: str) -> Optional[bytes]:
        resp = self._transport.get(
            f"{GRAPH}/drives/{drive_id}/items/{item_id}/content")
        if resp.status_code in (403, 404):
            # Vanished or forbidden between listing and fetch — the source's
            # ACL said no; skip + count, never wedge the pull (the filesystem
            # adapter's skipped_unreadable discipline).
            self.skipped_unreadable.append(native_id)
            logger.warning("msgraph %s: cannot fetch %r (HTTP %d), skipping",
                           self.source_ref, native_id, resp.status_code)
            return None
        self._expect_ok(resp)
        return resp.content

    def _native_metadata(self, drive: dict, entry: dict) -> dict[str, Any]:
        parent = entry.get("parentReference") or {}
        hashes = (entry.get("file") or {}).get("hashes") or {}
        meta = {
            "source_ref": self.source_ref,
            "name": entry.get("name"),
            "web_url": entry.get("webUrl"),
            "etag": entry.get("eTag"),
            "ctag": entry.get("cTag"),
            "size": entry.get("size"),
            "created": entry.get("createdDateTime"),
            "modified": entry.get("lastModifiedDateTime"),
            "created_by": (entry.get("createdBy") or {}).get("user"),
            "modified_by": (entry.get("lastModifiedBy") or {}).get("user"),
            "parent_path": parent.get("path"),
            "drive_id": drive["id"],
            "drive_name": drive.get("name"),
            "drive_type": drive.get("drive_type"),
            "site_id": drive.get("site_id"),
            "site_url": drive.get("site_url"),
            "owner_upn": drive.get("owner_upn"),
            "quickxor_hash": hashes.get("quickXorHash"),
            "sha256_hash": hashes.get("sha256Hash"),
        }
        return {k: v for k, v in meta.items() if v is not None}

    # ------------------------------------------------------------------ ACL --
    def _capture_acl(self, drive: dict, entry: dict) -> SourceAcl:
        resp = self._transport.get(
            f"{GRAPH}/drives/{drive['id']}/items/{entry['id']}/permissions")
        if resp.status_code in (403, 404):
            # The item landed but its ACL didn't — record that honestly so
            # downstream can default to most-restrictive, never guess.
            perms: list[dict] = []
            raw: dict[str, Any] = {
                "error": f"permissions fetch failed (HTTP {resp.status_code})"}
        else:
            self._expect_ok(resp)
            perms = resp.json().get("value", [])
            raw = {"permissions": perms}
        grants: list[AclGrant] = []
        for perm in perms:
            grants.extend(self._grants_from_permission(perm))
        created_by = (entry.get("createdBy") or {}).get("user") or {}
        return SourceAcl(model="msgraph.driveItem.v1",
                         owner=created_by.get("id") or created_by.get("email"),
                         grants=grants, raw=raw)

    def _grants_from_permission(self, perm: dict) -> list[AclGrant]:
        roles = [self.ROLE_MAP.get(r.lower().strip(), r.lower().strip())
                 for r in perm.get("roles") or []]
        inherited_from = (perm.get("inheritedFrom") or {}).get("id") \
            if "inheritedFrom" in perm else None
        link = perm.get("link")
        out: list[AclGrant] = []
        if link:
            link_type = link.get("type")
            if not roles and link_type:
                roles = [self.ROLE_MAP.get(link_type, link_type)]
            scope = link.get("scope")
            detail = {k: v for k, v in {
                "permission_id": perm.get("id"),
                "scope": scope,
                "link_type": link_type,
                "expires": perm.get("expirationDateTime"),
                "prevents_download": link.get("preventsDownload"),
            }.items() if v is not None}
            out.append(AclGrant(
                principal_type="anyone" if scope == "anonymous" else "link",
                principal_id=perm.get("id"), roles=roles, via="link",
                detail=detail))
            for identity_set in perm.get("grantedToIdentitiesV2") or []:
                grant = self._grant_from_identity_set(identity_set, roles,
                                                      via="link")
                if grant is not None:
                    out.append(grant)
            return out
        identity_set = perm.get("grantedToV2") or perm.get("grantedTo")
        if identity_set:
            detail = ({"inherited_from": inherited_from}
                      if inherited_from is not None else {})
            grant = self._grant_from_identity_set(
                identity_set, roles,
                via="inherited" if "inheritedFrom" in perm else "direct",
                detail=detail)
            if grant is not None:
                out.append(grant)
        return out

    @classmethod
    def _grant_from_identity_set(cls, identity_set: dict, roles: list[str],
                                 via: str,
                                 detail: Optional[dict] = None
                                 ) -> Optional[AclGrant]:
        for graph_key, ptype in cls.IDENTITY_KINDS:
            ident = identity_set.get(graph_key)
            if ident:
                # Groups by reference (§2 #9): the id is the grant; membership
                # is resolved at serving time, never flattened here.
                return AclGrant(
                    principal_type=ptype,
                    principal_id=(ident.get("id") or ident.get("loginName")
                                  or ident.get("email")),
                    display=ident.get("displayName"),
                    roles=roles, via=via, detail=detail or {})
        return None

    # ------------------------------------------------------------ discovery --
    def _list_drives(self) -> list[dict]:
        drives: dict[str, dict] = {}
        if self.sites == "all":
            sites = self._paged(f"{GRAPH}/sites/getAllSites"
                                "?$select=id,webUrl,name")
        else:
            sites = (self._get_json(f"{GRAPH}/sites/{ref}")
                     for ref in self.sites)
        for site in sites:
            for d in self._paged(f"{GRAPH}/sites/{site['id']}/drives"
                                 "?$select=id,name,driveType,webUrl"):
                drives[d["id"]] = {
                    "id": d["id"], "name": d.get("name"),
                    "drive_type": d.get("driveType"),
                    "web_url": d.get("webUrl"),
                    "site_id": site.get("id"), "site_url": site.get("webUrl"),
                }
        if self.include_onedrive:
            for user in self._paged(f"{GRAPH}/users"
                                    "?$select=id,userPrincipalName"):
                resp = self._transport.get(f"{GRAPH}/users/{user['id']}/drive")
                if resp.status_code == 404:
                    continue  # no OneDrive provisioned
                self._expect_ok(resp)
                d = resp.json()
                drives[d["id"]] = {
                    "id": d["id"], "name": d.get("name"),
                    "drive_type": d.get("driveType"),
                    "web_url": d.get("webUrl"),
                    "owner_upn": user.get("userPrincipalName"),
                }
        return sorted(drives.values(), key=lambda d: d["id"])

    def _paged(self, url: Optional[str]) -> Iterator[dict]:
        while url:
            page = self._get_json(url)
            yield from page.get("value", [])
            url = page.get("@odata.nextLink")

    def _get_json(self, url: str) -> dict:
        resp = self._transport.get(url)
        self._expect_ok(resp)
        return resp.json()

    # -------------------------------------------------------------- plumbing --
    @staticmethod
    def _expect_ok(resp: requests.Response) -> None:
        if resp.status_code != 200:
            raise RuntimeError(f"unexpected Graph response"
                               f" (HTTP {resp.status_code})")

    def _parse_cursor(self, tenant_id: str,
                      token: Optional[str]) -> dict[str, Any]:
        if not token:
            return {"v": 1, "done": {}, "cur": None}
        try:
            state = json.loads(token)
            if not isinstance(state, dict) or state.get("v") != 1:
                raise ValueError(f"unknown cursor version"
                                 f" {state.get('v') if isinstance(state, dict) else '?'!r}")
            return {"v": 1, "done": dict(state.get("done") or {}),
                    "cur": state.get("cur")}
        except (ValueError, TypeError) as e:
            # Corrupt/foreign state is indistinguishable from expired —
            # resync rather than guess (self-healing, idempotent).
            raise CursorInvalid(tenant_id, self.source_ref,
                                f"unparseable cursor ({type(e).__name__})"
                                ) from e

    @staticmethod
    def _serialize(state: dict[str, Any]) -> str:
        return json.dumps(state, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _parse_ts(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
