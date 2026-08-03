"""Operator/admin write API — the write-twin of the read choke point (BP19).

The read serving layer (service_http.py) is READ-ONLY by design and stays
that way: its choke-point connection runs `default_transaction_read_only=on`
and nothing here touches it. Operator actions — resolve reviews, control
ingestion, acknowledge errors, manage sources — go through THIS separate,
enforced, audited write path. The read path's provable read-only property is
not weakened, not routed around, and gains no write door.

THE SAME DISCIPLINE, MIRRORED ONTO WRITES:

  * Server-side identity, REUSED. The caller presents an opaque bearer
    token; S2's `OpenBaoCredentialResolver` resolves it to a Principal.
    Operator/reviewer principals carry WRITE roles ('reviewer',
    'operator') — distinct from agent read-principals, whose label roles
    grant no write action at all. Tenant and role are NEVER caller-asserted:
    a body field naming a tenant is an unknown param, refused.
  * Fixed NAMED write operations, not arbitrary mutation. `WriteOperation`
    specs registered at build time; endpoints are generated from that
    registry; there is no raw-SQL / free-mutation surface to reach.
  * ONE write choke point, tenant-scoped, fail-closed, deny-by-default.
    `OperatorGate.execute()` is the single dispatch: role gate first (a
    write op MUST declare a non-empty role scope — an unscoped write spec
    is unregistrable), then the principal's tenant_id is injected as THE
    tenant for the handler. Handlers cannot receive a tenant any other way,
    so a write can never touch another tenant's data — a cross-tenant
    target simply does not exist in the handler's scope (LookupError ->
    404, the absence rule on the write side).
  * Writes call INTO existing domain logic — mutations are not
    reimplemented here. Merge/keep-separate/split -> ResolutionService's
    reversible entity_merges machinery; quarantine triage -> the 004
    quarantine table; pause/resume -> the 002 source registry (capture
    already skips disabled sources); retry/acknowledge -> the outbox rows.
    Reversibility is preserved where the domain has it.
  * EVERY write is AUDITED (migration 010 operator_audit: principal ·
    action · target · params · outcome · reversible-snapshot ref ·
    timestamp) — including REFUSED attempts, which are their own security
    signal. Review decisions ALSO feed the flywheel: decide_match /
    resolve_as_new / reverse_merge write `labels` rows (005 §3.4) inside
    the domain logic — one action, two records (audit + label).
  * Credentials NEVER touch this API. add_source/edit_scope refuse
    credential-shaped config keys structurally; the response points at the
    OpenBao path where the secret belongs (the vault flow) and reports only
    whether one is PRESENT — the value is never received, stored, or
    logged.

Alert model: an "alert" is an existing error condition, not a new event
stream — the `operator_alerts` view (010) lists unacknowledged failed queue
items and degraded sources. acknowledge_alert marks a queue row seen;
retry_failed_item requeues it (and clears the ack); a degraded source
clears by being FIXED (resume_source / a healthy run), not dismissed.

Explicitly NOT built (bookmarked): `start_pull` — a caller-triggered
capture run needs a pull-request queue the capture runner consumes; capture
runs are scheduled/batch today (`khctl ingest`). The op registry is where
it lands when that queue exists. No other write surface is deferred.

Keep in lock-step with: choke_point.py (identity), resolution.py (review
actions + reversibility), capture.py (source registry), models.py +
migrations/010 (audit + ack columns), config.py (operator_* settings).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, model_validator

from knowledge_hub.capture import SourceRegistry
from knowledge_hub.choke_point import (
    CredentialResolver,
    OpenBaoCredentialResolver,
    PrincipalUnresolvable,
)
from knowledge_hub.config import settings
from knowledge_hub.factstore_pg import PostgresFactStore
from knowledge_hub.interfaces import SecretNotFound, SecretsProvider
from knowledge_hub.models import Label, OperatorAudit
from knowledge_hub.ontology_registry import (
    OntologyValidationError,
    save_ontology_file,
    validate_ontology_set,
)
from knowledge_hub.pipeline import Pipeline
from knowledge_hub.resolution import ResolutionService
from knowledge_hub.serving import Principal
from knowledge_hub.operator_reads import OperatorReadService
from knowledge_hub.service_http import (
    LatencyStats,
    RawResponse,
    installed_version,
    make_server,
)

# The operator UI ships INSIDE the package (BP20): rebuilt from Design's
# .dc.html spec (markup + inline styles verbatim, the design-tool runtime
# replaced with plain fetch/render JS), served statically from this service
# so the whole console is one origin, one process, air-gap clean.
_UI_DIR = Path(__file__).resolve().parent / "operator_ui"
_UI_TYPES = {".html": "text/html; charset=utf-8",
             ".js": "text/javascript; charset=utf-8",
             ".css": "text/css; charset=utf-8"}

# The write roles. Deny-by-default is structural: a WriteOperation MUST
# declare a non-empty scope, and an unknown role matches nothing. 'operator'
# strictly includes 'reviewer' powers by scoping review ops to both.
ROLE_REVIEWER = "reviewer"
ROLE_OPERATOR = "operator"
REVIEW_SCOPE = [ROLE_REVIEWER, ROLE_OPERATOR]
OPERATE_SCOPE = [ROLE_OPERATOR]

WRITE_PARAM_TYPES = ("str", "int", "float", "bool", "dict")

# Identity may only ever come from the resolved principal — a spec that
# tries to declare it as a caller param is unregistrable.
_RESERVED_PARAMS = ("tenant", "tenant_id", "principal", "principal_id",
                    "reviewer", "roles")

_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

# Credential-shaped config keys are refused structurally: secrets travel the
# vault flow, never this API (matches source_registry.config's contract).
_CREDENTIAL_KEY = re.compile(
    r"(?i)(password|passwd|secret|token|credential|api_?key|private_?key)")

_ALERT_QUEUES = {"dispatch": "dispatch_queue", "extraction": "extraction_queue"}


def _parse_globs(field: str, raw: Any) -> Optional[list[str]]:
    """Comma-separated glob patterns -> list (WriteParamSpec has no list
    type — str in, parsed deterministically). None/blank -> no filter."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    if not isinstance(raw, str):
        raise WriteCallError(f"ingest_folder: {field} must be a "
                             f"comma-separated string of glob patterns")
    patterns = [p.strip() for p in raw.split(",") if p.strip()]
    return patterns or None


# ---------------------------------------------------------------- refusals --
class UnknownWriteOperation(Exception):
    """No such write operation is registered (absence, not description)."""


class WriteRefused(Exception):
    """The role gate refused this principal for this action (deny-by-default
    — carries WHO/WHAT, never payload)."""

    def __init__(self, action: str, principal: Principal):
        self.action = action
        super().__init__(
            f"principal {principal.principal_id!r} (tenant "
            f"{principal.tenant_id!r}) may not perform {action!r}")


class WriteCallError(Exception):
    """A syntactically valid call carried bad params."""


class WriteOperationRejected(Exception):
    """The registry refused a write-op spec at registration (build time)."""


# -------------------------------------------------------------------- specs --
class WriteParamSpec(BaseModel):
    """One typed parameter of a write operation — declarative, mirrored from
    the read side's ParamSpec but with `dict` (adapter config) instead of
    the retrieval-only types."""
    model_config = ConfigDict(extra="forbid")

    type: str = "str"
    required: bool = False
    default: Any = None
    choices: Optional[list[Any]] = None
    description: str = ""

    @model_validator(mode="after")
    def _check(self) -> "WriteParamSpec":
        if self.type not in WRITE_PARAM_TYPES:
            raise ValueError(f"param type must be one of {WRITE_PARAM_TYPES}")
        return self


class WriteOperation(BaseModel):
    """One declarative write action: name, typed params, and the ROLE SCOPE
    that may perform it. scope is ANY-of and MUST be non-empty — an
    unscoped (everyone-may-write) operation is structurally unexpressible."""
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    params: dict[str, WriteParamSpec] = Field(default_factory=dict)
    scope: list[str] = Field(min_length=1)
    notes: Optional[str] = None


class WriteOutcome(BaseModel):
    """What a handler returns: the caller-visible result, the audited
    target ('kind:id'), and the domain's reversible-snapshot ref when one
    exists ('entity_merges:<id>')."""
    model_config = ConfigDict(extra="forbid")

    result: dict[str, Any] = Field(default_factory=dict)
    target: str
    snapshot_ref: Optional[str] = None


def _coerce(op: str, name: str, spec: WriteParamSpec, value: Any) -> Any:
    if value is None:
        if spec.required and spec.default is None:
            raise WriteCallError(f"{op}: param {name!r} is required")
        value = spec.default
    if value is None:
        return None
    t = spec.type
    ok = (isinstance(value, str) if t == "str"
          else isinstance(value, bool) if t == "bool"
          else isinstance(value, int) and not isinstance(value, bool) if t == "int"
          else isinstance(value, (int, float)) and not isinstance(value, bool) if t == "float"
          else isinstance(value, dict) if t == "dict"
          else False)
    if not ok:
        raise WriteCallError(
            f"{op}: param {name!r} must be {t}, got {type(value).__name__}")
    if spec.choices is not None and value not in spec.choices:
        raise WriteCallError(f"{op}: param {name!r} must be one of {spec.choices}")
    return value


# --------------------------------------------------------- the write gate --
class OperatorGate:
    """THE single write choke point. Every operator action dispatches here:
    role gate (deny-by-default) -> param coercion -> handler, with the
    principal's tenant injected as the ONLY tenant a handler ever sees ->
    audit row (applied / failed; refusals are audited too). Registration is
    build-time; the registry rejects unscoped specs and identity-shaped
    params, so an unscoped write is unwritable — the read side's 'unfiltered
    op is unwritable' rule, mirrored."""

    def __init__(self, store: PostgresFactStore):
        self._store = store
        self._specs: dict[str, WriteOperation] = {}
        self._handlers: dict[str, Callable[[Principal, dict[str, Any]],
                                           WriteOutcome]] = {}

    # ---------------------------------------------------------- registration
    def register(self, spec: WriteOperation,
                 handler: Callable[[Principal, dict[str, Any]],
                                   WriteOutcome]) -> None:
        if not _NAME.match(spec.name):
            raise WriteOperationRejected(
                f"write op name {spec.name!r} must match {_NAME.pattern}")
        for pname in spec.params:
            if not _NAME.match(pname):
                raise WriteOperationRejected(
                    f"write op {spec.name!r}: param name {pname!r} must "
                    f"match {_NAME.pattern}")
            if pname in _RESERVED_PARAMS:
                raise WriteOperationRejected(
                    f"write op {spec.name!r}: param {pname!r} is identity — "
                    f"identity comes only from the resolved principal")
        if not spec.scope:
            raise WriteOperationRejected(
                f"write op {spec.name!r}: empty role scope — deny-by-default "
                f"means every write op names who may perform it")
        if not callable(handler):
            raise WriteOperationRejected(
                f"write op {spec.name!r}: handler is not callable")
        self._specs[spec.name] = spec
        self._handlers[spec.name] = handler

    def list_for(self, principal: Principal) -> list[WriteOperation]:
        """The write catalog as THIS principal sees it: actions its roles
        allow, nothing else (absence, not 'forbidden')."""
        return [spec for _, spec in sorted(self._specs.items())
                if set(spec.scope) & set(principal.roles)]

    def operations(self) -> list[str]:
        return sorted(self._specs)

    # -------------------------------------------------------------- dispatch
    def execute(self, name: str, params: Mapping[str, Any],
                principal: Principal) -> dict[str, Any]:
        spec = self._specs.get(name)
        if spec is None:
            raise UnknownWriteOperation(name)

        given = dict(params or {})
        if not set(spec.scope) & set(principal.roles):
            # Refused attempts are audited — they are a security signal.
            self._audit(principal, name, None, given, "refused",
                        error="insufficient role")
            raise WriteRefused(name, principal)

        unknown = set(given) - set(spec.params)
        if unknown:
            raise WriteCallError(
                f"{name}: unknown param(s) {sorted(unknown)}")
        coerced = {pname: _coerce(name, pname, pspec, given.get(pname))
                   for pname, pspec in spec.params.items()}

        started = time.perf_counter()
        try:
            outcome = self._handlers[name](principal, coerced)
        except Exception as e:
            self._audit(principal, name, None, coerced, "failed",
                        error=f"{type(e).__name__}: {e}")
            raise
        audit_id = self._audit(principal, name, outcome.target, coerced,
                               "applied", snapshot_ref=outcome.snapshot_ref,
                               result=outcome.result)
        return {
            "request_id": uuid.uuid4().hex,
            "tenant_id": principal.tenant_id,
            "action": name,
            "target": outcome.target,
            "result": outcome.result,
            "snapshot_ref": outcome.snapshot_ref,
            "audit_id": audit_id,
            "applied_at": datetime.now(timezone.utc).isoformat(),
            "wall_ms": int((time.perf_counter() - started) * 1000),
        }

    # ----------------------------------------------------------------- audit
    def _audit(self, principal: Principal, action: str,
               target: Optional[str], params: dict[str, Any], outcome: str,
               *, error: Optional[str] = None,
               snapshot_ref: Optional[str] = None,
               result: Optional[dict[str, Any]] = None) -> int:
        record = OperatorAudit(
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
            roles=list(principal.roles), action=action, target=target,
            params=params, outcome=outcome, error=error,
            snapshot_ref=snapshot_ref, result=result)
        with self._store.transaction(principal.tenant_id) as conn:
            row = conn.execute(
                """
                INSERT INTO operator_audit
                    (tenant_id, principal_id, roles, action, target, params,
                     outcome, error, snapshot_ref, result)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (record.tenant_id, record.principal_id, record.roles,
                 record.action, record.target, Jsonb(record.params),
                 record.outcome, record.error, record.snapshot_ref,
                 Jsonb(record.result) if record.result is not None else None),
            ).fetchone()
        return row["id"]


# ------------------------------------------------------------ the handlers --
class OperatorService:
    """The write actions, each calling INTO existing domain logic. Every
    handler takes (principal, params) and derives its tenant from the
    principal alone — there is no other way in."""

    def __init__(self, store: PostgresFactStore,
                 resolution: ResolutionService,
                 registry: SourceRegistry,
                 secrets: Optional[SecretsProvider] = None):
        self._store = store
        self._resolution = resolution
        self._registry = registry
        self._secrets = secrets

    # ------------------------------------------------------ review resolution
    def resolve_merge(self, principal: Principal,
                      p: dict[str, Any]) -> WriteOutcome:
        tenant = principal.tenant_id
        self._resolution.decide_match(tenant, p["candidate_id"],
                                      same=p["same"],
                                      reviewer=principal.principal_id)
        merge_id = None
        if p["same"]:
            with self._store.transaction(tenant) as conn:
                row = conn.execute(
                    "SELECT id FROM entity_merges WHERE tenant_id = %s"
                    " AND triggered_by = %s ORDER BY id DESC LIMIT 1",
                    (tenant, p["candidate_id"])).fetchone()
            merge_id = row["id"] if row else None
        return WriteOutcome(
            result={"decision": "merged" if p["same"] else "kept_separate",
                    "merge_id": merge_id},
            target=f"match_candidate:{p['candidate_id']}",
            snapshot_ref=f"entity_merges:{merge_id}" if merge_id else None)

    def resolve_as_new(self, principal: Principal,
                       p: dict[str, Any]) -> WriteOutcome:
        entity_id = self._resolution.resolve_as_new(
            principal.tenant_id, p["mention_id"],
            reviewer=principal.principal_id)
        return WriteOutcome(result={"entity_id": entity_id},
                            target=f"mention:{p['mention_id']}")

    def split_merge(self, principal: Principal,
                    p: dict[str, Any]) -> WriteOutcome:
        restored = self._resolution.reverse_merge(
            principal.tenant_id, p["merge_id"],
            reversed_by=principal.principal_id)
        return WriteOutcome(
            result={"restored_entity_id": restored},
            target=f"entity_merge:{p['merge_id']}",
            snapshot_ref=f"entity_merges:{p['merge_id']}")

    def triage_quarantine(self, principal: Principal,
                          p: dict[str, Any]) -> WriteOutcome:
        tenant = principal.tenant_id
        with self._store.transaction(tenant) as conn:
            row = conn.execute(
                "SELECT status, reason, detail FROM quarantined_extractions"
                " WHERE tenant_id = %s AND id = %s",
                (tenant, p["quarantine_id"])).fetchone()
            if row is None:
                raise LookupError(
                    f"quarantined_extraction id={p['quarantine_id']} not "
                    f"found for tenant {tenant!r}")
            if row["status"] != "open":
                raise ValueError(
                    f"quarantined_extraction id={p['quarantine_id']} is "
                    f"{row['status']!r}, not open")
            conn.execute(
                "UPDATE quarantined_extractions SET status = %s"
                " WHERE tenant_id = %s AND id = %s",
                (p["decision"], tenant, p["quarantine_id"]))
        # The triage verdict is extraction feedback — a flywheel correction
        # label (one action, two records).
        self._store.insert_label(Label(
            tenant_id=tenant, label_type="correction", source="human_review",
            authority=0.9,
            payload={"quarantine_id": p["quarantine_id"],
                     "reason": row["reason"], "detail": row["detail"],
                     "decision": p["decision"], "note": p.get("note"),
                     "reviewer": principal.principal_id},
            ontology_version=self._ontology_for(tenant)))
        return WriteOutcome(
            result={"status": p["decision"], "note": p.get("note")},
            target=f"quarantine:{p['quarantine_id']}")

    def resolve_flagged_document(self, principal: Principal,
                                 p: dict[str, Any]) -> WriteOutcome:
        tenant = principal.tenant_id
        with self._store.transaction(tenant) as conn:
            doc = conn.execute(
                "SELECT review_status, raw_document_id FROM documents"
                " WHERE tenant_id = %s AND id = %s",
                (tenant, p["document_id"])).fetchone()
            if doc is None:
                raise LookupError(f"document id={p['document_id']} not found"
                                  f" for tenant {tenant!r}")
            if doc["review_status"] != "review":
                raise ValueError(f"document id={p['document_id']} is "
                                 f"{doc['review_status']!r}, not in review")
            conn.execute(
                "UPDATE documents SET review_status = 'resolved'"
                " WHERE tenant_id = %s AND id = %s",
                (tenant, p["document_id"]))
            corrected = p.get("corrected_data_track")
            if corrected:
                # The human tag wins (§8.1a): correct the CLAIM at its
                # source so re-processing sees the adjudicated track.
                conn.execute(
                    "UPDATE raw_documents SET native_metadata ="
                    " jsonb_set(COALESCE(native_metadata, '{}'::jsonb),"
                    " '{data_track}', to_jsonb(%s::text))"
                    " WHERE tenant_id = %s AND id = %s",
                    (corrected, tenant, doc["raw_document_id"]))
            # Re-queue the raw doc so the processing consumer picks the
            # adjudicated document back up (chunking was withheld).
            requeued = conn.execute(
                "UPDATE dispatch_queue SET status = 'queued',"
                " available_at = now(), claimed_at = NULL, acked_at = NULL"
                " WHERE tenant_id = %s AND raw_document_id = %s",
                (tenant, doc["raw_document_id"])).rowcount
            if not requeued:
                conn.execute(
                    "INSERT INTO dispatch_queue (tenant_id, raw_document_id)"
                    " VALUES (%s, %s)"
                    " ON CONFLICT (tenant_id, raw_document_id) DO NOTHING",
                    (tenant, doc["raw_document_id"]))
        return WriteOutcome(
            result={"review_status": "resolved", "requeued": True,
                    "corrected_data_track": p.get("corrected_data_track")},
            target=f"document:{p['document_id']}")

    # ------------------------------------------------------ ingestion control
    def pause_source(self, principal: Principal,
                     p: dict[str, Any]) -> WriteOutcome:
        tenant = principal.tenant_id
        self._require_source(tenant, p["source_ref"])
        reason = f"paused by {principal.principal_id}"
        if p.get("reason"):
            reason += f": {p['reason']}"
        self._registry.set_status(tenant, p["source_ref"], "disabled", reason)
        return WriteOutcome(result={"status": "disabled", "reason": reason},
                            target=f"source:{p['source_ref']}")

    def resume_source(self, principal: Principal,
                      p: dict[str, Any]) -> WriteOutcome:
        tenant = principal.tenant_id
        self._require_source(tenant, p["source_ref"])
        self._registry.set_status(
            tenant, p["source_ref"], "active",
            f"resumed by {principal.principal_id}")
        return WriteOutcome(result={"status": "active"},
                            target=f"source:{p['source_ref']}")

    def retry_failed_item(self, principal: Principal,
                          p: dict[str, Any]) -> WriteOutcome:
        tenant = principal.tenant_id
        table = _ALERT_QUEUES[p["queue"]]
        with self._store.transaction(tenant) as conn:
            row = conn.execute(
                f"SELECT status FROM {table} WHERE tenant_id = %s AND id = %s",
                (tenant, p["item_id"])).fetchone()
            if row is None:
                raise LookupError(f"{p['queue']} item id={p['item_id']} not "
                                  f"found for tenant {tenant!r}")
            if row["status"] == "done":
                raise ValueError(f"{p['queue']} item id={p['item_id']} is "
                                 f"done — nothing to retry")
            conn.execute(
                f"UPDATE {table} SET status = 'queued', available_at = now(),"
                f" claimed_at = NULL, acked_at = NULL,"
                f" acknowledged_at = NULL, acknowledged_by = NULL"
                f" WHERE tenant_id = %s AND id = %s",
                (tenant, p["item_id"]))
        return WriteOutcome(result={"status": "queued"},
                            target=f"{p['queue']}:{p['item_id']}")

    def acknowledge_alert(self, principal: Principal,
                          p: dict[str, Any]) -> WriteOutcome:
        tenant = principal.tenant_id
        table = _ALERT_QUEUES[p["kind"]]
        with self._store.transaction(tenant) as conn:
            row = conn.execute(
                f"SELECT last_error, status, acknowledged_at FROM {table}"
                f" WHERE tenant_id = %s AND id = %s",
                (tenant, p["item_id"])).fetchone()
            if row is None:
                raise LookupError(f"{p['kind']} item id={p['item_id']} not "
                                  f"found for tenant {tenant!r}")
            if row["last_error"] is None or row["status"] == "done":
                raise ValueError(f"{p['kind']} item id={p['item_id']} is not "
                                 f"alerting")
            if row["acknowledged_at"] is not None:
                raise ValueError(f"{p['kind']} item id={p['item_id']} was "
                                 f"already acknowledged")
            conn.execute(
                f"UPDATE {table} SET acknowledged_at = now(),"
                f" acknowledged_by = %s WHERE tenant_id = %s AND id = %s",
                (principal.principal_id, tenant, p["item_id"]))
        return WriteOutcome(
            result={"acknowledged": True, "note": p.get("note")},
            target=f"{p['kind']}:{p['item_id']}")

    # --------------------------------------------------------------- sources
    def add_source(self, principal: Principal,
                   p: dict[str, Any]) -> WriteOutcome:
        tenant = principal.tenant_id
        config = self._guard_config(p.get("config") or {})
        entry = self._registry.register(tenant, p["source_ref"],
                                        p["source_system"], config)
        return WriteOutcome(
            result={"source_ref": entry.source_ref,
                    "source_system": entry.source_system,
                    "status": entry.status,
                    "credential": self._credential_info(tenant,
                                                        entry.source_ref)},
            target=f"source:{entry.source_ref}")

    def edit_scope(self, principal: Principal,
                   p: dict[str, Any]) -> WriteOutcome:
        tenant = principal.tenant_id
        entry = self._require_source(tenant, p["source_ref"])
        config = self._guard_config(p["config"])
        # register() is the documented config upsert: refreshes config,
        # never touches health or checkpoints.
        updated = self._registry.register(tenant, entry.source_ref,
                                          entry.source_system, config)
        return WriteOutcome(
            result={"source_ref": updated.source_ref,
                    "config": updated.config},
            target=f"source:{updated.source_ref}")

    # ------------------------------------------------------------ folder jobs
    def ingest_folder(self, principal: Principal,
                      p: dict[str, Any]) -> WriteOutcome:
        """Create one folder-ingest job (d.s Stage 2). Validation is all
        HERE, server-side and deterministic — the browser cannot supply a
        real path, so the operator types one and this refuses anything
        that isn't an absolute, existing, readable directory. The job's
        ontology version is RESOLVED NOW and frozen into params: the
        runner never reads the active selection mid-run."""
        tenant = principal.tenant_id
        folder = self._require_folder(p["path"])
        include = _parse_globs("include", p.get("include"))
        exclude = _parse_globs("exclude", p.get("exclude"))

        version = p.get("ontology_version")
        if version:
            try:  # must already be imported — a typo fails at creation
                self._store.get_ontology_definition(tenant, version)
            except LookupError:
                raise WriteCallError(
                    f"ingest_folder: ontology_version {version!r} is not "
                    f"imported — import it first, or omit the param to "
                    f"use the active selection")
        else:
            version, _ = self._store.get_ontology_definition(tenant)

        # Stable per path (casefolded — Windows paths are case-insensitive),
        # so re-ingesting the same folder resumes the same source's cursor.
        source_ref = p.get("source_ref") or (
            "folder-" + hashlib.sha256(
                str(folder).casefold().encode("utf-8")).hexdigest()[:8])

        params = {"path": str(folder), "recurse": p.get("recurse", True),
                  "include": include, "exclude": exclude,
                  "ontology_version": version, "source_ref": source_ref}
        job_id = self._store.insert_job(tenant, "folder_ingest", params,
                                        created_by=principal.principal_id)
        return WriteOutcome(
            result={"job_id": job_id, **params,
                    "note": "queued — the background runner picks it up; "
                            "watch it under GET /v1/jobs"},
            target=f"job:{job_id}")

    def jobs(self, tenant: str) -> dict[str, Any]:
        """The console's job listing (read; routed like open_alerts)."""
        rows = self._store.list_jobs(tenant)
        for r in rows:
            for ts in ("created_at", "started_at", "finished_at"):
                r[ts] = r[ts].isoformat() if r[ts] else None
        return {"jobs": rows}

    @staticmethod
    def _require_folder(raw_path: Any):
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise WriteCallError("ingest_folder: path must be a non-empty "
                                 "string")
        folder = Path(raw_path.strip())
        if not folder.is_absolute():
            raise WriteCallError(
                f"ingest_folder: path must be ABSOLUTE (got {raw_path!r}) — "
                f"the folder lives on the server, so a relative path would "
                f"resolve against the service's working directory, not "
                f"yours")
        if not folder.exists():
            raise WriteCallError(
                f"ingest_folder: {str(folder)!r} does not exist on this box")
        if not folder.is_dir():
            raise WriteCallError(
                f"ingest_folder: {str(folder)!r} is not a directory")
        if not os.access(folder, os.R_OK):
            raise WriteCallError(
                f"ingest_folder: {str(folder)!r} is not readable by the "
                f"operator service account")
        try:
            next(iter(folder.iterdir()), None)  # prove listability, not just the bit
        except OSError as e:
            raise WriteCallError(
                f"ingest_folder: cannot list {str(folder)!r} "
                f"({type(e).__name__})")
        return folder.resolve()

    # --------------------------------------------------------------- ontology
    def import_ontology(self, principal: Principal,
                        p: dict[str, Any]) -> WriteOutcome:
        """Validate + load one ontology set (d.s Stage 1): the deterministic
        gate (ontology_registry) first, then BOTH forms — the portable
        <version>.json in the git-tracked folder, and the ontology_versions
        row. Import is INERT: nothing extracts under this version until the
        operator separately selects it."""
        tenant = principal.tenant_id
        try:
            onto = validate_ontology_set(p["ontology"])
            path = save_ontology_file(onto)
        except OntologyValidationError as e:
            raise WriteCallError(f"import_ontology: {e}")
        status = self._store.insert_ontology_version(
            tenant, onto.version, onto.definition,
            notes=onto.notes or "imported via operator console")
        return WriteOutcome(
            result={"version": onto.version,
                    "status": status,     # created | already_imported
                    "entity_types": len(onto.definition["entity_types"]),
                    "predicates": len(onto.definition["predicates"]),
                    "file": str(path),
                    "active": False if status == "created" else None,
                    "note": "imported, NOT active — select it to apply to "
                            "future ingests"},
            target=f"ontology:{onto.version}")

    def select_ontology(self, principal: Principal,
                        p: dict[str, Any]) -> WriteOutcome:
        """Point the single active-ontology row (migration 011) at an
        imported version. FUTURE ingests only: facts keep the version that
        extracted them (true provenance); re-extracting existing data is a
        separate, scoped, deliberate action (Stage 3)."""
        tenant = principal.tenant_id
        self._store.set_active_ontology(
            tenant, p["version"], activated_by=principal.principal_id)
        return WriteOutcome(
            result={"active_version": p["version"],
                    "applies_to": "future ingests only — existing facts "
                                  "keep the ontology version that "
                                  "extracted them"},
            target=f"ontology:{p['version']}")

    def list_ontologies(self, tenant: str) -> dict[str, Any]:
        """The console's ontology listing (read; routed like open_alerts)."""
        rows = self._store.list_ontology_versions(tenant)
        active = next((r["version"] for r in rows if r["active"]), None)
        return {"active": active,
                "versions": [{**r, "effective_from":
                              r["effective_from"].isoformat()
                              if r["effective_from"] else None}
                             for r in rows]}

    # ---------------------------------------------------------------- helpers
    def _require_source(self, tenant: str, source_ref: str):
        entry = self._registry.get(tenant, source_ref)
        if entry is None:
            raise LookupError(
                f"source {source_ref!r} not registered for tenant {tenant!r}")
        return entry

    @staticmethod
    def _guard_config(config: dict[str, Any]) -> dict[str, Any]:
        bad = sorted(k for k in config if _CREDENTIAL_KEY.search(k))
        if bad:
            raise WriteCallError(
                f"config key(s) {bad} look like credentials — secrets travel "
                f"the vault flow (tenants/<tenant>/sources/<ref>), never "
                f"this API")
        return config

    def _credential_info(self, tenant: str,
                         source_ref: str) -> dict[str, Any]:
        """Where the credential BELONGS and whether one is present — never
        what it is. The API triggers/points at the vault flow; the secret
        value never transits here."""
        if self._secrets is None:
            return {"vault_path": None, "present": None}
        path = self._secrets.path_for(tenant, source_ref)
        try:
            self._secrets.get_secret(tenant, source_ref)
            present = True
        except SecretNotFound:
            present = False
        except Exception:
            present = None       # vault unreachable ≠ absent; stay honest
        return {"vault_path": path, "present": present}

    def _ontology_for(self, tenant: str) -> str:
        """Deliberately UNCACHED (d.s Stage 1): flywheel labels must stamp
        the version that is active at decision time, and this process lives
        for weeks — the old first-use cache would survive an operator swap
        and stamp a retired version forever."""
        version, _ = self._store.get_ontology_definition(tenant)
        return version

    # ----------------------------------------------------------------- reads
    def open_alerts(self, tenant: str) -> list[dict[str, Any]]:
        with self._store.transaction(tenant) as conn:
            rows = conn.execute(
                "SELECT kind, ref_id, detail, created_at FROM operator_alerts"
                " WHERE tenant_id = %s ORDER BY created_at, kind, ref_id",
                (tenant,)).fetchall()
        return [{**r, "created_at": r["created_at"].isoformat()
                 if r["created_at"] else None} for r in rows]

    def ping_postgres(self) -> bool:
        try:
            with self._store.transaction("_warmup") as conn:
                conn.execute("SELECT 1 FROM operator_audit LIMIT 0")
            return True
        except Exception:
            return False


# ------------------------------------------------- the standard write surface
def register_operator_defaults(gate: OperatorGate,
                               service: OperatorService) -> None:
    """Build-time registration of the v1 write surface. Adding an action is
    a spec+handler registration here + redeploy — the endpoint set follows
    the registry, exactly like the read side."""
    P = WriteParamSpec
    for spec, handler in [
        (WriteOperation(
            name="resolve_merge",
            description="Human verdict on a review-band match pair: merge"
                        " (reversible, snapshotted to entity_merges) or keep"
                        " separate. Writes the human_review flywheel label.",
            params={"candidate_id": P(type="int", required=True),
                    "same": P(type="bool", required=True)},
            scope=REVIEW_SCOPE), service.resolve_merge),
        (WriteOperation(
            name="resolve_as_new",
            description="Human verdict: none of the candidates match — every"
                        " open review pair becomes a labeled hard negative"
                        " and the mention becomes a new entity.",
            params={"mention_id": P(type="int", required=True)},
            scope=REVIEW_SCOPE), service.resolve_as_new),
        (WriteOperation(
            name="split_merge",
            description="Reverse a prior merge from its snapshot: restores"
                        " the absorbed entity under its original id,"
                        " repoints the dependent facts back, re-resolves its"
                        " mentions, writes the er_nonmatch reversal label.",
            params={"merge_id": P(type="int", required=True)},
            scope=REVIEW_SCOPE), service.split_merge),
        (WriteOperation(
            name="triage_quarantine",
            description="Adjudicate one quarantined extraction: resolved or"
                        " dismissed. The verdict lands as a 'correction'"
                        " flywheel label (extraction feedback).",
            params={"quarantine_id": P(type="int", required=True),
                    "decision": P(type="str", required=True,
                                  choices=["resolved", "dismissed"]),
                    "note": P(type="str")},
            scope=REVIEW_SCOPE), service.triage_quarantine),
        (WriteOperation(
            name="resolve_flagged_document",
            description="Adjudicate a data-track mismatch (§8.1a): the human"
                        " tag wins, optionally corrected at the source claim,"
                        " and the document re-queues for processing.",
            params={"document_id": P(type="int", required=True),
                    "corrected_data_track": P(type="str"),
                    "note": P(type="str")},
            scope=REVIEW_SCOPE), service.resolve_flagged_document),
        (WriteOperation(
            name="pause_source",
            description="Disable one source in the registry — capture skips"
                        " disabled sources, so the pause is real.",
            params={"source_ref": P(type="str", required=True),
                    "reason": P(type="str")},
            scope=OPERATE_SCOPE), service.pause_source),
        (WriteOperation(
            name="resume_source",
            description="Re-enable a paused/degraded source.",
            params={"source_ref": P(type="str", required=True)},
            scope=OPERATE_SCOPE), service.resume_source),
        (WriteOperation(
            name="retry_failed_item",
            description="Requeue one failed outbox item (dispatch or"
                        " extraction); clears its alert acknowledgement.",
            params={"queue": P(type="str", required=True,
                               choices=sorted(_ALERT_QUEUES)),
                    "item_id": P(type="int", required=True)},
            scope=OPERATE_SCOPE), service.retry_failed_item),
        (WriteOperation(
            name="acknowledge_alert",
            description="Mark one failed queue item as seen — it leaves the"
                        " operator_alerts view without being retried.",
            params={"kind": P(type="str", required=True,
                              choices=sorted(_ALERT_QUEUES)),
                    "item_id": P(type="int", required=True),
                    "note": P(type="str")},
            scope=OPERATE_SCOPE), service.acknowledge_alert),
        (WriteOperation(
            name="add_source",
            description="Register a source (adapter config only — the"
                        " response points at the OpenBao path where the"
                        " credential belongs and whether one is present;"
                        " the secret value NEVER transits this API).",
            params={"source_ref": P(type="str", required=True),
                    "source_system": P(type="str", required=True),
                    "config": P(type="dict")},
            scope=OPERATE_SCOPE), service.add_source),
        (WriteOperation(
            name="edit_scope",
            description="Replace a registered source's adapter config"
                        " (credential-shaped keys refused; health and"
                        " checkpoints untouched).",
            params={"source_ref": P(type="str", required=True),
                    "config": P(type="dict", required=True)},
            scope=OPERATE_SCOPE), service.edit_scope),
        (WriteOperation(
            name="ingest_folder",
            description="Ingest a local folder on the server (d.s Stage 2):"
                        " typed absolute path, validated server-side;"
                        " optional recurse/include/exclude and an ontology"
                        " version (defaults to the active selection, FIXED"
                        " at creation). Creates a background job — eligible"
                        " files land content-hash idempotently, unknown"
                        " types are skipped and counted, extraction runs"
                        " against the stored copy.",
            params={"path": P(type="str", required=True),
                    "recurse": P(type="bool", default=True),
                    "include": P(type="str"),
                    "exclude": P(type="str"),
                    "ontology_version": P(type="str"),
                    "source_ref": P(type="str")},
            scope=OPERATE_SCOPE), service.ingest_folder),
        (WriteOperation(
            name="import_ontology",
            description="Validate and load one ontology set (version + the"
                        " two allowlists, optional examples/aliases) into"
                        " the git-tracked folder AND the registry."
                        " Deterministic validation, specific errors."
                        " Importing is inert — nothing extracts under the"
                        " new version until it is selected.",
            params={"ontology": P(type="dict", required=True)},
            scope=OPERATE_SCOPE), service.import_ontology),
        (WriteOperation(
            name="select_ontology",
            description="Make an imported ontology version the active one —"
                        " the single selection every future ingest extracts"
                        " under. Future ingests ONLY: existing facts keep"
                        " the version that extracted them.",
            params={"version": P(type="str", required=True)},
            scope=OPERATE_SCOPE), service.select_ontology),
    ]:
        gate.register(spec, handler)


# ------------------------------------------------------------------- the app --
class OperatorApp:
    """The HTTP core, framework-free like the read side: handle() maps
    (method, path, headers, body) -> (status, JSON body); the socket layer
    is service_http's thin adapter (make_server duck-types on .handle)."""

    def __init__(self, gate: OperatorGate, service: OperatorService,
                 resolver: CredentialResolver, *,
                 reads: Optional[OperatorReadService] = None,
                 stats: Optional[LatencyStats] = None):
        self.gate = gate
        self.service = service
        self.reads = reads
        self._resolver = resolver
        self.stats = stats or LatencyStats()
        # EVERY store-touching request is serialized — writes AND reads.
        # The store is ONE psycopg connection, and psycopg transaction
        # contexts are not thread-safe on a shared connection: two
        # concurrent requests interleave BEGIN/SAVEPOINT frames and strand
        # the connection idle-in-transaction, after which nothing commits.
        # (Found live: the UI polls monitor+activity+reviews concurrently.)
        # Operator traffic is UI-click + 5s-poll volume; one-at-a-time is
        # invisible here and keeps the audit trail strictly ordered.
        self._store_lock = threading.Lock()

    def endpoints(self) -> list[str]:
        """The registry spelled as URLs — one POST per registered write op
        plus the fixed operational surface. Nothing else answers."""
        return sorted(f"POST /v1/actions/{name}"
                      for name in self.gate.operations()) + [
            "GET /v1/actions",
            "GET /v1/alerts",
            "GET /v1/jobs",
            "GET /v1/monitor",
            "GET /v1/monitor/activity",
            "GET /v1/ontology",
            "GET /v1/passages/<chunk_id>",
            "GET /v1/reviews",
            "GET /v1/reviews/<kind:id>",
            "GET /v1/health",
            "GET /v1/metrics",
            "GET /ui/",
        ]

    # -------------------------------------------------------------- dispatch
    def handle(self, method: str, path: str, headers: Mapping[str, Any],
               body: bytes) -> tuple[int, dict[str, Any]]:
        try:
            return self._route(method, path, headers, body)
        except Exception:
            return 500, {"error": "internal"}

    def _route(self, method: str, raw_path: str, headers: Mapping[str, Any],
               body: bytes) -> tuple[int, Any]:
        if method == "GET" and (raw_path in ("/", "/ui")
                                or raw_path.startswith("/ui/")):
            # Static UI shell only — it renders nothing until the operator
            # authenticates; every data byte still travels the gated APIs.
            # The RAW path is used: '/ui/' (index) and '/ui' (redirect so
            # relative asset URLs resolve) are different answers.
            return self._static(raw_path)
        path = raw_path.rstrip("/") or "/"
        if method == "GET" and path == "/v1/health":
            return self._health()
        if method == "GET" and path == "/v1/metrics":
            return 200, self.stats.snapshot()

        try:
            principal = self._principal(headers)
        except PrincipalUnresolvable:
            return 401, {"error": "unauthorized"}

        if method == "GET" and (
                path in ("/v1/monitor", "/v1/monitor/activity",
                         "/v1/reviews")
                or path.startswith("/v1/reviews/")
                or path.startswith("/v1/passages/")):
            return self._read(path, principal)

        if method == "GET" and path == "/v1/actions":
            if not {ROLE_REVIEWER, ROLE_OPERATOR} & set(principal.roles):
                # F3: this is the console's login check — a resolvable
                # principal with no console role is almost always the AGENT
                # serving credential (printed back-to-back with the operator
                # one at bootstrap). Accepting it used to unlock a console
                # where every read 403s silently: a blank dashboard under
                # "SYSTEM : NOMINAL".
                return 403, {
                    "error": "forbidden",
                    "detail": "this credential is valid but has no console "
                              "role — it is an AGENT serving credential; "
                              "log in with the OPERATOR CONSOLE credential "
                              "(issue one: khctl provision-operator)"}
            return 200, {"tenant_id": principal.tenant_id,
                         "actions": [spec.model_dump(mode="json")
                                     for spec in
                                     self.gate.list_for(principal)]}
        if method == "GET" and path == "/v1/alerts":
            if not {ROLE_REVIEWER, ROLE_OPERATOR} & set(principal.roles):
                return 403, {"error": "forbidden"}
            with self._store_lock:
                alerts = self.service.open_alerts(principal.tenant_id)
            return 200, {"tenant_id": principal.tenant_id, "alerts": alerts}
        if method == "GET" and path == "/v1/ontology":
            # Routed like alerts: a service read, role-gated, store-locked.
            if not {ROLE_REVIEWER, ROLE_OPERATOR} & set(principal.roles):
                return 403, {"error": "forbidden"}
            with self._store_lock:
                listing = self.service.list_ontologies(principal.tenant_id)
            return 200, {"tenant_id": principal.tenant_id, **listing}
        if method == "GET" and path == "/v1/jobs":
            if not {ROLE_REVIEWER, ROLE_OPERATOR} & set(principal.roles):
                return 403, {"error": "forbidden"}
            with self._store_lock:
                jobs = self.service.jobs(principal.tenant_id)
            return 200, {"tenant_id": principal.tenant_id, **jobs}
        if method == "POST" and path.startswith("/v1/actions/"):
            name = path[len("/v1/actions/"):]
            if "/" in name or not name:
                return 404, {"error": "not_found"}
            return self._execute(name, body, principal)
        return 404, {"error": "not_found"}

    def _execute(self, name: str, body: bytes,
                 principal: Principal) -> tuple[int, dict[str, Any]]:
        started = time.perf_counter()
        status, payload = 500, {"error": "internal"}
        try:
            params = _json_object(body)
        except (ValueError, UnicodeDecodeError) as e:
            return 400, {"error": "bad_request", "detail": str(e)}
        try:
            with self._store_lock:
                payload = self.gate.execute(name, params, principal)
            status = 200
        except UnknownWriteOperation:
            status, payload = 404, {"error": "unknown_action", "action": name}
        except WriteRefused:
            status, payload = 403, {"error": "forbidden"}
        except WriteCallError as e:
            status, payload = 400, {"error": "bad_request", "detail": str(e)}
        except LookupError:
            # Cross-tenant targets land here too: the row does not exist in
            # the principal's tenant — absence, never description.
            status, payload = 404, {"error": "not_found"}
        except ValueError as e:
            # The target exists but is not in an actionable state.
            status, payload = 409, {"error": "conflict", "detail": str(e)}
        finally:
            self.stats.record(f"action:{name}",
                              (time.perf_counter() - started) * 1000,
                              error=status >= 400)
        return status, payload

    # ----------------------------------------------------------------- reads
    def _read(self, path: str,
              principal: Principal) -> tuple[int, dict[str, Any]]:
        """The operator READ surface (BP20 Part A): same identity + role +
        tenant enforcement as the writes, structurally read-only — this
        method dispatches to OperatorReadService, which issues SELECTs and
        holds no write path. Reads are not audited (audit is the write
        trail) and not write-locked."""
        if self.reads is None:
            return 404, {"error": "not_found"}
        if not {ROLE_REVIEWER, ROLE_OPERATOR} & set(principal.roles):
            return 403, {"error": "forbidden"}
        tenant = principal.tenant_id
        started = time.perf_counter()
        endpoint = "read:" + path.split("/")[2]
        status, payload = 500, {"error": "internal"}
        try:
            with self._store_lock:
                if path == "/v1/monitor":
                    status, payload = 200, self.reads.monitor(tenant)
                elif path == "/v1/monitor/activity":
                    status, payload = 200, {
                        "tenant_id": tenant,
                        "events": self.reads.activity(tenant)}
                elif path == "/v1/reviews":
                    status, payload = 200, self.reads.reviews(tenant)
                elif path.startswith("/v1/passages/"):
                    # F18: fact -> evidence. A served FactEnvelope carries
                    # document_id/chunk_id; this dereferences the chunk to
                    # its passage + document title through a proper door —
                    # "where did this fact come from?" no longer dead-ends
                    # at numeric IDs (or psql).
                    raw_id = path[len("/v1/passages/"):]
                    passage = (self.reads.passage(tenant, int(raw_id))
                               if raw_id.isdigit() else None)
                    if passage is None:
                        status, payload = 404, {"error": "not_found"}
                    else:
                        status, payload = 200, passage
                else:
                    ref = path[len("/v1/reviews/"):]
                    kind, _, raw_id = ref.partition(":")
                    detail = (self.reads.review_detail(tenant, kind,
                                                       int(raw_id))
                              if raw_id.isdigit() else None)
                    if detail is None:
                        # Unknown kind, malformed id, or another tenant's
                        # item — one answer for all of them (absence).
                        status, payload = 404, {"error": "not_found"}
                    else:
                        status, payload = 200, detail
        finally:
            self.stats.record(endpoint,
                              (time.perf_counter() - started) * 1000,
                              error=status >= 400)
        return status, payload

    # ------------------------------------------------------------- static UI
    @staticmethod
    def _static(raw_path: str) -> tuple[int, Any]:
        if raw_path in ("/", "/ui"):
            return 302, RawResponse(
                "text/html; charset=utf-8",
                b'<meta http-equiv="refresh" content="0; url=/ui/">')
        name = raw_path[len("/ui/"):] or "index.html"
        # Flat directory, allowlisted extensions, no traversal to resolve.
        if "/" in name or "\\" in name or ".." in name:
            return 404, {"error": "not_found"}
        target = _UI_DIR / name
        suffix = target.suffix.lower()
        if suffix not in _UI_TYPES or not target.is_file():
            return 404, {"error": "not_found"}
        return 200, RawResponse(_UI_TYPES[suffix], target.read_bytes())

    # ---------------------------------------------------------------- pieces
    def _principal(self, headers: Mapping[str, Any]) -> Principal:
        auth = headers.get("Authorization") or headers.get("authorization")
        if not isinstance(auth, str) or not auth.startswith("Bearer "):
            raise PrincipalUnresolvable("no bearer credential presented")
        return self._resolver.resolve_principal(auth[len("Bearer "):].strip())

    def _health(self) -> tuple[int, dict[str, Any]]:
        with self._store_lock:
            postgres_ok = self.service.ping_postgres()
        # F1: sealed ≠ unreachable ≠ ok. `vault` stays a bool (sealed =
        # False — a sealed vault refuses every credential); `vault_status`
        # carries the distinction the lock screen branches on.
        status_fn = getattr(self._resolver, "status", None)
        if callable(status_fn):
            vault_status = status_fn()
        else:
            ping = getattr(self._resolver, "ping", None)
            vault_status = ("ok" if (bool(ping()) if callable(ping)
                                     else True) else "unreachable")
        vault_ok = vault_status == "ok"
        ok = postgres_ok and vault_ok
        return (200 if ok else 503), {
            "status": "ok" if ok else "degraded",
            "version": installed_version(),
            "postgres": postgres_ok,
            "vault": vault_ok,
            "vault_status": vault_status,
            "actions": len(self.gate.operations()),
        }


def _json_object(body: bytes) -> dict[str, Any]:
    if not body:
        return {}
    parsed = json.loads(body.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("request body must be a JSON object")
    return parsed


# ------------------------------------------------------------------ assembly --
def build_operator_app(*, dsn: Optional[str] = None,
                       resolver: Optional[CredentialResolver] = None,
                       embedder=None,
                       secrets: Optional[SecretsProvider] = None,
                       store: Optional[PostgresFactStore] = None
                       ) -> OperatorApp:
    """Assemble the operator write service. Tenancy-parameterized like the
    read side: the DSN is an input, the tenant is always the principal's.
    The store here is WRITE-CAPABLE (the internal-path PostgresFactStore) —
    which is exactly why this is a separate service from the read boundary,
    whose connection stays read-only and untouched."""
    if embedder is None:
        from knowledge_hub.embedding_ollama import OllamaEmbedder
        embedder = OllamaEmbedder()
    if secrets is None:
        from knowledge_hub.secrets_openbao import OpenBaoSecretsProvider
        secrets = OpenBaoSecretsProvider()
    store = store or PostgresFactStore(dsn=dsn)
    pipeline = Pipeline(store=store)
    from knowledge_hub.scoring_tiered import TieredScorer
    resolution = ResolutionService(pipeline, TieredScorer(store), embedder)
    service = OperatorService(store, resolution, SourceRegistry(store),
                              secrets)
    gate = OperatorGate(store)
    register_operator_defaults(gate, service)
    resolver = resolver or OpenBaoCredentialResolver()
    return OperatorApp(gate, service, resolver,
                       reads=OperatorReadService(store))


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        description="Knowledge Hub operator write API — the write-twin")
    parser.add_argument("--host", default=settings.operator_host)
    parser.add_argument("--port", type=int, default=settings.operator_port)
    parser.add_argument("--dsn", default=None)
    parser.add_argument("--no-worker", action="store_true",
                        help="serve the API without the background job "
                             "runner (jobs queue up untouched)")
    args = parser.parse_args(argv)

    app = build_operator_app(dsn=args.dsn)
    if not app.service.ping_postgres():
        raise SystemExit("operator store failed to answer (is migration 010 "
                         "applied?) — refusing to start blind")
    runner = None
    if not args.no_worker:
        # The job runner rides in this process on its OWN store connection
        # (operator_jobs.py explains why it must not share the app's) —
        # console up = jobs run, no separate service to babysit.
        from knowledge_hub.operator_jobs import JobRunner
        runner = JobRunner(dsn=args.dsn)
        runner.start()
    server = make_server(app, args.host, args.port)
    print(f"knowledge_hub operator API {installed_version()} on "
          f"http://{args.host}:{args.port} — "
          f"{len(app.gate.operations())} write actions, every one audited"
          + ("" if runner else " — JOB RUNNER OFF (--no-worker)"))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        if runner:
            runner.stop()
        server.shutdown()


if __name__ == "__main__":
    main()
