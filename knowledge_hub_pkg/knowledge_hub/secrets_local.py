"""Local-posture credential store — one gitignored file, no vault.

d.s Stage 3. In LOCAL posture (see config.py), credentials come from a single
JSON file instead of OpenBao, so nothing needs a vault to start, run, or ingest.
DEPLOYED posture never opens this file; the OpenBao path is untouched and one
setting away.

WHY A FILE IS THE RIGHT ANSWER HERE, and where it stops being one:

OpenBao earns its complexity when credentials must be shared between machines,
scoped per tenant by policy, rotated without redeploying, and readable by
people who must not read each other's. None of that describes a single-user
internal tool on one box. What remains of a vault in that setting is a process
to start, a seal to unseal after every reboot, five shares to have not lost,
and a root token in a .env — all of it protecting a secret from an attacker who,
by construction, already has the disk. The file is not a downgrade; it is the
same threat model with the ritual removed.

It stops being right the moment a SECOND person or a SECOND machine is real. At
that point the sharing, the per-tenant policy, and the rotation-without-redeploy
all come back, and so does OpenBao — by flipping KH_POSTURE, not by rewriting
this.

TWO PILES, ONE FILE. The vault holds credentials at two unrelated path layouts
and this file mirrors both, keeping the paths recognizable so an operator moving
between postures reads the same shapes:

    OpenBao  <mount>/tenants/<tenant>/sources/<ref>   ->  "sources"
    OpenBao  <mount>/serving/principals/<sha256>      ->  "principals"

    {
      "version": 1,
      "sources":    {"<tenant>": {"<source_ref>": {"username": "...", ...}}},
      "principals": {"<sha256(token)>": {"tenant_id": "...",
                                         "principal_id": "...",
                                         "roles": ["operator"]}}
    }

Principals are keyed on the sha256 of the credential, exactly as the vault path
is, so the token VALUE is never a key, never in an error message, and never in a
log line. The digest is safe to surface; the token is not. Storing the token in
plaintext would buy nothing — this file is read by the process that authenticates
with it — but keying on the digest costs nothing and keeps every diagnostic
surface (paths, keys, exceptions) free of the live value.

INVARIANTS, matching secrets_openbao.py because callers cannot tell the two
apart and must not have to:
  * secret VALUES never appear in logs, exception messages, or return values,
    except through the explicit `get_secret` escape hatch;
  * the same exception types for the same conditions — SecretNotFound,
    SecretAccessDenied, SecretsError, PrincipalUnresolvable;
  * writes are ATOMIC (temp file + os.replace). A torn write here would lose the
    console principal registry, which is the one thing that cannot be re-derived
    from anywhere else on the box.

Out of scope, named so it is not silently assumed:
  1. Multi-user and secret rotation-by-policy: that is the deployed posture's
     job, reintroduced with it when a real second user exists.
  2. Encryption at rest. It would be theater: the key would have to live beside
     the file, readable by the same process. File permissions are the honest
     boundary, and on a single-user box they are the same boundary the .env
     already relies on.
  3. This file is NOT a provenance or correctness surface. Nothing here gates
     grounding, the ontology allowlist, the migration ledger, or adjudication.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets as pysecrets
import stat
import tempfile
from pathlib import Path
from typing import Any, Mapping, Optional

from knowledge_hub.choke_point import CredentialResolver, PrincipalUnresolvable
from knowledge_hub.config import settings
from knowledge_hub.interfaces import (
    CredentialRotator,
    OutboundRequest,
    SecretAccessDenied,
    SecretNotFound,
    SecretsError,
    SecretsProvider,
)
from knowledge_hub.serving import Principal

logger = logging.getLogger(__name__)

STORE_VERSION = 1

# The auto-provisioned local console identity. One principal, one box, one
# person — see ensure_local_operator().
LOCAL_PRINCIPAL_ID = "local-operator"
LOCAL_TENANT_FALLBACK = "default"
LOCAL_ROLES = ("operator",)
# Token shape is `kh-<role>-<tenant>-<32 hex>`, produced by ONE function
# (provision_local_credential) and matching what
# deploy_apply.provision_operator_credential mints in the vault path — so
# nothing downstream can tell a local credential from a deployed one by looking
# at it, and a token seen in some other program's config is recognizable as a
# d.s credential with its role legible.
TOKEN_HEX_BYTES = 16


def credential_digest(credential: str) -> str:
    """sha256 of a credential — the principal key, and the ONLY form of a token
    that may appear in a path, key, log line, or exception."""
    return hashlib.sha256(credential.encode("utf-8")).hexdigest()


class LocalCredentialStore:
    """Read/write access to the local credential file.

    Deliberately re-reads on every operation rather than caching. The file is
    small, the posture is single-user, and a cache would introduce a staleness
    question ("did khctl write a principal after the console loaded?") whose
    only honest answer is more invalidation machinery. Reading a few kilobytes
    is cheaper than being wrong about it.
    """

    def __init__(self, path: Optional[Path | str] = None):
        # Relative paths resolve against the working directory — the deployment
        # home under khctl launch, the infra root on the bench — the same
        # convention as ontology_dir and bge_m3_tokenizer_json.
        self._path = Path(path) if path is not None else Path(
            settings.local_secrets_file)

    @property
    def path(self) -> Path:
        return self._path

    def exists(self) -> bool:
        return self._path.is_file()

    # -------------------------------------------------------------- reading
    def read(self) -> dict[str, Any]:
        """The whole store, or an empty one when the file does not exist yet.

        A MISSING FILE IS EMPTY, NOT AN ERROR: first run has nothing to read
        and must still start. A file that exists but is CORRUPT is an error —
        the difference matters, because silently treating unparseable JSON as
        empty would auto-provision a second identity beside a registry that is
        still sitting there, and the operator would be told everything is fine.
        """
        if not self._path.is_file():
            return {"version": STORE_VERSION, "sources": {}, "principals": {}}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise SecretsError(
                "-", "-",
                f"{self._path} is not readable JSON ({type(e).__name__}) — "
                f"refusing to treat a damaged credential store as empty; fix "
                f"or delete it") from e
        if not isinstance(raw, dict):
            raise SecretsError("-", "-",
                               f"{self._path} must hold a JSON object")
        version = raw.get("version", STORE_VERSION)
        if version != STORE_VERSION:
            raise SecretsError(
                "-", "-",
                f"{self._path} is store version {version!r}; this build reads "
                f"version {STORE_VERSION}")
        raw.setdefault("sources", {})
        raw.setdefault("principals", {})
        return raw

    # -------------------------------------------------------------- writing
    def write(self, data: Mapping[str, Any]) -> None:
        """Replace the store ATOMICALLY: temp file in the same directory, then
        os.replace, which is atomic on Windows and POSIX alike.

        Same discipline as everywhere else state matters in this codebase. The
        principal registry is the one thing on the box that cannot be
        re-derived from anything else — lose it mid-write and the console has
        no identity to authenticate, with no upstream to rebuild from. A
        crash-safe write is not ceremony; it is the reason this file can be
        trusted as a store at all.
        """
        payload = {"version": STORE_VERSION,
                   "sources": dict(data.get("sources", {})),
                   "principals": dict(data.get("principals", {}))}
        parent = self._path.parent
        parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(parent),
                                        prefix=".secrets.local.",
                                        suffix=".tmp")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            _restrict_permissions(tmp)
            os.replace(tmp, self._path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def update(self, mutate) -> dict[str, Any]:
        """read -> mutate -> write, returning the written store.

        Single-writer by construction (one box, one person), so no
        compare-and-swap — the same argument that makes opaque cursor
        checkpoints safe in the capture path.
        """
        data = self.read()
        mutate(data)
        self.write(data)
        return data


def _restrict_permissions(path: Path) -> None:
    """Owner-only, best effort.

    On POSIX this is chmod 600. On Windows it is a no-op: the meaningful ACL
    work there is icalcs-shaped and changing a user's ACLs is a system-state
    change this build has no mandate for. Said plainly rather than pretended:
    on Windows this file is protected exactly as much as the .env beside it,
    which is the boundary the pilot already relies on.
    """
    if os.name == "nt":
        return
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError as e:
        logger.warning("could not restrict permissions on %s: %s", path, e)


# ===========================================================================
# Pile A — source credentials
# ===========================================================================
class LocalFileSecretsProvider(SecretsProvider, CredentialRotator):
    """SecretsProvider over the local file. Contract-identical to
    OpenBaoSecretsProvider, including which exception means what — capture-path
    code holds a SecretsProvider and must not be able to tell them apart.

    Implements CredentialRotator for the same reason the OpenBao one does: QBO
    re-issues its refresh token on every refresh, and an adapter that declares
    rotation needs a write path in every posture or the connector locks itself
    out after one use.
    """

    def __init__(self, store: Optional[LocalCredentialStore] = None):
        self._store = store or LocalCredentialStore()

    @property
    def store(self) -> LocalCredentialStore:
        return self._store

    def path_for(self, tenant_id: str, source_ref: str) -> str:
        """Where this source's credential BELONGS, as a human-readable
        location. The local twin of OpenBaoSecretsProvider.path_for.

        NOT optional and not cosmetic: operator_http._credential_info calls this
        to tell the console where to put a credential and whether one is there
        yet. Without it the console's add_source answered HTTP 500 in local
        posture — found by test_operator_http, after a surface-parity test of
        mine had explicitly excused this one method. A seam is only a seam if
        every member callers actually use exists on both sides.

        An instance method, where the vault version is a staticmethod, because
        the answer includes WHICH file — a store path is only meaningful with it.
        Callers invoke it on an instance either way.
        """
        return f"{self._store.path}::sources.{tenant_id}.{source_ref}"

    # ------------------------------------------------------------------ seam
    def inject_credential(self, tenant_id: str, source_ref: str,
                          request: OutboundRequest) -> None:
        for key, value in self._read(tenant_id, source_ref).items():
            request.attach_secret(key, value)

    def get_secret(self, tenant_id: str, source_ref: str) -> Mapping[str, Any]:
        return self._read(tenant_id, source_ref)

    # ------------------------------------------------------------ provision
    def put_secret(self, tenant_id: str, source_ref: str,
                   secret: Mapping[str, Any]) -> None:
        """Provision a source credential. Not part of the SecretsProvider ABC —
        capture-flow code never writes secrets."""
        if not isinstance(secret, Mapping) or not secret:
            raise SecretAccessDenied(tenant_id, source_ref,
                                     "refusing to store an empty credential")

        def mutate(data):
            data["sources"].setdefault(tenant_id, {})[source_ref] = dict(secret)

        self._store.update(mutate)

    # -------------------------------------------------------------- rotation
    def rotate_credential(self, tenant_id: str, source_ref: str,
                          updates: Mapping[str, Any]) -> None:
        """MERGE `updates` over the stored credential — named fields replaced,
        unnamed fields kept (a refresh-token rotation must never drop the
        client_id beside it). Read-modify-write inside one atomic store write."""
        current = dict(self._read(tenant_id, source_ref))
        current.update(updates)
        self.put_secret(tenant_id, source_ref, current)

    # -------------------------------------------------------------- internal
    def _read(self, tenant_id: str, source_ref: str) -> dict[str, Any]:
        # Errors name the FILE and the tenant/source, never a value — the same
        # invariant as the vault implementation, which reports paths only.
        sources = self._store.read()["sources"]
        if not isinstance(sources, dict):
            raise SecretsError(tenant_id, source_ref,
                               f"malformed 'sources' in {self._store.path}")
        per_tenant = sources.get(tenant_id)
        if not isinstance(per_tenant, dict) or source_ref not in per_tenant:
            raise SecretNotFound(
                tenant_id, source_ref,
                f"no credential in {self._store.path} at "
                f"sources.{tenant_id}.{source_ref}")
        secret = per_tenant[source_ref]
        if not isinstance(secret, dict) or not secret:
            raise SecretNotFound(
                tenant_id, source_ref,
                f"empty credential in {self._store.path} at "
                f"sources.{tenant_id}.{source_ref}")
        return dict(secret)


# ===========================================================================
# Pile B — the login identity registry
# ===========================================================================
class LocalFileCredentialResolver(CredentialResolver):
    """CredentialResolver over the local file. Contract-identical to
    OpenBaoCredentialResolver: opaque credential in, resolved Principal out,
    one refusal for every failure mode.

    THIS is the class that makes local posture possible. Source credentials
    turned out to be dead weight internally — FilesystemSourceAdapter needs
    none — but both HTTP boundaries authenticate ONLY through a
    CredentialResolver, so without a non-vault implementation you cannot open
    your own console without OpenBao running. Everything the choke point does
    downstream is unchanged: this resolves an identity, it does not bypass
    enforcement. The role gate, the tenant filter, and the audit rows all still
    happen, on a Principal that came from here instead of from a vault.
    """

    def __init__(self, store: Optional[LocalCredentialStore] = None):
        self._store = store or LocalCredentialStore()

    @property
    def store(self) -> LocalCredentialStore:
        return self._store

    @staticmethod
    def key_for(credential: str) -> str:
        """Store key for one credential: the digest, never the value."""
        return credential_digest(credential)

    # ------------------------------------------------------------------ seam
    def resolve_principal(self, credential: str) -> Principal:
        if not isinstance(credential, str) or not credential.strip():
            raise PrincipalUnresolvable("empty credential")
        digest = self.key_for(credential)
        try:
            principals = self._store.read()["principals"]
        except SecretsError as e:
            # A damaged store refuses every credential, and says so as a
            # refusal rather than leaking the parse error to an HTTP client.
            raise PrincipalUnresolvable(
                f"credential store unreadable ({type(e).__name__})") from e
        record = principals.get(digest) if isinstance(principals, dict) else None
        if record is None:
            # The digest is safe to surface; it is what the store is keyed on
            # and reveals nothing about the token.
            raise PrincipalUnresolvable(
                f"credential does not resolve at principals.{digest[:12]}…")
        if not isinstance(record, dict):
            raise PrincipalUnresolvable(
                f"malformed record at principals.{digest[:12]}…")
        try:
            principal = Principal(
                tenant_id=record["tenant_id"],
                principal_id=record["principal_id"],
                roles=list(record["roles"]),
            )
        except Exception as e:
            raise PrincipalUnresolvable(
                f"malformed record at principals.{digest[:12]}…") from e
        if not principal.tenant_id.strip() or not principal.principal_id.strip():
            raise PrincipalUnresolvable(
                f"blank identity in record at principals.{digest[:12]}…")
        return principal

    # ---------------------------------------------------------------- health
    def status(self) -> str:
        """'ok' | 'unreachable', matching the vault resolver's health contract.

        There is no 'sealed' — a file has no seal, which is most of the point of
        this posture. The health surfaces branch on these strings (F1), so the
        vocabulary is shared even though one value can never occur here.
        """
        try:
            self._store.read()
        except SecretsError:
            return "unreachable"
        return "ok"

    def ping(self) -> bool:
        return self.status() == "ok"

    # ------------------------------------------------------------ provision
    def register_principal(self, credential: str,
                           principal: Principal) -> None:
        """Provision one console/serving credential. Not part of the
        CredentialResolver ABC — serve-path code never writes the registry."""
        if not isinstance(credential, str) or not credential.strip():
            raise ValueError("credential must be a non-empty string")

        def mutate(data):
            data["principals"][self.key_for(credential)] = {
                "tenant_id": principal.tenant_id,
                "principal_id": principal.principal_id,
                "roles": list(principal.roles),
            }

        self._store.update(mutate)

    def revoke_principal(self, credential: str) -> bool:
        """Remove one credential. True if something was removed.

        Not on the vault side today, and added here because local posture has no
        other way to retire a token: with no vault UI and no provision-ceremony
        to re-run, deleting the row IS the revocation path.
        """
        digest = self.key_for(credential)
        removed = {"hit": False}

        def mutate(data):
            removed["hit"] = data["principals"].pop(digest, None) is not None

        self._store.update(mutate)
        return removed["hit"]

    def find_principal_credential(self,
                                  principal_id: str = LOCAL_PRINCIPAL_ID
                                  ) -> Optional[str]:
        """Always None: the store keeps DIGESTS, so a token cannot be read back
        out of it by design.

        Exists to make that explicit rather than leaving a caller to wonder. The
        local session token is handed to the console at mint time (see
        ensure_local_operator) — it is never recovered afterwards, in either
        posture. Recovering it would mean storing it in plaintext, which is the
        one thing the digest keying is here to avoid.
        """
        return None


# ===========================================================================
# Auto-provisioning — nothing for a human to record or type
# ===========================================================================
def ensure_local_operator(store: Optional[LocalCredentialStore] = None,
                          tenant_id: Optional[str] = None) -> tuple[str, bool]:
    """Guarantee a local console identity exists. Returns (token, minted_now).

    The requirement this satisfies: in local posture a human should never have
    to record or type anything except real connector credentials. So the first
    time anything needs the console, an operator principal is minted, written,
    and handed straight to the caller — no print-once value, no lock screen, no
    "type RECORDED".

    NOT IDEMPOTENT IN THE TOKEN, and this is the honest part. Because the store
    holds digests, an existing principal's token cannot be read back out. So
    when a local-operator principal is already registered under a DIFFERENT
    token, this mints a fresh one, registers it alongside, and returns that. The
    old row is left in place rather than deleted: it may be the token a console
    tab is still holding in sessionStorage, and silently invalidating a working
    session to keep the file tidy is a worse trade than a few dead rows in a
    single-user file. `revoke_principal` is there for when you actually want one
    gone.

    Mints through provision_local_credential rather than generating its own
    token, so the console identity has the SAME SHAPE as every other credential
    in the system (`kh-operator-<tenant>-<hex>`). This function used to call
    token_urlsafe directly while a comment above claimed shape parity — a claim
    the browser round-trip disproved in one line. One minting path now, so the
    claim cannot drift again.
    """
    store = store or LocalCredentialStore()
    tenant = (tenant_id or _default_local_tenant() or LOCAL_TENANT_FALLBACK)

    existing = [record for record in store.read()["principals"].values()
                if isinstance(record, dict)
                and record.get("principal_id") == LOCAL_PRINCIPAL_ID]

    token, _ = provision_local_credential(
        tenant, LOCAL_ROLES, actor="auto:local-posture",
        label="local console identity (self-login)", store=store,
        principal_id=LOCAL_PRINCIPAL_ID)
    return token, not existing


def provision_local_credential(tenant: str, roles: tuple[str, ...],
                               actor: str,
                               label: str = "credential",
                               store: Optional[LocalCredentialStore] = None,
                               principal_id: Optional[str] = None
                               ) -> tuple[str, str]:
    """Mint + register ONE credential in the local store. Returns
    (token, principal_id) — the caller owns any print-once handling.

    The local twin of deploy_apply.provision_operator_credential /
    provision_agent_credential, and deliberately the same SHAPE: same token
    format, same principal_id format, same attribution fields riding the record
    that the resolver ignores. A credential minted here is indistinguishable
    downstream from one minted by the vault path, which is what lets the same
    console, the same role gate, and the same audit trail serve both postures.

    Empty `roles` means an AGENT serving principal — it can read through the
    serving boundary and perform no operator write. That is a real integration
    need rather than ceremony (an external agent must be handed a token
    somehow), which is why this path survives in local posture at all while the
    custody ceremony around it does not.

    `principal_id` overrides the generated one, mirroring
    deploy_apply.provision_agent_credential's parameter of the same name. Used by
    ensure_local_operator, whose identity must be STABLE across runs so a later
    run can recognize that a console identity already exists.
    """
    from datetime import datetime, timezone

    store = store or LocalCredentialStore()
    role_part = roles[0] if roles else tenant
    token = f"kh-{role_part}-{tenant}-{pysecrets.token_hex(TOKEN_HEX_BYTES)}"
    principal_id = (principal_id
                    or f"{tenant}-{role_part}-{pysecrets.token_hex(3)}")

    def mutate(data):
        data["principals"][credential_digest(token)] = {
            "tenant_id": tenant,
            "principal_id": principal_id,
            "roles": list(roles),
            # Attribution rides the record; the resolver reads only the
            # identity triple above and ignores these. Same as the vault path.
            "provisioned_by": actor,
            "provisioned_at": datetime.now(timezone.utc).isoformat(),
            "label": label,
        }

    store.update(mutate)
    return token, principal_id


def _default_local_tenant() -> Optional[str]:
    """The tenant a local console should land in: the first configured serving
    tenant if there is one, else None so the caller falls back."""
    tenants = [t.strip() for t in settings.serving_tenants.split(",")
               if t.strip()]
    return tenants[0] if tenants else None
