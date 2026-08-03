"""Contract-hardening proof, adapter-agnostic: everything the Microsoft
Graph family needs from the capture flow, exercised with synthetic adapters
against the REAL stack (Postgres/SeaweedFS/OpenBao) — no M365 tenant
required. The live-tenant validation runbook is CONNECTOR_NOTES.md.

Covers: opaque (non-ordered) cursor checkpointing, the end-of-sweep
final_cursor channel (including zero-item sweeps), tombstone soft-deletes +
revival, the §8.1g tombstone-storm guard, CursorInvalid -> automatic resync,
mid-pull SecretsError containment, and the cross-tenant prepare guard."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterator, Optional

import pytest

from knowledge_hub.capture import CaptureService
from knowledge_hub.interfaces import (
    CursorInvalid,
    SecretNotFound,
    SourceAcl,
    SourceAdapter,
    SourceItem,
)


def upsert(nid: str, content: bytes, cur: str) -> SourceItem:
    return SourceItem(native_id=nid, content=content, size=len(content),
                      mtime=datetime.now(tz=timezone.utc),
                      source_acl=SourceAcl(model="test.v1"), cursor=cur)


def tombstone(nid: str, cur: str) -> SourceItem:
    return SourceItem(native_id=nid, change="tombstone",
                      mtime=datetime.now(tz=timezone.utc), cursor=cur)


class OpaqueAdapter(SourceAdapter):
    """Scripted opaque-cursor adapter: yields `items`, reports `final` at
    end of sweep, optionally rejects its incremental cursor (expiry)."""
    source_system = "scripted"
    cursor_ordering = "opaque"

    def __init__(self, source_ref: str = "opq", items=(),
                 final: Optional[str] = None,
                 reject_cursor: bool = False):
        super().__init__(source_ref)
        self.items = list(items)
        self.final = final
        self.reject_cursor = reject_cursor
        self.seen_cursor: Optional[str] = "unset"

    def backfill(self, tenant_id: str,
                 resume_after: Optional[str] = None) -> Iterator[SourceItem]:
        self.seen_cursor = resume_after
        yield from self.items

    def incremental(self, tenant_id: str,
                    cursor: Optional[str]) -> Iterator[SourceItem]:
        self.seen_cursor = cursor
        if self.reject_cursor:
            raise CursorInvalid(tenant_id, self.source_ref, "scripted expiry")
        yield from self.items

    def final_cursor(self) -> Optional[str]:
        return self.final


class MidPullSecretsAdapter(OpaqueAdapter):
    """Yields its first item, then loses its credential (rotation mid-pull)."""

    def backfill(self, tenant_id: str,
                 resume_after: Optional[str] = None) -> Iterator[SourceItem]:
        yield self.items[0]
        raise SecretNotFound(tenant_id, self.source_ref, "rotated away")


def raw_rows(store, tenant, native_id=None):
    q = "SELECT * FROM raw_documents WHERE tenant_id = %s"
    params = [tenant]
    if native_id is not None:
        q += " AND source_native_id = %s"
        params.append(native_id)
    with store.transaction(tenant) as conn:
        return conn.execute(q + " ORDER BY id", params).fetchall()


# ---------------------------------------------------------------- cursors --
def test_opaque_cursor_stored_verbatim_not_greatest(capture, tenant):
    # Opaque tokens do NOT sort: the later checkpoint ("aaa") compares
    # lexicographically SMALLER than the earlier one ("zzz"). The registry
    # must keep the latest write, not the greatest string.
    adapter = OpaqueAdapter(items=[upsert("a", b"first bytes", "zzz"),
                                   upsert("b", b"second bytes", "aaa")])
    result = capture.run_source(tenant, adapter)

    assert result.status == "ok" and result.landed == 2
    entry = capture.registry.get(tenant, "opq")
    assert entry.cursor == "aaa"
    assert entry.backfill_done


def test_final_cursor_persisted_even_for_empty_sweep(capture, tenant):
    # Backfill sweep: the deltaLink-style high-water mark arrives only via
    # final_cursor() (zero items — nothing to piggyback a checkpoint on).
    capture.run_source(tenant, OpaqueAdapter(items=[], final="DELTA-1"))
    entry = capture.registry.get(tenant, "opq")
    assert entry.backfill_done and entry.cursor == "DELTA-1"

    # Empty incremental sweep: the stored cursor is handed to the adapter
    # and the fresh final token still advances the checkpoint (a delta token
    # must never quietly age toward expiry across empty runs).
    adapter = OpaqueAdapter(items=[], final="DELTA-2")
    result = capture.run_source(tenant, adapter)
    assert result.mode == "incremental"
    assert adapter.seen_cursor == "DELTA-1"
    assert capture.registry.get(tenant, "opq").cursor == "DELTA-2"


def test_cursor_invalid_triggers_automatic_resync(capture, store, tenant):
    capture.run_source(tenant, OpaqueAdapter(
        items=[upsert("doc/x", b"v1 bytes", "c1")], final="OLD-DELTA"))

    # The incremental cursor is now expired at the "source": the adapter
    # raises CursorInvalid, and the flow must reset + re-backfill in the
    # same call — landing stays idempotent, so nothing duplicates.
    adapter = OpaqueAdapter(items=[upsert("doc/x", b"v1 bytes", "c2"),
                                   upsert("doc/y", b"new doc bytes", "c3")],
                            final="NEW-DELTA", reject_cursor=True)
    result = capture.run_source(tenant, adapter)

    assert result.status == "ok"
    assert result.mode == "backfill"          # the resync pass
    assert result.replayed == 1 and result.landed == 1
    entry = capture.registry.get(tenant, "opq")
    assert entry.cursor == "NEW-DELTA" and entry.backfill_done
    assert "resync" in (entry.status_reason or "")
    assert len(raw_rows(store, tenant)) == 2  # no duplicate rows


# -------------------------------------------------------------- tombstones --
def test_tombstone_soft_deletes_all_versions_then_revives(
        capture, store, tenant):
    capture.run_source(tenant, OpaqueAdapter(
        items=[upsert("doc/x", b"version one", "c1")]))
    capture.run_source(tenant, OpaqueAdapter(
        items=[upsert("doc/x", b"version two", "c2")]))
    assert [r["version"] for r in raw_rows(store, tenant, "doc/x")] == [1, 2]

    # Explicit delete signal -> every version row is stamped, bytes stay.
    result = capture.run_source(tenant, OpaqueAdapter(
        items=[tombstone("doc/x", "c3")]))
    assert result.tombstoned == 1
    assert all(r["deleted_at"] is not None
               for r in raw_rows(store, tenant, "doc/x"))

    # Recycle-bin restore: same bytes reappear (a replay, not a new version)
    # and the observed upsert outranks the earlier delete signal.
    result = capture.run_source(tenant, OpaqueAdapter(
        items=[upsert("doc/x", b"version two", "c4")]))
    assert result.replayed == 1 and result.landed == 0
    assert all(r["deleted_at"] is None
               for r in raw_rows(store, tenant, "doc/x"))


def test_tombstone_storm_halts_for_review(pipeline, raw_store, dispatcher,
                                          store, tenant):
    # A tight guard: at most max(1, 10% of corpus) removals per run.
    capture = CaptureService(pipeline, raw_store, dispatcher,
                             tombstone_storm_min=1,
                             tombstone_storm_fraction=0.10)
    capture.run_source(tenant, OpaqueAdapter(
        items=[upsert("d/a", b"aaa bytes", "c1"),
               upsert("d/b", b"bbb bytes", "c2"),
               upsert("d/c", b"ccc bytes", "c3")]))

    # "Everything vanished" — the §8.1g scope-artifact shape. One tombstone
    # is allowed through, the second trips the guard BEFORE being applied or
    # checkpointed.
    storm = OpaqueAdapter(items=[tombstone("d/a", "c4"),
                                 tombstone("d/b", "c5"),
                                 tombstone("d/c", "c6")])
    result = capture.run_source(tenant, storm)

    assert result.status == "degraded"
    assert "tombstone storm" in result.reason
    assert result.tombstoned == 1
    deleted = [r for r in raw_rows(store, tenant) if r["deleted_at"]]
    assert len(deleted) == 1
    entry = capture.registry.get(tenant, "opq")
    assert entry.status == "degraded"
    assert entry.cursor == "c4"  # halted un-checkpointed at the tripping item

    # Operator reviewed: apply deliberately. The already-applied tombstone
    # replays as a no-op (deleted_at already set -> 0 rows stamped).
    result = capture.run_source(tenant, OpaqueAdapter(
        items=[tombstone("d/a", "c4"), tombstone("d/b", "c5"),
               tombstone("d/c", "c6")]), allow_mass_tombstone=True)
    assert result.status == "ok" and result.tombstoned == 2
    assert all(r["deleted_at"] is not None for r in raw_rows(store, tenant))
    assert capture.registry.get(tenant, "opq").status == "active"


# ------------------------------------------------------------- containment --
def test_mid_pull_secrets_error_degrades_source_only(capture, store, tenant):
    adapter = MidPullSecretsAdapter(
        items=[upsert("doc/a", b"landed before rotation", "c1")])
    result = capture.run_source(tenant, adapter)

    # Same containment as a prepare() failure: degraded, never raised —
    # and the item landed before the failure stays landed + checkpointed.
    assert result.status == "degraded"
    assert "rotated away" in result.reason
    assert result.landed == 1
    entry = capture.registry.get(tenant, "opq")
    assert entry.status == "degraded"
    assert entry.backfill_cursor == "c1" and not entry.backfill_done
    assert len(raw_rows(store, tenant)) == 1


def test_adapter_refuses_cross_tenant_pull():
    class Guarded(OpaqueAdapter):
        def backfill(self, tenant_id, resume_after=None):
            self.require_prepared(tenant_id)
            return iter(())

    adapter = Guarded()
    adapter.prepare("tenant-a", None)
    with pytest.raises(RuntimeError, match="one adapter instance"):
        adapter.backfill("tenant-b")
    # Re-preparing for the other tenant rebinds legitimately.
    adapter.prepare("tenant-b", None)
    assert list(adapter.backfill("tenant-b")) == []
