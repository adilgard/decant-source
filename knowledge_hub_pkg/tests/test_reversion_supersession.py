"""Re-version supersession (BP8 — the BP7 primitive's second trigger),
against the real stack: when version N+1's facts promote, version N's facts
retire through the SAME retraction machinery (reason='superseded'), in ONE
transaction with ONE cutover timestamp.

What must hold: the cutover DIFFS (an assertion the new version still makes
keeps its existing row — temporally continuous, no duplicate; only dropped
assertions retract; new ones insert with valid_from = the cutover); the
cutover is atomic (a failure anywhere rolls the whole document group back —
no window sees both versions or neither); the two retraction reasons stay
independent under supersede→delete→revive; evidence retrieval serves only
the current version's chunks; a superseded version's late-resolving pending
facts never promote; the §8.1g eager/lazy split defers RE-extraction of
lazy tracks via the outbox's available_at."""
from __future__ import annotations

import uuid
from datetime import timedelta
from types import SimpleNamespace
from typing import Optional

import pytest

from conftest import unit_vec
from factories import ONTOLOGY, make_chunk, make_entity, make_raw

from knowledge_hub.choke_point import PostgresChokePoint
from knowledge_hub.dispatch_pg import PostgresDispatcher
from knowledge_hub.factstore_pg import vector_literal
from knowledge_hub.models import ChunkLevel, DocType, Document, Fact, PendingFact
from knowledge_hub.operations import (
    DENSE_RETRIEVE_SQL,
    InProcessOperationCatalog,
    register_serving_defaults,
)
from knowledge_hub.pipeline import (
    RETRACTED_BY_REVERSION,
    RETRACTED_BY_TOMBSTONE,
)
from knowledge_hub.processing import ProcessingService
from knowledge_hub.serving import Principal, RetrievalQuery, UncertaintyState

SOURCE = "sharepoint"  # factories.make_raw's default source_system


# ---------------------------------------------------------------- fixtures --
@pytest.fixture(scope="module")
def choke(test_dsn: str) -> PostgresChokePoint:
    cp = PostgresChokePoint(dsn=test_dsn)
    yield cp
    cp.close()


@pytest.fixture()
def catalog(choke, tenant) -> InProcessOperationCatalog:
    cat = InProcessOperationCatalog(choke, None)
    register_serving_defaults(cat, tenant)
    return cat


def principal_for(tenant: str) -> Principal:
    return Principal(tenant_id=tenant, principal_id="test-caller", roles=[])


# ------------------------------------------------------------- seed helpers --
def land_version(pipeline, store, tenant: str, *, native_id: str,
                 axis: int = 0) -> SimpleNamespace:
    """One landed version of a logical document (same native_id across calls
    -> _next_version stamps 1, 2, ...), with an embedded child chunk."""
    raw = make_raw(tenant, source_system=SOURCE, source_native_id=native_id)
    pipeline.ingest_raw(raw)
    doc = Document(tenant_id=tenant, raw_document_id=raw.id,
                   doc_type=DocType.prose,
                   title=f"{native_id} v{raw.version}")
    store.insert_document(doc)
    parent = make_chunk(tenant, doc.id)
    store.insert_chunks([parent])
    child = make_chunk(tenant, doc.id, level=ChunkLevel.child,
                       parent_chunk_id=parent.id, embedding=unit_vec(axis),
                       embedding_model="bge-m3")
    store.insert_chunks([child])
    return SimpleNamespace(raw=raw, doc=doc, child=child,
                           native_id=native_id, version=raw.version)


def add_fact(store, tenant: str, subject_id: int, predicate: str,
             literal: str, *, doc_id: int) -> int:
    return store.write_facts([Fact(
        tenant_id=tenant, subject_entity_id=subject_id, predicate=predicate,
        object_literal=literal, ontology_version=ONTOLOGY,
        source_document_id=doc_id, extractor="test",
        extractor_version="rs")])[0]


def stage(store, tenant: str, subject_id: int, predicate: str, literal: str,
          *, doc_id: int) -> None:
    store.stage_pending({}, [PendingFact(
        tenant_id=tenant, subject_ref=f"entity:{subject_id}",
        predicate=predicate, object_literal=literal,
        ontology_version=ONTOLOGY, source_document_id=doc_id,
        extractor="test", extractor_version="rs")])


def fact_rows(store, tenant: str, ids=None) -> dict[int, dict]:
    q = ("SELECT id, valid_from, valid_to, retraction_reason FROM facts"
         " WHERE tenant_id = %s")
    params: list = [tenant]
    if ids is not None:
        q += " AND id = ANY(%s)"
        params.append(list(ids))
    with store.transaction(tenant) as conn:
        return {r["id"]: r for r in conn.execute(q, params).fetchall()}


def doc_row(store, tenant: str, doc_id: int) -> dict:
    with store.transaction(tenant) as conn:
        return conn.execute(
            "SELECT valid_to, retraction_reason FROM documents"
            " WHERE tenant_id = %s AND id = %s", (tenant, doc_id)).fetchone()


def served(catalog, tenant: str, entity_id: int, **extra) -> dict[int, object]:
    envelopes = catalog.execute(
        "get_facts", {"entity_id": entity_id, **extra}, principal_for(tenant))
    return {env.fact_id: env for env in envelopes}


# ------------------------------------------------------------ diff cutover --
def test_diff_cutover_retires_changed_keeps_unchanged_adds_new(
        pipeline, store, catalog, tenant):
    v1 = land_version(pipeline, store, tenant, native_id="DOC-X")
    v2 = land_version(pipeline, store, tenant, native_id="DOC-X")
    assert (v1.version, v2.version) == (1, 2)
    subj = make_entity(tenant, "Granite Botanicals")
    store.upsert_entity(subj)

    unchanged = add_fact(store, tenant, subj.id, "references", "Net-30",
                         doc_id=v1.doc.id)
    dropped = add_fact(store, tenant, subj.id, "mentions", "old-clause",
                       doc_id=v1.doc.id)
    assert set(served(catalog, tenant, subj.id)) == {unchanged, dropped}

    # v2 re-asserts the unchanged triple and replaces the dropped one.
    stage(store, tenant, subj.id, "references", "Net-30", doc_id=v2.doc.id)
    stage(store, tenant, subj.id, "mentions", "new-clause", doc_id=v2.doc.id)
    promoted = pipeline.promote_pending(tenant)

    # The unchanged assertion promoted to its SURVIVING row — same id, no
    # new row; the new assertion inserted.
    assert unchanged in promoted
    added = next(fid for fid in promoted if fid != unchanged)

    # Default serve: exactly v2's view, no duplicates, no stale row.
    now_served = served(catalog, tenant, subj.id)
    assert set(now_served) == {unchanged, added}
    assert now_served[unchanged].state is UncertaintyState.known_confident

    rows = fact_rows(store, tenant, [unchanged, dropped, added])
    # Continuity: the unchanged fact never blinked — no valid_to, no reason,
    # same row id it was born with.
    assert rows[unchanged]["valid_to"] is None
    assert rows[unchanged]["retraction_reason"] is None
    # The dropped assertion retired through the shared primitive.
    assert rows[dropped]["retraction_reason"] == RETRACTED_BY_REVERSION
    # ONE cutover timestamp bookends the change: old validity ends exactly
    # where new validity begins.
    cutover = rows[dropped]["valid_to"]
    assert cutover is not None
    assert rows[added]["valid_from"] == cutover

    # The prior version's document retired with the same stamp; the new
    # version's document is current.
    v1_doc = doc_row(store, tenant, v1.doc.id)
    assert (v1_doc["valid_to"], v1_doc["retraction_reason"]) == \
        (cutover, RETRACTED_BY_REVERSION)
    assert doc_row(store, tenant, v2.doc.id)["valid_to"] is None

    # Audit trail: the re-assertion is recorded (v2's staging row points at
    # the surviving fact), and the retired fact serves honestly on request.
    with store.transaction(tenant) as conn:
        staged = conn.execute(
            "SELECT promoted_fact_id, resolution_status FROM pending_facts"
            " WHERE tenant_id = %s AND source_document_id = %s ORDER BY id",
            (tenant, v2.doc.id)).fetchall()
    assert [s["resolution_status"] for s in staged] == ["promoted", "promoted"]
    assert staged[0]["promoted_fact_id"] == unchanged
    audit = served(catalog, tenant, subj.id, include_retracted=True)
    assert audit[dropped].state is UncertaintyState.retracted


def test_cutover_is_atomic_poison_rolls_back_whole_group(
        pipeline, store, catalog, tenant):
    v1 = land_version(pipeline, store, tenant, native_id="DOC-X")
    v2 = land_version(pipeline, store, tenant, native_id="DOC-X")
    subj = make_entity(tenant, "Atomic Labs")
    store.upsert_entity(subj)
    old = add_fact(store, tenant, subj.id, "mentions", "old-clause",
                   doc_id=v1.doc.id)

    stage(store, tenant, subj.id, "references", "Net-30", doc_id=v2.doc.id)
    store.stage_pending({}, [PendingFact(  # poison: dangling mention ref
        tenant_id=tenant, subject_ref="mention:999999999",
        predicate="mentions", object_literal="poison",
        ontology_version=ONTOLOGY, source_document_id=v2.doc.id,
        extractor="test", extractor_version="rs")])

    with pytest.raises(LookupError):
        pipeline.promote_pending(tenant)

    # NOTHING happened: no new fact, no retraction, no promoted staging row
    # — the group is one transaction, so a query never sees half a cutover.
    assert set(served(catalog, tenant, subj.id)) == {old}
    assert fact_rows(store, tenant, [old])[old]["valid_to"] is None
    assert doc_row(store, tenant, v1.doc.id)["valid_to"] is None
    with store.transaction(tenant) as conn:
        statuses = conn.execute(
            "SELECT resolution_status FROM pending_facts"
            " WHERE tenant_id = %s", (tenant,)).fetchall()
    assert {s["resolution_status"] for s in statuses} == {"pending"}

    # Cure the poison; the same sweep now completes the whole cutover.
    with store.transaction(tenant) as conn:
        conn.execute(
            "UPDATE pending_facts SET subject_ref = %s"
            " WHERE tenant_id = %s AND subject_ref = 'mention:999999999'",
            (f"entity:{subj.id}", tenant))
    promoted = pipeline.promote_pending(tenant)
    assert len(promoted) == 2
    assert old not in served(catalog, tenant, subj.id)
    assert fact_rows(store, tenant, [old])[old]["retraction_reason"] == \
        RETRACTED_BY_REVERSION


def test_all_current_prior_versions_heal_at_cutover(
        pipeline, store, catalog, tenant):
    """History before this build left v1 AND v2 facts current side by side;
    v3's cutover retires every earlier version in one pass."""
    v1 = land_version(pipeline, store, tenant, native_id="DOC-X")
    v2 = land_version(pipeline, store, tenant, native_id="DOC-X")
    v3 = land_version(pipeline, store, tenant, native_id="DOC-X")
    subj = make_entity(tenant, "Sediment Corp")
    store.upsert_entity(subj)
    f1 = add_fact(store, tenant, subj.id, "mentions", "v1-only",
                  doc_id=v1.doc.id)
    f2 = add_fact(store, tenant, subj.id, "mentions", "v2-only",
                  doc_id=v2.doc.id)

    stage(store, tenant, subj.id, "mentions", "v3-only", doc_id=v3.doc.id)
    promoted = pipeline.promote_pending(tenant)

    assert set(served(catalog, tenant, subj.id)) == set(promoted)
    rows = fact_rows(store, tenant, [f1, f2])
    assert all(r["retraction_reason"] == RETRACTED_BY_REVERSION
               for r in rows.values())
    assert doc_row(store, tenant, v1.doc.id)["retraction_reason"] == \
        RETRACTED_BY_REVERSION
    assert doc_row(store, tenant, v2.doc.id)["retraction_reason"] == \
        RETRACTED_BY_REVERSION
    assert doc_row(store, tenant, v3.doc.id)["valid_to"] is None


# ------------------------------------------------ reasons stay independent --
def test_supersede_then_delete_then_revive(pipeline, store, catalog, tenant):
    v1 = land_version(pipeline, store, tenant, native_id="DOC-X")
    v2 = land_version(pipeline, store, tenant, native_id="DOC-X")
    subj = make_entity(tenant, "Layered GmbH")
    store.upsert_entity(subj)
    kept = add_fact(store, tenant, subj.id, "references", "Net-30",
                    doc_id=v1.doc.id)
    dropped = add_fact(store, tenant, subj.id, "mentions", "old-clause",
                       doc_id=v1.doc.id)
    stage(store, tenant, subj.id, "references", "Net-30", doc_id=v2.doc.id)
    stage(store, tenant, subj.id, "mentions", "new-clause", doc_id=v2.doc.id)
    promoted = pipeline.promote_pending(tenant)
    added = next(fid for fid in promoted if fid != kept)

    # Whole logical doc deleted at the source: current facts (kept + added)
    # tombstone; the superseded row is NOT double-stamped.
    pipeline.tombstone_raw(tenant, SOURCE, "DOC-X")
    rows = fact_rows(store, tenant, [kept, dropped, added])
    assert rows[kept]["retraction_reason"] == RETRACTED_BY_TOMBSTONE
    assert rows[added]["retraction_reason"] == RETRACTED_BY_TOMBSTONE
    assert rows[dropped]["retraction_reason"] == RETRACTED_BY_REVERSION
    assert served(catalog, tenant, subj.id) == {}

    # Restored from the recycle bin: ONLY the tombstone reverses — the
    # superseded row stays retired, the pre-delete serve returns exactly.
    pipeline.revive_raw(tenant, SOURCE, "DOC-X")
    assert set(served(catalog, tenant, subj.id)) == {kept, added}
    rows = fact_rows(store, tenant, [kept, dropped, added])
    assert rows[dropped]["retraction_reason"] == RETRACTED_BY_REVERSION
    assert doc_row(store, tenant, v1.doc.id)["retraction_reason"] == \
        RETRACTED_BY_REVERSION  # superseded doc never revives
    assert doc_row(store, tenant, v2.doc.id)["valid_to"] is None


# -------------------------------------------------------- serve-side sweeps --
def test_evidence_serves_only_the_current_versions_chunks(
        pipeline, store, choke, catalog, tenant):
    axis = 7
    v1 = land_version(pipeline, store, tenant, native_id="DOC-X", axis=axis)
    v2 = land_version(pipeline, store, tenant, native_id="DOC-X", axis=axis)
    subj = make_entity(tenant, "Chunky Ltd")
    store.upsert_entity(subj)
    params = {"query": vector_literal(unit_vec(axis)), "k": 10}
    fq = choke.enforce(RetrievalQuery(text="q"), principal_for(tenant))

    # Before cutover BOTH versions' chunks serve — the staleness gap.
    chunk_ids = {r["chunk_id"] for r in
                 choke.read(fq, DENSE_RETRIEVE_SQL, params)}
    assert {v1.child.id, v2.child.id} <= chunk_ids

    stage(store, tenant, subj.id, "mentions", "v2-clause", doc_id=v2.doc.id)
    pipeline.promote_pending(tenant)

    chunk_ids = {r["chunk_id"] for r in
                 choke.read(fq, DENSE_RETRIEVE_SQL, params)}
    assert v2.child.id in chunk_ids
    assert v1.child.id not in chunk_ids  # the gap, closed

    audit_fq = choke.enforce(
        RetrievalQuery(text="q", include_retracted=True),
        principal_for(tenant))
    chunk_ids = {r["chunk_id"] for r in
                 choke.read(audit_fq, DENSE_RETRIEVE_SQL, params)}
    assert {v1.child.id, v2.child.id} <= chunk_ids


def test_superseded_versions_late_pendings_never_promote(
        pipeline, store, tenant):
    v1 = land_version(pipeline, store, tenant, native_id="DOC-X")
    v2 = land_version(pipeline, store, tenant, native_id="DOC-X")
    subj = make_entity(tenant, "Latecomer Inc")
    store.upsert_entity(subj)
    stage(store, tenant, subj.id, "mentions", "v2-clause", doc_id=v2.doc.id)
    pipeline.promote_pending(tenant)  # v1 is now superseded

    # A stale v1 extraction resolves late — it must never become current.
    stage(store, tenant, subj.id, "mentions", "stale-v1", doc_id=v1.doc.id)
    assert pipeline.promote_pending(tenant) == []
    with store.transaction(tenant) as conn:
        row = conn.execute(
            "SELECT resolution_status FROM pending_facts"
            " WHERE tenant_id = %s AND object_literal = 'stale-v1'",
            (tenant,)).fetchone()
    assert row["resolution_status"] == "pending"  # skipped, never mutated


# ------------------------------------------------------------- eager/lazy --
def test_reextraction_policy_defers_only_lazy_reversions(pipeline):
    service = ProcessingService(
        pipeline, None, None, None, None,
        lazy_reextract_tracks=frozenset({"prose"}),
        lazy_reextract_delay=timedelta(hours=2))
    assert service._reextraction_delay(1, "prose") == timedelta(0)   # first landing
    assert service._reextraction_delay(2, "prose") == timedelta(hours=2)  # lazy re-version
    assert service._reextraction_delay(2, "sop") == timedelta(0)     # eager track
    assert service._reextraction_delay(2, None) == timedelta(0)      # unknown: eager
    # Default policy is eager-for-everything (empty lazy set).
    eager = ProcessingService(pipeline, None, None, None, None)
    assert eager._reextraction_delay(5, "prose") == timedelta(0)


def test_deferred_dispatch_is_not_claimable_until_available(
        pipeline, store, tenant):
    dispatcher = PostgresDispatcher(store, table="extraction_queue")
    lazy_raw = make_raw(tenant, source_system=SOURCE)
    eager_raw = make_raw(tenant, source_system=SOURCE)
    pipeline.ingest_raw(lazy_raw)
    pipeline.ingest_raw(eager_raw)

    message_id = dispatcher.dispatch(tenant, lazy_raw.id,
                                     delay=timedelta(hours=1))
    dispatcher.dispatch(tenant, eager_raw.id)

    claimed = dispatcher.claim(tenant, limit=10)
    assert [m.raw_document_id for m in claimed] == [eager_raw.id]

    # Idempotent re-dispatch keeps the schedule — it never resets the clock
    # to now, and never duplicates the record.
    assert dispatcher.dispatch(tenant, lazy_raw.id) == message_id
    assert [m.raw_document_id for m in dispatcher.claim(tenant, limit=10)] == []
