"""Extraction handoff (stage_pending), ref rewriting/promotion, review queue."""
from __future__ import annotations

import json

import pytest

from conftest import unit_vec
from factories import ONTOLOGY, land_document, make_entity
from knowledge_hub.models import EntityMention, Fact, PendingFact


def _mention(tenant, doc_id, text, axis, entity_type="Organization"):
    return EntityMention(
        tenant_id=tenant, surface_text=text, entity_type=entity_type,
        source_system="gmail", source_document_id=doc_id,
        context_embedding=unit_vec(axis))


def _pending(tenant, doc_id, subject_ref, predicate, **kw):
    return PendingFact(
        tenant_id=tenant, subject_ref=subject_ref, predicate=predicate,
        ontology_version=ONTOLOGY, source_document_id=doc_id,
        extractor="qwen3.6", extractor_version="0.1", **kw)


def _resolve(store, tenant, mention_id, entity_id):
    with store.transaction(tenant) as conn:
        conn.execute(
            "UPDATE entity_mentions SET resolved_entity_id = %s,"
            " resolution_status = 'resolved', resolved_at = now()"
            " WHERE tenant_id = %s AND id = %s",
            (entity_id, tenant, mention_id))


def test_stage_pending_rewrites_mention_keys(store, pipeline, tenant):
    doc = land_document(pipeline, store, tenant)
    staged = store.stage_pending(
        {"m1": _mention(tenant, doc.id, "Acme Corp", 0),
         "m2": _mention(tenant, doc.id, "Globex", 1)},
        [_pending(tenant, doc.id, "m1", "owns", object_ref="m2"),
         _pending(tenant, doc.id, "m1", "references", object_literal="Net-30")],
    )

    assert set(staged.mention_ids) == {"m1", "m2"}
    assert len(staged.pending_fact_ids) == 2

    rel = store.get_pending_fact(tenant, staged.pending_fact_ids[0])
    assert rel.subject_ref == f"mention:{staged.mention_ids['m1']}"
    assert rel.object_ref == f"mention:{staged.mention_ids['m2']}"
    assert rel.resolution_status == "pending"

    lit = store.get_pending_fact(tenant, staged.pending_fact_ids[1])
    assert lit.object_ref is None and lit.object_literal == "Net-30"

    # unknown key -> hard error, nothing silently staged
    with pytest.raises(KeyError):
        store.stage_pending(
            {"m1": _mention(tenant, doc.id, "X", 2)},
            [_pending(tenant, doc.id, "nope", "owns", object_ref="m1")])


def test_rewrite_refs_and_promotion(store, pipeline, tenant):
    doc = land_document(pipeline, store, tenant)
    staged = store.stage_pending(
        {"m1": _mention(tenant, doc.id, "Acme Corp", 0),
         "m2": _mention(tenant, doc.id, "Globex", 1)},
        [_pending(tenant, doc.id, "m1", "owns", object_ref="m2"),
         _pending(tenant, doc.id, "m1", "references", object_literal="Net-30")],
    )
    m1, m2 = staged.mention_ids["m1"], staged.mention_ids["m2"]

    # nothing resolved yet -> nothing promotes
    assert pipeline.promote_pending(tenant) == []

    acme = store.upsert_entity(make_entity(tenant, "Acme Corporation"))
    _resolve(store, tenant, m1, acme)

    # subject resolved: the literal fact promotes; the relationship still waits on m2
    promoted = pipeline.promote_pending(tenant)
    assert len(promoted) == 1
    lit_fact = store.get_fact(tenant, promoted[0])
    assert (lit_fact.subject_entity_id, lit_fact.object_literal) == (acme, "Net-30")

    globex = store.upsert_entity(make_entity(tenant, "Globex Inc"))
    _resolve(store, tenant, m2, globex)
    (rel_fact_id,) = pipeline.promote_pending(tenant)
    rel_fact = store.get_fact(tenant, rel_fact_id)
    assert (rel_fact.subject_entity_id, rel_fact.predicate,
            rel_fact.object_entity_id) == (acme, "owns", globex)

    # staging rows are marked, and re-promotion is a no-op
    for pid in staged.pending_fact_ids:
        row = store.get_pending_fact(tenant, pid)
        assert row.resolution_status == "promoted"
        assert row.promoted_fact_id is not None
    assert pipeline.promote_pending(tenant) == []

    # the AGE projection is retired (BP9): promotion wrote NO graph edge
    ((val,),) = store.run_cypher(
        tenant,
        f"MATCH (:Entity {{id: {acme}}})-[r:REL {{fact_id: {rel_fact_id}}}]->"
        f"(:Entity {{id: {globex}}}) RETURN count(r)")
    assert json.loads(str(val)) == 0


def test_entity_refs_pass_through(store, pipeline, tenant):
    doc = land_document(pipeline, store, tenant)
    acme = store.upsert_entity(make_entity(tenant, "Acme"))
    globex = store.upsert_entity(make_entity(tenant, "Globex"))

    staged = store.stage_pending({}, [_pending(
        tenant, doc.id, f"entity:{acme}", "owns", object_ref=f"entity:{globex}")])
    (fact_id,) = pipeline.promote_pending(tenant)
    fact = store.get_fact(tenant, fact_id)
    assert (fact.subject_entity_id, fact.object_entity_id) == (acme, globex)


def test_enqueue_review_feeds_the_view(store, pipeline, tenant):
    doc = land_document(pipeline, store, tenant)
    staged = store.stage_pending(
        {"m1": _mention(tenant, doc.id, "Ambiguous Ltd", 5)}, [])
    mention_id = staged.mention_ids["m1"]

    pipeline._enqueue_review(tenant, "mention", mention_id)

    with store.transaction(tenant) as conn:
        rows = conn.execute(
            "SELECT kind, ref_id FROM review_queue WHERE tenant_id = %s",
            (tenant,)).fetchall()
    assert {(r["kind"], r["ref_id"]) for r in rows} == {("mention", mention_id)}
    assert store.get_mention(tenant, mention_id).resolution_status == "review"

    with pytest.raises(LookupError):
        pipeline._enqueue_review(tenant, "mention", 999999)
    with pytest.raises(ValueError):
        pipeline._enqueue_review(tenant, "nonsense", mention_id)


def test_oversized_fact_soft_alarm(store, pipeline, tenant):
    doc = land_document(pipeline, store, tenant)
    acme = store.upsert_entity(make_entity(tenant, "Acme"))

    bloated = Fact(
        tenant_id=tenant, subject_entity_id=acme, predicate="references",
        object_literal="a fact that is secretly a document",
        attributes={f"field_{i}": f"value {i}" for i in range(120)},
        ontology_version=ONTOLOGY, source_document_id=doc.id,
        extractor="test", extractor_version="1")
    (fact_id,) = store.write_facts([bloated])

    got = store.get_fact(tenant, fact_id)
    assert got.oversized  # written intact, flagged — not rejected
    assert got.serialized_lines > 70
    assert got.attributes["field_119"] == "value 119"

    with store.transaction(tenant) as conn:
        kinds = {r["kind"] for r in conn.execute(
            "SELECT kind FROM review_queue WHERE tenant_id = %s AND ref_id = %s",
            (tenant, fact_id)).fetchall()}
    assert "oversized_fact" in kinds
