"""Permission choke point + server-side identity (Build Prompt S2).

Implements the S1 `ChokePoint` seam: the single mandatory permission gate on
the EXTERNAL serve/read path. Every query that will hit Postgres on that path
— base op, composite step, retrieval, traversal — transits `enforce()` and
executes only through `read()`, unconditionally.

THE BOUNDARY (internal vs external — keep this exact):

  * INTERNAL pipeline components — FactStore writes, resolver reads,
    extraction, capture — are trusted infrastructure. They process ALL
    tenants by design and do NOT pass through this choke point; their door
    is PostgresFactStore. Nothing in this module is on their path.
  * EXTERNAL serve/read path — anything answering a caller (S3 ops, S4
    retrieval, S5 API) — has exactly one door to Postgres: this module.
    The serving connection lives in a name-mangled private attribute of
    PostgresChokePoint, is opened `default_transaction_read_only = on`,
    and is reachable only via `read()`, which demands a proof-of-passage
    FilteredQuery. There is no other handle to hold.

Postgres connection auth is UNTOUCHED: no new DB roles, no row-level
security. Enforcement is application-layer filtering above the single
existing app connection — the same isolation the FactStore promises on the
write side, made mandatory on the read side.

TRUST CHAIN (the request says WHAT, the principal says what it may SEE):

  credential --OpenBaoCredentialResolver--> Principal
      (server-side lookup; an opaque per-tenant token is hashed and resolved
       against the hub-owned vault registry — the caller cannot assert its
       own tenant, identity, or roles)
  Principal --PostgresChokePoint.enforce--> FilteredQuery
      (roles joined against label_role_grants; tenant + allowed_label_ids
       attached as mandatory predicates; anything identity-like on the
       incoming query object is DISCARDED and rebuilt from the principal)
  FilteredQuery --PostgresChokePoint.read--> rows
      (the only executor; refuses any FilteredQuery it did not itself mint,
       or that was mutated after minting)

FAIL CLOSED: a missing principal, an unresolvable credential, an unreachable
grants table, a template without a security marker — every failure mode
RAISES. Nothing ever runs unfiltered, and no FilteredQuery is produced
except by a real enforcement pass.

LABEL MODEL (flat, aligned with the S1 spine): grants and
`allowed_label_ids` are label IDs; a NULL `security_label_id` on a row
serves as 'public'. Membership is flat set membership — no hierarchy, no
inheritance, no per-row ACLs (each would be a new leak vector; add only on
real need). The seeded 'public' label id is granted to every resolved
principal, and the SQL predicate additionally passes NULL-labeled rows, so
public/NULL items are visible to all authenticated callers of their tenant.
Labels and roles are a GLOBAL vocabulary (the reference tables carry no
tenant); tenancy is enforced by the independent tenant_id predicate that is
always injected alongside the label check — the pair is never separable.

PERMISSION-INVISIBILITY IS SILENT: a filtered-out item is simply absent
from the rows `read()` returns. It is never reported, counted, or served as
`unknown` — revealing that a hidden item exists is itself a leak. This
happens logically BEFORE the S1 uncertainty states apply (the absence rule
in serving.py).

The S1 docstring's "never raises on 'no access'" and this module's
fail-closed rule are two sides of one line: a RESOLVED principal with no
grants gets a valid FilteredQuery that sees only public/NULL items (no
access ≠ error); an UNRESOLVED or missing identity gets an exception
(no identity = no query).

Graph note: AGE `cypher()` cannot take bind parameters, so serve-path
traversals go through SQL over `facts` (joins / recursive CTEs) via this
gateway. The AGE projection stays an internal-path structure until a
literal-safe cypher builder is designed and reviewed.
"""
from __future__ import annotations

import hashlib
import re
from abc import ABC, abstractmethod
from typing import Any, Mapping, Optional

import hvac
import hvac.exceptions
import psycopg
from psycopg.rows import dict_row
from pydantic import ValidationError

from knowledge_hub.config import settings
from knowledge_hub.serving import (
    ChokePoint,
    FilteredQuery,
    Principal,
    RetrievalQuery,
)

# The seeded default label (schema section 1). NULL security_label_id on a
# row and this label's id are BOTH "public": visible to every resolved
# principal of the row's tenant.
PUBLIC_LABEL_TEXT = "public"

# Template markers the gateway expands into mandatory predicates:
#   {sec:a}    -> (a.tenant_id = ... AND (a.security_label_id IS NULL OR
#                  a.security_label_id = ANY(...)))   [tables with a label]
#   {tenant:a} -> a.tenant_id = ...                   [label-less tables,
#                  e.g. chunks — their label lives on the parent document,
#                  so evidence reads must JOIN documents and {sec:} it]
#   {cur:a}    -> (a.valid_to IS NULL), or TRUE under an enforce()-scoped
#                  include_retracted audit query — the TEMPORAL predicate
#                  (migration 009). Mandatory for every read of the temporal
#                  tables (facts, documents): the same "unfiltered read is
#                  unwritable" discipline as {sec:}, on the other axis.
#                  Retraction ≠ permission: {sec:} hides silently, {cur:}
#                  widens only on explicit audit request and the rows come
#                  back honestly labeled (state='retracted').
_SEC_MARKER = re.compile(r"\{sec:([A-Za-z_][A-Za-z0-9_]*)\}")
_TENANT_MARKER = re.compile(r"\{tenant:([A-Za-z_][A-Za-z0-9_]*)\}")
_CUR_MARKER = re.compile(r"\{cur:([A-Za-z_][A-Za-z0-9_]*)\}")

# The tables whose rows carry the serve-relevant temporal axis. Every
# FROM/JOIN of one of these in a serve template must alias it and carry a
# {cur:<alias>} marker. (entities.valid_to is the identity axis — merge
# retirement — and stays an explicit inline predicate where ops need it.)
_TEMPORAL_TABLE = re.compile(
    r"\b(?:FROM|JOIN)\s+(?:facts|documents)\b\s+(?:AS\s+)?"
    r"([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)
_SQL_KEYWORDS = frozenset((
    "WHERE", "ON", "JOIN", "LEFT", "RIGHT", "INNER", "FULL", "CROSS",
    "GROUP", "ORDER", "LIMIT", "UNION", "USING", "AS", "SET", "HAVING",
    "WINDOW", "RETURNING", "AND", "OR"))

# Bind-parameter names the gateway owns; caller params may not collide.
_RESERVED_PARAMS = ("kh_tenant_id", "kh_allowed_label_ids")

# Attribute carrying the proof-of-passage stamp on a minted FilteredQuery.
_PROOF_ATTR = "_kh_enforced_proof"


# ---------------------------------------------------------------- refusals --
class EnforcementRefused(Exception):
    """The choke point refused to let a serve-path query proceed. Fail-closed
    umbrella: raised instead of EVER running unfiltered. Carries WHERE
    (tenant/principal ids), never query payloads, row data, or credential
    values."""

    def __init__(self, detail: str, tenant_id: Optional[str] = None,
                 principal_id: Optional[str] = None):
        self.tenant_id, self.principal_id = tenant_id, principal_id
        where = ""
        if tenant_id is not None:
            where = f" (tenant {tenant_id!r}"
            where += (f", principal {principal_id!r})"
                      if principal_id is not None else ")")
        super().__init__(f"{type(self).__name__}: {detail}{where}")


class PrincipalUnresolvable(EnforcementRefused):
    """The presented credential does not resolve to a server-side identity
    (unknown, revoked, malformed record, or the vault is unreachable)."""


class UnenforcedQuery(EnforcementRefused):
    """A query reached the gateway without a REAL enforcement pass: a
    hand-built FilteredQuery, one minted by a different choke point, or one
    mutated after enforce() stamped it."""


# -------------------------------------------------------- identity resolver --
class CredentialResolver(ABC):
    """Server-side identity seam: opaque credential in, resolved Principal
    out. The ONLY legitimate way a Principal enters the serving layer —
    request payloads never carry identity that anything trusts."""

    @abstractmethod
    def resolve_principal(self, credential: str) -> Principal:
        """Resolve an authenticated per-tenant credential to its Principal
        (tenant + identity + roles). Raises PrincipalUnresolvable on ANY
        failure — unknown credential, malformed record, vault error."""


class OpenBaoCredentialResolver(CredentialResolver):
    """CredentialResolver over the existing OpenBao seam (same KV v2 mount,
    client construction, and no-values-in-errors invariant as
    OpenBaoSecretsProvider).

    Layout: serving credentials are HUB-owned registry data, not tenant
    data, so they live under

        <mount>/serving/principals/<sha256(credential)>

    (not under tenants/<id>/... — a tenant's own vault policy must never
    read the registry that says who anyone is). The stored secret is
    {"tenant_id", "principal_id", "roles"}. The path keys on the sha256 of
    the credential, so the credential VALUE never appears in paths, logs,
    or exception messages — the digest is safe to surface, the token is not.
    """

    def __init__(self, client: Optional[hvac.Client] = None,
                 mount: Optional[str] = None):
        self._client = client or hvac.Client(
            url=settings.bao_addr, token=settings.bao_root_token)
        self._mount = mount or settings.bao_kv_mount

    @staticmethod
    def path_for(credential: str) -> str:
        """Vault path (relative to the KV mount) for one credential."""
        digest = hashlib.sha256(credential.encode("utf-8")).hexdigest()
        return f"serving/principals/{digest}"

    # ------------------------------------------------------------------ seam
    def resolve_principal(self, credential: str) -> Principal:
        if not isinstance(credential, str) or not credential.strip():
            raise PrincipalUnresolvable("empty credential")
        path = self.path_for(credential)
        try:
            response = self._client.secrets.kv.v2.read_secret_version(
                mount_point=self._mount, path=path,
                raise_on_deleted_version=True,
            )
        except hvac.exceptions.VaultError as e:
            # InvalidPath (unknown/revoked), Forbidden, transport — one
            # answer for all of them: refuse. hvac messages describe path
            # state, never stored values.
            raise PrincipalUnresolvable(
                f"credential does not resolve at {self._mount}/{path} "
                f"({type(e).__name__})") from e
        data = response["data"]["data"]
        if not isinstance(data, dict):
            raise PrincipalUnresolvable(f"malformed record at {self._mount}/{path}")
        try:
            principal = Principal(
                tenant_id=data["tenant_id"],
                principal_id=data["principal_id"],
                roles=list(data["roles"]),
            )
        except (KeyError, TypeError, ValidationError) as e:
            raise PrincipalUnresolvable(
                f"malformed record at {self._mount}/{path}") from e
        if not principal.tenant_id.strip() or not principal.principal_id.strip():
            raise PrincipalUnresolvable(
                f"blank identity in record at {self._mount}/{path}")
        return principal

    # ---------------------------------------------------------------- health
    def status(self) -> str:
        """'ok' | 'sealed' | 'unreachable' for the health surfaces (F1).
        A SEALED vault answers the health endpoint with a non-empty body —
        bool-testing that body reported vault:true while every credential
        was being refused, which is how a routine post-reboot state got
        diagnosed as mass credential loss. Deliberately NOT a resolution
        attempt: resolve_principal answers one refusal for every failure
        mode (by design), so health needs its own transport-level probe."""
        try:
            health = self._client.sys.read_health_status(method="GET")
        except Exception:
            return "unreachable"
        if isinstance(health, dict):
            return "sealed" if health.get("sealed") else "ok"
        # hvac hands back a Response object for some non-200 statuses.
        try:
            body = health.json()
            if isinstance(body, dict):
                return "sealed" if body.get("sealed") else "ok"
        except Exception:
            pass
        return ("ok" if getattr(health, "status_code", None) == 200
                else "unreachable")

    def ping(self) -> bool:
        """Vault is reachable AND usable. Sealed counts as NOT ok — a
        sealed vault refuses every credential, and health must not say
        'vault: true' while that is happening."""
        return self.status() == "ok"

    # ------------------------------------------------------------ provision
    def register_principal(self, credential: str, principal: Principal) -> None:
        """Provision/rotate one serving credential (setup + tests). Not part
        of the CredentialResolver ABC — serve-path code never writes the
        registry."""
        if not isinstance(credential, str) or not credential.strip():
            raise ValueError("credential must be a non-empty string")
        self._client.secrets.kv.v2.create_or_update_secret(
            mount_point=self._mount,
            path=self.path_for(credential),
            secret={
                "tenant_id": principal.tenant_id,
                "principal_id": principal.principal_id,
                "roles": list(principal.roles),
            },
        )


# ------------------------------------------------------------- choke point --
class PostgresChokePoint(ChokePoint):
    """The enforcement boundary: mints FilteredQuery (enforce) and is the
    only executor of serve-path SQL (read). See the module docstring for the
    trust chain and the internal/external boundary.

    Encapsulation is deliberate: the serving connection is name-mangled
    private, opened read-only, and has no accessor — S3/S4 hold a
    PostgresChokePoint, never a connection. Combined with the S1 type
    guarantee (S4 signatures accept only FilteredQuery) and the runtime
    proof-of-passage stamp, "forgot the permission filter" is unreachable
    by construction, by type, and by runtime check."""

    def __init__(self, dsn: Optional[str] = None,
                 resolver: Optional[CredentialResolver] = None):
        # The SERVING role: SELECT-only at the grant level. The
        # `SET default_transaction_read_only = on` below is now belt AND
        # braces rather than the only thing standing there — the server
        # refuses a write on this connection whether or not the client asks
        # it to.
        self.__dsn = dsn or settings.serving_dsn
        self.__resolver = resolver
        self.__connection: Optional[psycopg.Connection] = None
        # Per-instance sentinel: only enforce() can stamp it onto a query,
        # so a FilteredQuery built anywhere else can never present it.
        self.__proof = object()
        self.__public_label_id: Optional[int] = None

    # ------------------------------------------------------------- plumbing
    def __conn(self) -> psycopg.Connection:
        if self.__connection is None or self.__connection.closed:
            conn = psycopg.connect(self.__dsn, row_factory=dict_row,
                                   autocommit=True, connect_timeout=10)
            # Belt and braces: the serve path cannot write even if a
            # statement slips past the template checks.
            conn.execute("SET default_transaction_read_only = on;")
            conn.execute('SET search_path = public, ag_catalog, "$user";')
            self.__connection = conn
        return self.__connection

    def close(self) -> None:
        if self.__connection is not None and not self.__connection.closed:
            self.__connection.close()
        self.__connection = None

    def _public_label_id(self) -> int:
        if self.__public_label_id is None:
            row = self.__conn().execute(
                "SELECT id FROM security_labels WHERE label = %s",
                (PUBLIC_LABEL_TEXT,)).fetchone()
            if row is None:
                # Schema drift; serving blind would be worse than serving not.
                raise EnforcementRefused(
                    "seeded 'public' security label is missing")
            self.__public_label_id = row["id"]
        return self.__public_label_id

    def _grants_for(self, roles: list[str]) -> set[int]:
        """Flat label-id grant set for these roles: label_role_grants rows
        plus the always-granted public label. Deny-by-default — an unknown
        role simply matches no grants."""
        allowed = {self._public_label_id()}
        if roles:
            rows = self.__conn().execute(
                "SELECT DISTINCT label_id FROM label_role_grants"
                " WHERE role = ANY(%s::text[])",
                (list(roles),)).fetchall()
            allowed.update(r["label_id"] for r in rows)
        return allowed

    # ---------------------------------------------------------- enforcement
    def enforce(self, query: RetrievalQuery, principal: Principal) -> FilteredQuery:
        """The S1 seam, implemented. Fail closed: raises EnforcementRefused
        unless `principal` is a well-formed, resolvable identity. A resolved
        principal with no role grants is NOT an error — it gets a
        FilteredQuery that sees only public/NULL items of its tenant."""
        if not isinstance(query, RetrievalQuery):
            raise TypeError(
                f"enforce() takes a RetrievalQuery, got {type(query).__name__}")
        if not isinstance(principal, Principal):
            raise EnforcementRefused(
                "missing or invalid principal — the serve path never runs "
                "unfiltered")
        tenant_id = principal.tenant_id.strip()
        principal_id = principal.principal_id.strip()
        if not tenant_id or not principal_id:
            raise EnforcementRefused("principal has a blank identity",
                                     principal.tenant_id,
                                     principal.principal_id)
        try:
            allowed = self._grants_for(principal.roles)
        except psycopg.Error as e:
            raise EnforcementRefused(
                f"grant resolution failed ({type(e).__name__})",
                tenant_id, principal_id) from e

        # Identity comes ONLY from the principal: rebuild from the base
        # RetrievalQuery fields, so tenant/labels asserted on the incoming
        # object (e.g. a forged FilteredQuery) are discarded here.
        base = {name: getattr(query, name) for name in RetrievalQuery.model_fields}
        filtered = FilteredQuery(
            **base,
            tenant_id=tenant_id,
            principal_id=principal_id,
            allowed_label_ids=sorted(allowed),
        )
        # Proof-of-passage: the sentinel says WE minted it; the snapshot says
        # nobody widened it afterwards — including the temporal audit flag,
        # so flipping include_retracted post-mint is a refusal, not a leak.
        # read() checks all of it.
        object.__setattr__(filtered, _PROOF_ATTR, (
            self.__proof, filtered.tenant_id, filtered.principal_id,
            tuple(filtered.allowed_label_ids),
            bool(filtered.include_retracted)))
        return filtered

    def enforce_credential(self, query: RetrievalQuery,
                           credential: str) -> FilteredQuery:
        """Preferred external entry: resolve the credential server-side,
        then enforce. Requires a resolver to have been wired in."""
        if self.__resolver is None:
            raise EnforcementRefused(
                "no credential resolver configured on this choke point")
        return self.enforce(query, self.__resolver.resolve_principal(credential))

    # -------------------------------------------------------------- gateway
    def read(self, query: FilteredQuery, sql: str,
             params: Optional[Mapping[str, Any]] = None) -> list[dict[str, Any]]:
        """Execute one serve-path SELECT under the query's enforcement scope.

        `sql` is a single read-only statement using NAMED bind parameters
        (%(name)s; escape literal % as %%) containing at least one {sec:<alias>}
        marker — one per label-bearing table it touches — plus {tenant:<alias>}
        for label-less tables (chunks) and {cur:<alias>} for every aliased
        read of a temporal table (facts, documents). The markers expand to
        the mandatory tenant + label + currency predicates bound to THIS
        query's enforced scope; a template that names no {sec:} marker, or
        that reads a temporal table without its {cur:} marker, is refused —
        an unfiltered read is unwritable, not merely unlikely.

        Rows that fail the permission predicates are silently absent from
        the result — never surrogated, counted, or reported
        (permission-invisibility). Rows that fail the TEMPORAL predicate are
        absent by default but reachable through an enforce()-scoped
        include_retracted audit query, where they serve honestly labeled —
        deletion is a temporal state, not a permission."""
        if not isinstance(query, FilteredQuery):
            raise TypeError(
                "serve-path reads take a FilteredQuery — run enforce() first "
                f"(got {type(query).__name__})")
        proof = getattr(query, _PROOF_ATTR, None)
        if not (isinstance(proof, tuple) and len(proof) == 5
                and proof[0] is self.__proof):
            raise UnenforcedQuery(
                "FilteredQuery was not minted by this choke point's enforce()",
                query.tenant_id, query.principal_id)
        if (query.tenant_id, query.principal_id,
                tuple(query.allowed_label_ids),
                bool(query.include_retracted)) != proof[1:]:
            raise UnenforcedQuery(
                "FilteredQuery was mutated after enforcement",
                query.tenant_id, query.principal_id)

        body = sql.strip()
        if ";" in body:
            raise EnforcementRefused(
                "multi-statement SQL is refused on the serve path",
                query.tenant_id, query.principal_id)
        if not body.upper().startswith(("SELECT", "WITH")):
            raise EnforcementRefused(
                "serve path is read-only: statement must be SELECT/WITH",
                query.tenant_id, query.principal_id)
        if not _SEC_MARKER.search(body):
            raise EnforcementRefused(
                "template carries no {sec:<alias>} marker — an unmarked "
                "read cannot transit the choke point",
                query.tenant_id, query.principal_id)
        if params is not None and not isinstance(params, Mapping):
            raise EnforcementRefused(
                "gateway params must be a named mapping (%(name)s style)",
                query.tenant_id, query.principal_id)
        bind = dict(params or {})
        for reserved in _RESERVED_PARAMS:
            if reserved in bind:
                raise EnforcementRefused(
                    f"param name {reserved!r} is reserved by the gateway",
                    query.tenant_id, query.principal_id)
        # Temporal discipline (migration 009), same fail-closed shape as
        # {sec:}: every aliased read of a temporal table must carry its
        # {cur:<alias>} marker, so "forgot the retraction filter" is
        # unwritable, not merely unlikely. (Checked last so more specific
        # template errors keep their own refusal messages.)
        temporal_aliases = set()
        for match in _TEMPORAL_TABLE.finditer(body):
            alias = match.group(1)
            if alias.upper() in _SQL_KEYWORDS:
                raise EnforcementRefused(
                    "facts/documents must be aliased so the {cur:<alias>} "
                    "temporal marker can bind",
                    query.tenant_id, query.principal_id)
            temporal_aliases.add(alias)
        unmarked = temporal_aliases - set(_CUR_MARKER.findall(body))
        if unmarked:
            raise EnforcementRefused(
                f"template reads temporal table alias(es) {sorted(unmarked)} "
                f"without a {{cur:<alias>}} marker — a temporally "
                f"unfiltered read cannot transit the choke point",
                query.tenant_id, query.principal_id)

        rendered = _SEC_MARKER.sub(
            lambda m: self._security_predicate(m.group(1)), body)
        rendered = _TENANT_MARKER.sub(
            lambda m: f"{m.group(1)}.tenant_id = %(kh_tenant_id)s", rendered)
        # Temporal predicate from the VERIFIED snapshot (proof[4]), never
        # from a field the caller could have touched since minting. Default:
        # current rows only. Audit scope: the marker collapses to TRUE and
        # retracted rows return honestly labeled — while every {sec:}
        # predicate above still applies unchanged (temporal ≠ permission).
        include_retracted = proof[4]
        rendered = _CUR_MARKER.sub(
            lambda m: ("TRUE" if include_retracted
                       else f"({m.group(1)}.valid_to IS NULL)"), rendered)
        # Bind the enforced scope from the verified proof snapshot, not from
        # anything the caller could have touched since.
        bind["kh_tenant_id"] = proof[1]
        bind["kh_allowed_label_ids"] = list(proof[3])
        return self.__conn().execute(rendered, bind).fetchall()

    @staticmethod
    def _security_predicate(alias: str) -> str:
        # NULL security_label_id serves as 'public' (S1 spine): explicitly
        # passed here; every explicit label must be in the enforced set.
        return (
            f"({alias}.tenant_id = %(kh_tenant_id)s"
            f" AND ({alias}.security_label_id IS NULL"
            f" OR {alias}.security_label_id ="
            f" ANY(%(kh_allowed_label_ids)s::bigint[])))"
        )
