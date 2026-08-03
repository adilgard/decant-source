"""Capture flow (Build Prompt 2): SourceRegistry + CaptureService.

CaptureService is the landing loop that connects the capture seams to Prompt
1's persistence, per item:

    adapter yields SourceItem
      -> sha256(content)
      -> RawStore.exists(tenant, hash)          idempotency probe (no re-put)
      -> RawStore.put(...)                      write-once, WORM, version-pinned
      -> Pipeline.ingest_raw(RawDocument)       _next_version + _persist_raw
      -> Dispatcher.dispatch(tenant, raw_id)    reference-only, idempotent
      -> SourceRegistry checkpoint(item.cursor) resume point advances

Idempotency and versioning are Prompt 1's `_persist_raw`/`_next_version` —
this module never re-implements them. Dispatch happens even when the row
already existed: a crash between land and dispatch resumes into the same
item, and the dispatcher's idempotent enqueue makes the retry safe (that
combination is what makes the handoff at-least-once end to end).

Failure containment: a SecretsError — while preparing a source OR mid-pull
(a credential rotated to invalid partway through) — marks THAT source
degraded in the registry and returns; the tenant's other sources are
untouched. A CursorInvalid from the adapter (expired delta token) triggers a
RESYNC: checkpoints cleared, one automatic re-run as backfill (safe — landing
is idempotent, changed items version up). Any other mid-pull error re-raises
after recording status_reason; the per-item checkpoint already taken means a
re-run resumes, not restarts.

Tombstones (§8.1g): a tombstone item soft-deletes the logical document —
deleted_at stamped on every version row; bytes stay in the WORM store
(retention owns their physical fate) and a later re-appearance revives the
rows (an observed upsert outranks any earlier delete). Because even an
"explicit" delete signal can be a scope artifact (a folder moved out of the
app's reach arrives as removals), a storm guard halts the run for review
when one run tries to tombstone more than max(storm_min, storm_fraction ×
corpus) documents — re-run with allow_mass_tombstone=True to apply
intentionally.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any, Iterator, Literal, Optional

from pydantic import BaseModel, Field

from knowledge_hub.factstore_pg import PostgresFactStore
from knowledge_hub.interfaces import (
    CursorInvalid,
    Dispatcher,
    RawStore,
    SecretsError,
    SecretsProvider,
    SourceAdapter,
    SourceItem,
)
from knowledge_hub.models import RawDocument, SourceRegistryEntry
from knowledge_hub.pipeline import Pipeline

logger = logging.getLogger(__name__)

Mode = Literal["auto", "backfill", "incremental"]


class TombstoneStorm(Exception):
    """One run tried to soft-delete more documents than the storm threshold
    allows (§8.1g: a permission/filter/scope change masquerades as a mass
    delete). The run halts UN-checkpointed at the tripping item, so a re-run
    re-sees it; nothing already applied is lost (deleted_at is reversible).
    Re-run with allow_mass_tombstone=True to apply deliberately."""


class CaptureRunResult(BaseModel):
    """Summary of one run of one source (returned, and safe to log)."""
    tenant_id: str
    source_ref: str
    mode: str
    status: str = "ok"            # ok|degraded|skipped
    reason: Optional[str] = None
    landed: int = 0               # new raw_documents rows
    replayed: int = 0             # items whose bytes had already landed
    dispatched: int = 0
    tombstoned: int = 0           # logical docs soft-deleted this run
    source_stats: dict[str, Any] = Field(default_factory=dict)  # adapter.stats()


class SourceRegistry:
    """CRUD + checkpointing over source_registry (migration 002)."""

    def __init__(self, store: PostgresFactStore):
        self._store = store

    def register(self, tenant_id: str, source_ref: str, source_system: str,
                 config: Optional[dict] = None) -> SourceRegistryEntry:
        """Idempotent upsert; re-registering refreshes config but never
        touches health or checkpoints."""
        from psycopg.types.json import Jsonb
        with self._store.transaction(tenant_id) as conn:
            row = conn.execute(
                """
                INSERT INTO source_registry (tenant_id, source_ref, source_system, config)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (tenant_id, source_ref) DO UPDATE
                    SET config = EXCLUDED.config, updated_at = now()
                RETURNING *
                """,
                (tenant_id, source_ref, source_system, Jsonb(config or {})),
            ).fetchone()
        return SourceRegistryEntry(**row)

    def list_for_tenant(self, tenant_id: str) -> list[SourceRegistryEntry]:
        """Every registered source for a tenant, source_ref order — the
        Data Ingestion program (khctl ingest) sweeps exactly this list."""
        with self._store.transaction(tenant_id) as conn:
            rows = conn.execute(
                "SELECT * FROM source_registry WHERE tenant_id = %s"
                " ORDER BY source_ref",
                (tenant_id,),
            ).fetchall()
        return [SourceRegistryEntry(**row) for row in rows]

    def get(self, tenant_id: str, source_ref: str) -> Optional[SourceRegistryEntry]:
        with self._store.transaction(tenant_id) as conn:
            row = conn.execute(
                "SELECT * FROM source_registry"
                " WHERE tenant_id = %s AND source_ref = %s",
                (tenant_id, source_ref),
            ).fetchone()
        return SourceRegistryEntry(**row) if row else None

    def set_status(self, tenant_id: str, source_ref: str, status: str,
                   reason: Optional[str] = None) -> None:
        self._update(tenant_id, source_ref,
                     "status = %s, status_reason = %s", (status, reason))

    def note(self, tenant_id: str, source_ref: str, reason: str) -> None:
        """Record why the last run stopped, without changing health status."""
        self._update(tenant_id, source_ref, "status_reason = %s", (reason,))

    def checkpoint(self, tenant_id: str, source_ref: str, *,
                   cursor: Optional[str] = None,
                   backfill_cursor: Optional[str] = None,
                   ordered: bool = True) -> None:
        """Advance resume points. Ordered cursors are defended monotonically
        (GREATEST — a stale writer can never move one backwards); opaque
        cursors (ordered=False; Graph delta links, serialized state) are
        stored verbatim, last-write-wins — safe because the adapter is the
        checkpoint's only writer and yields in sweep order."""
        sets, params = [], []
        if cursor is not None:
            sets.append("cursor = GREATEST(COALESCE(cursor, ''), %s)"
                        if ordered else "cursor = %s")
            params.append(cursor)
        if backfill_cursor is not None:
            sets.append("backfill_cursor = GREATEST(COALESCE(backfill_cursor, ''), %s)"
                        if ordered else "backfill_cursor = %s")
            params.append(backfill_cursor)
        if sets:
            self._update(tenant_id, source_ref, ", ".join(sets), tuple(params))

    def reset_for_resync(self, tenant_id: str, source_ref: str,
                         reason: str) -> None:
        """Clear every checkpoint so the next run re-backfills — the cursor
        died at the source (expired delta token). Safe end to end: re-landing
        is a content-hash no-op and changed items version up."""
        self._update(tenant_id, source_ref,
                     "cursor = NULL, backfill_cursor = NULL,"
                     " backfill_done = false, status_reason = %s", (reason,))

    def finish_backfill(self, tenant_id: str, source_ref: str) -> None:
        self._update(tenant_id, source_ref,
                     "backfill_done = true, backfill_cursor = NULL,"
                     " last_run_at = now()", ())

    def finish_incremental(self, tenant_id: str, source_ref: str) -> None:
        self._update(tenant_id, source_ref, "last_run_at = now()", ())

    def _update(self, tenant_id: str, source_ref: str,
                set_sql: str, params: tuple) -> None:
        with self._store.transaction(tenant_id) as conn:
            updated = conn.execute(
                f"UPDATE source_registry SET {set_sql}, updated_at = now()"
                " WHERE tenant_id = %s AND source_ref = %s",
                (*params, tenant_id, source_ref),
            ).rowcount
        if updated == 0:
            raise LookupError(
                f"source {source_ref!r} not registered for tenant {tenant_id!r}")


class CaptureService:
    def __init__(self, pipeline: Pipeline, raw_store: RawStore,
                 dispatcher: Dispatcher,
                 secrets: Optional[SecretsProvider] = None,
                 tombstone_storm_min: int = 10,
                 tombstone_storm_fraction: float = 0.10):
        self.pipeline = pipeline
        self.raw_store = raw_store
        self.dispatcher = dispatcher
        self.secrets = secrets
        self.registry = SourceRegistry(pipeline.store)
        # §8.1g storm guard: one run may tombstone at most
        # max(storm_min, storm_fraction × live corpus) documents.
        self.tombstone_storm_min = tombstone_storm_min
        self.tombstone_storm_fraction = tombstone_storm_fraction

    # ----------------------------------------------------------------- runs --
    def run_source(self, tenant_id: str, adapter: SourceAdapter,
                   mode: Mode = "auto",
                   allow_mass_tombstone: bool = False) -> CaptureRunResult:
        """Pull one source to a landed, dispatched state. Registers the source
        on first sight; resumes from registry checkpoints on every run."""
        entry = self.registry.get(tenant_id, adapter.source_ref)
        if entry is None:
            entry = self.registry.register(
                tenant_id, adapter.source_ref, adapter.source_system)
        if entry.status == "disabled":
            return CaptureRunResult(
                tenant_id=tenant_id, source_ref=adapter.source_ref, mode=mode,
                status="skipped", reason="source disabled in registry")

        try:
            adapter.prepare(tenant_id, self.secrets)
        except SecretsError as e:
            # Degrade THIS source; the tenant's other sources are unaffected.
            # str(e) carries tenant/source/path — never secret values.
            return self._degrade(tenant_id, adapter, mode, e)

        # Two passes at most: the second only after a CursorInvalid resync.
        for attempt in ("run", "resync"):
            entry = self.registry.get(tenant_id, adapter.source_ref)
            run_mode: str = mode
            if run_mode == "auto":
                run_mode = "incremental" if entry.backfill_done else "backfill"
            if run_mode == "backfill":
                items = adapter.backfill(tenant_id,
                                         resume_after=entry.backfill_cursor)
            else:
                items = adapter.incremental(tenant_id, cursor=entry.cursor)

            result = CaptureRunResult(
                tenant_id=tenant_id, source_ref=adapter.source_ref,
                mode=run_mode)
            try:
                self._land_all(tenant_id, adapter, items, run_mode, result,
                               allow_mass_tombstone)
            except CursorInvalid as e:
                if attempt == "resync" or run_mode == "backfill":
                    # A backfill needs no cursor — a CursorInvalid here (or
                    # twice in a row) is an adapter bug, not an expiry.
                    self.registry.note(tenant_id, adapter.source_ref,
                                       f"interrupted: {type(e).__name__}: {e}")
                    raise
                self.registry.reset_for_resync(tenant_id, adapter.source_ref,
                                               f"resync: {e}")
                logger.warning("capture: %s — resyncing via backfill", e)
                mode = "backfill"
                continue
            except SecretsError as e:
                # Mid-pull credential failure (rotated/revoked partway):
                # same containment as prepare() — degrade THIS source only.
                # Checkpoints already persisted mean the next good run
                # resumes, not restarts.
                return self._degrade(tenant_id, adapter, run_mode, e,
                                     result=result)
            except TombstoneStorm as e:
                logger.warning("capture: %s", e)
                return self._degrade(tenant_id, adapter, run_mode, e,
                                     result=result)
            except Exception as e:
                # Checkpoints for every completed item are already persisted —
                # a re-run resumes after the last landed item.
                self.registry.note(tenant_id, adapter.source_ref,
                                   f"interrupted: {type(e).__name__}: {e}")
                raise

            ordered = adapter.cursor_ordering == "ordered"
            final = adapter.final_cursor()
            if final is not None:
                # End-of-sweep high-water mark (deltaLink): persisted even for
                # a zero-item sweep, so the token advances instead of aging.
                self.registry.checkpoint(tenant_id, adapter.source_ref,
                                         cursor=final, ordered=ordered)
            if run_mode == "backfill":
                self.registry.finish_backfill(tenant_id, adapter.source_ref)
            else:
                self.registry.finish_incremental(tenant_id, adapter.source_ref)
            if entry.status == "degraded":
                self.registry.set_status(tenant_id, adapter.source_ref, "active")
            result.source_stats = adapter.stats() or {}
            return result

    def _degrade(self, tenant_id: str, adapter: SourceAdapter, mode: str,
                 error: Exception,
                 result: Optional[CaptureRunResult] = None) -> CaptureRunResult:
        self.registry.set_status(tenant_id, adapter.source_ref,
                                 "degraded", str(error))
        logger.warning("capture: source degraded: %s", error)
        if result is None:
            result = CaptureRunResult(
                tenant_id=tenant_id, source_ref=adapter.source_ref, mode=mode)
        result.status = "degraded"
        result.reason = str(error)
        result.source_stats = adapter.stats() or {}
        return result

    def _land_all(self, tenant_id: str, adapter: SourceAdapter,
                  items: Iterator[SourceItem], mode: str,
                  result: CaptureRunResult,
                  allow_mass_tombstone: bool = False) -> None:
        ordered = adapter.cursor_ordering == "ordered"
        storm_limit: Optional[int] = None  # computed on first tombstone
        for item in items:
            if item.change == "tombstone":
                if storm_limit is None:
                    corpus = self._corpus_count(tenant_id, adapter.source_system)
                    storm_limit = max(
                        self.tombstone_storm_min,
                        int(corpus * self.tombstone_storm_fraction))
                if (not allow_mass_tombstone
                        and result.tombstoned + 1 > storm_limit):
                    # Halt BEFORE applying or checkpointing this item: a
                    # re-run re-sees it, nothing is lost (at-least-once).
                    raise TombstoneStorm(
                        f"tombstone storm for source {adapter.source_ref!r}"
                        f" (tenant {tenant_id!r}): more than {storm_limit}"
                        " removals in one run — halted for review (§8.1g);"
                        " re-run with allow_mass_tombstone=True to apply")
                if self.pipeline.tombstone_raw(tenant_id,
                                               adapter.source_system,
                                               item.native_id):
                    result.tombstoned += 1
            else:
                raw_id, created = self._land_item(tenant_id, adapter, item)
                self.dispatcher.dispatch(tenant_id, raw_id)
                result.dispatched += 1
                if created:
                    result.landed += 1
                else:
                    result.replayed += 1
            # Checkpoint AFTER the item is fully applied: it can no longer be
            # lost. Incremental cursor advances during backfill too, so the
            # first incremental run doesn't re-walk what backfill just landed.
            if mode == "backfill":
                self.registry.checkpoint(tenant_id, adapter.source_ref,
                                         cursor=item.cursor,
                                         backfill_cursor=item.cursor,
                                         ordered=ordered)
            else:
                self.registry.checkpoint(tenant_id, adapter.source_ref,
                                         cursor=item.cursor, ordered=ordered)

    def _corpus_count(self, tenant_id: str, source_system: str) -> int:
        """Live (non-tombstoned) logical documents for the storm denominator."""
        with self.pipeline.store.transaction(tenant_id) as conn:
            row = conn.execute(
                "SELECT count(DISTINCT source_native_id) AS n"
                " FROM raw_documents"
                " WHERE tenant_id = %s AND source_system = %s"
                "   AND deleted_at IS NULL",
                (tenant_id, source_system),
            ).fetchone()
        return row["n"]

    # ----------------------------------------------------------------- item --
    def _land_item(self, tenant_id: str, adapter: SourceAdapter,
                   item: SourceItem) -> tuple[int, bool]:
        """Land one item; returns (raw_document_id, created). Raw bytes are
        stored faithfully, untransformed; the source ACL rides along. An
        observed upsert outranks any earlier delete signal, so a tombstoned
        logical doc that reappears (recycle-bin restore, access re-granted)
        is revived either way."""
        content_hash = hashlib.sha256(item.content).hexdigest()

        existing = self.raw_store.exists(tenant_id, content_hash)
        if existing is not None:
            self.pipeline.revive_raw(tenant_id, adapter.source_system,
                                     item.native_id)
            return existing.id, False  # same bytes already landed: no-op

        raw_uri = self.raw_store.put(tenant_id, item.content, meta={
            "content_hash": content_hash,
            "native_id": item.native_id,
            "mime_type": item.mime_type,
        })
        raw = RawDocument(
            tenant_id=tenant_id,
            source_system=adapter.source_system,
            source_native_id=item.native_id,
            mime_type=item.mime_type,
            content_hash=content_hash,
            raw_uri=raw_uri,
            source_acl=item.source_acl.model_dump(exclude_none=True),
            captured_at=item.mtime,  # when it existed/occurred at source
            native_metadata=item.native_metadata,
            status="landed",
        )
        raw_id = self.pipeline.ingest_raw(raw)  # version stamp + idempotent row
        self.pipeline.revive_raw(tenant_id, adapter.source_system,
                                 item.native_id)
        return raw_id, True
