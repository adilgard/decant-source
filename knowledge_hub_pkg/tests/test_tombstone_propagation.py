"""Tombstone propagation (migration 009), against the real stack: a source
deletion carries through raw_documents -> documents -> facts on the reserved
temporal axis, and the serving layer stops returning the gone facts at the
choke point — while an explicit include_retracted audit query reaches them
honestly labeled.

What must hold: retraction is per PROVENANCE LINK (a multi-source assertion
survives losing one source via its sibling row); delete->revive is a perfect
round trip; revival never resurrects valid_to set by any other writer; the
{cur:} temporal filter is unwritable-to-skip at registration AND at the
gateway (the {sec:} discipline on the temporal axis); retracted facts serve
only under audit and then as state='retracted', never as current; a
retracted document's pending facts do not promote until revival."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Iterator, Optional

import pytest

from conftest import unit_vec
from factories import ONTOLOGY, make_chunk, make_entity, make_raw

from knowledge_hub.choke_point import (
    EnforcementRefused,
    PostgresChokePoint,
    UnenforcedQuery,
)
from knowledge_hub.factstore_pg import vector_literal
from knowledge_hub.models import ChunkLevel, DocType, Document, Fact, PendingFact
from knowledge_hub.operations import (
    DENSE_RETRIEVE_SQL,
    InProcessOperationCatalog,
    OperationRejected,
    OperationSpec,
    ParamSpec,
    fact_template,
    register_serving_defaults,
)
from knowledge_hub.pipeline import RETRACTED_BY_TOMBSTONE
from knowledge_hub.serving import (
    Principal,
    RetrievalQuery,
    UncertaintyState,
)

SOURCE = "sharepoint"  # matches factories.make_raw's default source_system


# ---------------------------------------------------------------- fixtures --
@pytest.fixture(scope="module")
def choke(test_dsn: str) -> PostgresChokePoint:
    cp = PostgresChokePoint(dsn=test_dsn)
    yield cp
    cp.close()


@pytest.fixture()
def catalog(choke, tenant) -> InProcessOperationCatalog:
    # No embedder: these tests never run the embedding_text op.
    cat = InProcessOperationCatalog(choke, None)
    register_serving_defaults(cat, tenant)
    return cat


def principal_for(tenant: str) -> Principal:
    return Principal(tenant_id=tenant, principal_id="test-caller", roles=[])


# ------------------------------------------------------------- seed helpers --
def seed_doc(pipeline, store, tenant: str, *, native_id: str,
             axis: int = 0) -> SimpleNamespace:
    """One landed document with an embedded child chunk."""
    raw = make_raw(tenant, source_system=SOURCE, source_native_id=native_id)
    pipeline.ingest_raw(raw)
    doc = Document(tenant_id=tenant, raw_document_id=raw.id,
                   doc_type=DocType.prose, title=f"{native_id} doc")
    store.insert_document(doc)
    parent = make_chunk(tenant, doc.id)
    store.insert_chunks([parent])
    child = make_chunk(tenant, doc.id, level=ChunkLevel.child,
                       parent_chunk_id=parent.id, embedding=unit_vec(axis),
                       embedding_model="bge-m3")
    store.insert_chunks([child])
    return SimpleNamespace(raw=raw, doc=doc, child=child,
                           native_id=native_id)


def add_fact(store, tenant: str, subject_id: int, predicate: str, *,
             doc_id: Optional[int] = None, chunk_id: Optional[int] = None,
             literal: str = "Net-30") -> int:
    return store.write_facts([Fact(
        tenant_id=tenant, subject_entity_id=subject_id, predicate=predicate,
        object_literal=literal, ontology_version=ONTOLOGY,
        source_document_id=doc_id, source_chunk_id=chunk_id,
        extractor="test", extractor_version="tp")])[0]


def fact_rows(store, tenant: str, ids=None) -> dict[int, dict]:
    q = ("SELECT id, valid_to, retraction_reason FROM facts"
         " WHERE tenant_id = %s")
    params: list = [tenant]
    if ids is not None:
        q += " AND id = ANY(%s)"
        params.append(list(ids))
    with store.transaction(tenant) as conn:
        return {r["id"]: r for r in conn.execute(q, params).fetchall()}


def served(catalog, tenant: str, entity_id: int, **extra) -> dict[int, object]:
    envelopes = catalog.execute(
        "get_facts", {"entity_id": entity_id, **extra}, principal_for(tenant))
    return {env.fact_id: env for env in envelopes}


# ------------------------------------------------------------- propagation --
def test_single_source_retraction_default_hidden_audit_honest(
        pipeline, store, catalog, tenant):
    site = seed_doc(pipeline, store, tenant, native_id="DOC-A")
    subj = make_entity(tenant, "Granite Botanicals")
    store.upsert_entity(subj)
    by_doc = add_fact(store, tenant, subj.id, "references",
                      doc_id=site.doc.id)
    by_chunk = add_fact(store, tenant, subj.id, "mentions",
                        chunk_id=site.child.id)  # chunk-anchored provenance

    assert set(served(catalog, tenant, subj.id)) == {by_doc, by_chunk}

    assert pipeline.tombstone_raw(tenant, SOURCE, site.native_id) == 1

    # Default serve: gone. Both anchor flavors propagated, and the document
    # row itself is retracted with the SAME timestamp as the raw tombstone.
    assert served(catalog, tenant, subj.id) == {}
    rows = fact_rows(store, tenant, [by_doc, by_chunk])
    assert all(r["valid_to"] is not None for r in rows.values())
    assert all(r["retraction_reason"] == RETRACTED_BY_TOMBSTONE
               for r in rows.values())
    with store.transaction(tenant) as conn:
        doc_row = conn.execute(
            "SELECT valid_to, retraction_reason FROM documents WHERE id = %s",
            (site.doc.id,)).fetchone()
        raw_row = conn.execute(
            "SELECT deleted_at FROM raw_documents WHERE id = %s",
            (site.raw.id,)).fetchone()
    assert doc_row["retraction_reason"] == RETRACTED_BY_TOMBSTONE
    assert doc_row["valid_to"] == raw_row["deleted_at"]

    # Audit escape: explicitly requested, the facts return HONESTLY labeled —
    # state 'retracted', valid_to visible — never masquerading as current.
    audit = served(catalog, tenant, subj.id, include_retracted=True)
    assert set(audit) == {by_doc, by_chunk}
    for env in audit.values():
        assert env.state is UncertaintyState.retracted
        assert env.valid_to is not None and not env.is_current


def test_multi_source_fact_survives_losing_one_source(
        pipeline, store, catalog, tenant):
    """THE load-bearing case: an assertion grounded in two documents exists
    as sibling provenance rows; deleting one source retracts only its row,
    and the assertion stays served through the survivor."""
    doc_a = seed_doc(pipeline, store, tenant, native_id="DOC-A")
    doc_b = seed_doc(pipeline, store, tenant, native_id="DOC-B", axis=1)
    subj = make_entity(tenant, "Meridian Supply")
    store.upsert_entity(subj)
    from_a = add_fact(store, tenant, subj.id, "references",
                      doc_id=doc_a.doc.id)
    from_b = add_fact(store, tenant, subj.id, "references",
                      doc_id=doc_b.doc.id)

    pipeline.tombstone_raw(tenant, SOURCE, doc_a.native_id)

    now_served = served(catalog, tenant, subj.id)
    assert from_a not in now_served          # the deleted source's link
    assert from_b in now_served              # the assertion SURVIVES
    assert now_served[from_b].predicate == "references"
    rows = fact_rows(store, tenant, [from_a, from_b])
    assert rows[from_a]["valid_to"] is not None
    assert rows[from_b]["valid_to"] is None  # untouched, not merely re-served


def test_delete_revive_round_trip_is_identity(pipeline, store, catalog, tenant):
    site = seed_doc(pipeline, store, tenant, native_id="DOC-A")
    subj = make_entity(tenant, "Round Trip Labs")
    store.upsert_entity(subj)
    add_fact(store, tenant, subj.id, "references", doc_id=site.doc.id)
    add_fact(store, tenant, subj.id, "mentions", chunk_id=site.child.id)

    before = {fid: env.state for fid, env in
              served(catalog, tenant, subj.id).items()}

    assert pipeline.tombstone_raw(tenant, SOURCE, site.native_id) == 1
    assert served(catalog, tenant, subj.id) == {}
    assert pipeline.revive_raw(tenant, SOURCE, site.native_id) == 1

    after = {fid: env.state for fid, env in
             served(catalog, tenant, subj.id).items()}
    assert after == before  # identical to never-deleted
    assert all(r["valid_to"] is None and r["retraction_reason"] is None
               for r in fact_rows(store, tenant, list(before)).values())


def test_revival_never_resurrects_other_writers(pipeline, store, tenant):
    """valid_to set by anything other than tombstone propagation (a future
    're-version supersession' writer, a manual temporal edit) survives a
    delete->revive cycle untouched — revival reverses ONLY its own reason."""
    site = seed_doc(pipeline, store, tenant, native_id="DOC-A")
    subj = make_entity(tenant, "Half Superseded Inc")
    store.upsert_entity(subj)
    live = add_fact(store, tenant, subj.id, "references", doc_id=site.doc.id)
    superseded = add_fact(store, tenant, subj.id, "mentions",
                          doc_id=site.doc.id)
    old_stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with store.transaction(tenant) as conn:
        conn.execute(
            "UPDATE facts SET valid_to = %s, retraction_reason = %s"
            " WHERE tenant_id = %s AND id = %s",
            (old_stamp, "superseded", tenant, superseded))

    pipeline.tombstone_raw(tenant, SOURCE, site.native_id)
    rows = fact_rows(store, tenant, [live, superseded])
    # The already-superseded row keeps its own stamp — never overwritten.
    assert rows[superseded]["valid_to"] == old_stamp
    assert rows[superseded]["retraction_reason"] == "superseded"

    pipeline.revive_raw(tenant, SOURCE, site.native_id)
    rows = fact_rows(store, tenant, [live, superseded])
    assert rows[live]["valid_to"] is None            # tombstone reversed
    assert rows[superseded]["valid_to"] == old_stamp  # other writer intact
    assert rows[superseded]["retraction_reason"] == "superseded"


# ------------------------------------------------------------ serve filter --
def test_temporal_filter_is_unbypassable(choke, catalog, tenant):
    # Registration: a facts-reading template without {cur:} is unwritable —
    # the same authoring-time guarantee as a missing {sec:}.
    with pytest.raises(OperationRejected, match="temporally unfiltered"):
        catalog.register(tenant, OperationSpec(
            name="sneaky_current_bypass",
            description="tries to read facts without the temporal marker",
            returns="facts", latency="lookup",
            params={"entity_id": ParamSpec(type="int", required=True)},
            sql="SELECT f.id AS fact_id FROM facts f WHERE {sec:f}"
                " AND f.subject_entity_id = %(entity_id)s"))

    # Authors may not smuggle the reserved audit flag in as an op param.
    with pytest.raises(OperationRejected, match="reserved"):
        catalog.register(tenant, OperationSpec(
            name="sneaky_param_shadow",
            description="declares the reserved query-level flag",
            returns="facts", latency="lookup",
            params={"include_retracted": ParamSpec(type="bool")},
            sql=fact_template(where="TRUE")))

    # Runtime gateway: the same template is refused at read() even if it
    # never went through the generator.
    fq = choke.enforce(RetrievalQuery(text="q"), principal_for(tenant))
    with pytest.raises(EnforcementRefused, match="temporally unfiltered"):
        choke.read(fq, "SELECT f.id FROM facts f WHERE {sec:f}")
    # Unaliased temporal tables cannot dodge the alias-bound marker either.
    with pytest.raises(EnforcementRefused, match="must be aliased"):
        choke.read(fq, "SELECT id FROM facts WHERE {sec:f}")


def test_audit_scope_is_tamper_proof(choke, tenant):
    """Flipping include_retracted AFTER enforcement is a refusal: the flag
    lives in the proof snapshot, exactly like tenant and labels."""
    fq = choke.enforce(RetrievalQuery(text="q"), principal_for(tenant))
    fq.include_retracted = True
    with pytest.raises(UnenforcedQuery, match="mutated"):
        choke.read(fq, "SELECT f.id FROM facts f WHERE {sec:f} AND {cur:f}")


def test_evidence_path_excludes_retracted_documents(pipeline, store, choke,
                                                    tenant):
    axis = 5
    site = seed_doc(pipeline, store, tenant, native_id="DOC-A", axis=axis)
    params = {"query": vector_literal(unit_vec(axis)), "k": 10}

    fq = choke.enforce(RetrievalQuery(text="q"), principal_for(tenant))
    chunk_ids = [r["chunk_id"] for r in
                 choke.read(fq, DENSE_RETRIEVE_SQL, params)]
    assert site.child.id in chunk_ids

    pipeline.tombstone_raw(tenant, SOURCE, site.native_id)

    # A deleted document's chunks stop serving as evidence (a chunk's
    # currency IS its document's) ...
    chunk_ids = [r["chunk_id"] for r in
                 choke.read(fq, DENSE_RETRIEVE_SQL, params)]
    assert site.child.id not in chunk_ids
    # ... and the audit scope reaches them again, permission checks intact.
    audit_fq = choke.enforce(
        RetrievalQuery(text="q", include_retracted=True),
        principal_for(tenant))
    chunk_ids = [r["chunk_id"] for r in
                 choke.read(audit_fq, DENSE_RETRIEVE_SQL, params)]
    assert site.child.id in chunk_ids


# ------------------------------------------------------------ pending gate --
def test_retracted_documents_pending_facts_do_not_promote(
        pipeline, store, tenant):
    site = seed_doc(pipeline, store, tenant, native_id="DOC-A")
    subj = make_entity(tenant, "Pending Corp")
    store.upsert_entity(subj)
    store.stage_pending({}, [PendingFact(
        tenant_id=tenant, subject_ref=f"entity:{subj.id}",
        predicate="references", object_literal="Net-30",
        ontology_version=ONTOLOGY, source_document_id=site.doc.id,
        extractor="test", extractor_version="tp")])

    pipeline.tombstone_raw(tenant, SOURCE, site.native_id)
    assert pipeline.promote_pending(tenant) == []  # skipped, NOT mutated

    pipeline.revive_raw(tenant, SOURCE, site.native_id)
    promoted = pipeline.promote_pending(tenant)
    assert len(promoted) == 1  # promotable again — reversibility for free


# -------------------------------------------------------------- end to end --
def test_capture_tombstone_item_propagates_to_facts(
        pipeline, store, capture, catalog, tenant):
    """The full trigger chain: a source adapter's explicit tombstone item ->
    CaptureService -> pipeline propagation -> facts retracted -> serve path
    silent. No direct pipeline calls — this is the path production takes."""
    from knowledge_hub.interfaces import SourceAdapter, SourceItem

    site = seed_doc(pipeline, store, tenant, native_id="DOC-A")
    subj = make_entity(tenant, "End To End GmbH")
    store.upsert_entity(subj)
    fact_id = add_fact(store, tenant, subj.id, "references",
                       doc_id=site.doc.id)

    class DeleteFeed(SourceAdapter):
        source_system = SOURCE
        cursor_ordering = "opaque"

        def backfill(self, tenant_id: str,
                     resume_after=None) -> Iterator[SourceItem]:
            yield SourceItem(native_id=site.native_id, change="tombstone",
                             mtime=datetime.now(tz=timezone.utc), cursor="t1")

        def incremental(self, tenant_id: str,
                        cursor=None) -> Iterator[SourceItem]:
            return iter(())

    result = capture.run_source(tenant, DeleteFeed(f"del-{uuid.uuid4().hex[:8]}"))
    assert result.tombstoned == 1
    assert served(catalog, tenant, subj.id) == {}
    audit = served(catalog, tenant, subj.id, include_retracted=True)
    assert audit[fact_id].state is UncertaintyState.retracted
