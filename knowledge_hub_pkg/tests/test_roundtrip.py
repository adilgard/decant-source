"""Round-trip every model: write through the store/pipeline, read back, compare."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from conftest import unit_vec
from factories import ONTOLOGY, land_document, make_chunk, make_entity, make_raw, sha
from knowledge_hub.models import (
    ChunkLevel, DocType, Document, EntityAlias, EntityMention, Fact, PendingFact,
)


def test_raw_document_roundtrip(pipeline, store, tenant):
    raw = make_raw(
        tenant,
        source_acl={"groups": ["finance"]},
        captured_at=datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc),
    )
    raw_id = pipeline.ingest_raw(raw)

    got = store.get_raw_document(tenant, raw_id)
    assert got.tenant_id == tenant
    assert got.source_system == raw.source_system
    assert got.source_native_id == raw.source_native_id
    assert got.mime_type == "application/pdf"
    assert got.content_hash == raw.content_hash
    assert got.raw_uri == raw.raw_uri
    assert got.source_acl == {"groups": ["finance"]}
    assert got.captured_at == raw.captured_at
    assert got.status == "landed"
    assert got.version == 1
    assert got.ingested_at is not None


def test_document_roundtrip(pipeline, store, tenant):
    raw = make_raw(tenant)
    pipeline.ingest_raw(raw)
    doc = Document(
        tenant_id=tenant, raw_document_id=raw.id, doc_type=DocType.communication,
        title="Q2 kickoff thread", author="Ops Lead", thread_id="thr-42",
        source_timestamp=datetime(2026, 6, 30, 9, 30, tzinfo=timezone.utc),
        metadata={"channel": "#ops"},
    )
    doc_id = store.insert_document(doc)

    got = store.get_document(tenant, doc_id)
    assert got.doc_type is DocType.communication
    assert got.raw_document_id == raw.id
    assert got.title == "Q2 kickoff thread"
    assert got.author == "Ops Lead"
    assert got.thread_id == "thr-42"
    assert got.source_timestamp == doc.source_timestamp
    assert got.metadata == {"channel": "#ops"}
    assert got.ingested_at is not None


def test_chunk_roundtrip_parent_child(store, pipeline, tenant):
    doc = land_document(pipeline, store, tenant)
    parent = make_chunk(tenant, doc.id, level=ChunkLevel.parent, seq=0,
                        char_start=0, char_end=500)
    store.insert_chunks([parent])
    child = make_chunk(
        tenant, doc.id, level=ChunkLevel.child, seq=0,
        parent_chunk_id=parent.id,
        contextual_prefix="From the Q2 ops doc:",
        token_count=87, char_start=0, char_end=180,
        locator={"page": 3},
        speaker="ops-lead", event_time=datetime(2026, 6, 1, tzinfo=timezone.utc),
        embedding=unit_vec(7), embedding_model="bge-m3", embedding_version="v1",
    )
    (child_id,) = store.insert_chunks([child])

    got = store.get_chunk(tenant, child_id)
    assert got.parent_chunk_id == parent.id
    assert got.level is ChunkLevel.child
    assert got.content == child.content
    assert got.contextual_prefix == "From the Q2 ops doc:"
    assert got.token_count == 87
    assert (got.char_start, got.char_end) == (0, 180)
    assert got.locator == {"page": 3}
    assert got.speaker == "ops-lead"
    assert got.event_time == child.event_time
    assert got.embedding == pytest.approx(child.embedding, abs=1e-6)
    assert got.embedding_model == "bge-m3"

    # idempotent replay: same content hash -> same row, no duplicate
    (again_id,) = store.insert_chunks([make_chunk(
        tenant, doc.id, level=ChunkLevel.child, seq=0,
        content=child.content, content_hash=child.content_hash)])
    assert again_id == child_id


def test_entity_roundtrip_upsert_and_aliases(store, tenant):
    entity = make_entity(
        tenant, "Acme Corporation",
        attributes={"domain": "acme.com"},
        embedding=unit_vec(3), embedding_model="bge-m3",
        aliases=[EntityAlias(tenant_id=tenant, alias="Acme Corp", source="gmail",
                             confidence=0.9)],
    )
    entity_id = store.upsert_entity(entity)

    got = store.get_entity(tenant, entity_id)
    assert got.canonical_name == "Acme Corporation"
    assert got.entity_type == "Organization"
    assert got.attributes == {"domain": "acme.com"}
    assert got.ontology_version == ONTOLOGY
    assert got.embedding == pytest.approx(entity.embedding, abs=1e-6)
    assert [a.alias for a in got.aliases] == ["Acme Corp"]
    assert got.created_at is not None

    # update in place (id set) + alias re-observation is a no-op, new alias lands
    got.canonical_name = "Acme Corporation Inc."
    got.aliases = [EntityAlias(tenant_id=tenant, alias="Acme Corp"),
                   EntityAlias(tenant_id=tenant, alias="ACME")]
    assert store.upsert_entity(got) == entity_id
    got2 = store.get_entity(tenant, entity_id)
    assert got2.canonical_name == "Acme Corporation Inc."
    assert sorted(a.alias for a in got2.aliases) == ["ACME", "Acme Corp"]

    with pytest.raises(LookupError):
        store.upsert_entity(make_entity(tenant, "ghost").model_copy(update={"id": 999999}))


def test_fact_roundtrip_entity_and_literal(store, pipeline, tenant):
    doc = land_document(pipeline, store, tenant)
    acme = store.upsert_entity(make_entity(tenant, "Acme"))
    globex = store.upsert_entity(make_entity(tenant, "Globex"))

    rel = Fact(tenant_id=tenant, subject_entity_id=acme, predicate="owns",
               object_entity_id=globex, ontology_version=ONTOLOGY,
               source_document_id=doc.id, char_start=10, char_end=60,
               locator={"page": 1}, extractor="qwen3.6", extractor_version="0.1",
               confidence=0.83, attributes={"stake": "60%"})
    lit = Fact(tenant_id=tenant, subject_entity_id=acme, predicate="references",
               object_literal="Net-30", ontology_version=ONTOLOGY,
               source_document_id=doc.id, extractor="qwen3.6", extractor_version="0.1")
    rel_id, lit_id = store.write_facts([rel, lit])

    got = store.get_fact(tenant, rel_id)
    assert (got.subject_entity_id, got.predicate, got.object_entity_id) == (acme, "owns", globex)
    assert got.object_literal is None
    assert got.attributes == {"stake": "60%"}
    assert (got.char_start, got.char_end) == (10, 60)
    assert got.locator == {"page": 1}
    assert got.confidence == pytest.approx(0.83)
    assert got.serialized_lines is not None and not got.oversized

    got_lit = store.get_fact(tenant, lit_id)
    assert got_lit.object_entity_id is None
    assert got_lit.object_literal == "Net-30"

    # model mirrors the DB CHECKs
    with pytest.raises(ValidationError):
        Fact(tenant_id=tenant, subject_entity_id=acme, predicate="owns",
             ontology_version=ONTOLOGY, source_document_id=doc.id,
             extractor="x", extractor_version="1")  # no object
    with pytest.raises(ValidationError):
        Fact(tenant_id=tenant, subject_entity_id=acme, predicate="owns",
             object_entity_id=globex, ontology_version=ONTOLOGY,
             extractor="x", extractor_version="1")  # no provenance


def test_mention_roundtrip(store, pipeline, tenant):
    doc = land_document(pipeline, store, tenant)
    mention = EntityMention(
        tenant_id=tenant, surface_text="Acme Corp", entity_type="Organization",
        source_system="gmail", source_document_id=doc.id,
        char_start=5, char_end=14, locator={"para": 2},
        extracted_keys={"domain": "acme.com"}, context_embedding=unit_vec(11),
    )
    staged = store.stage_pending({"m1": mention}, [])
    mention_id = staged.mention_ids["m1"]

    got = store.get_mention(tenant, mention_id)
    assert got.surface_text == "Acme Corp"
    assert got.entity_type == "Organization"
    assert got.source_system == "gmail"
    assert (got.char_start, got.char_end) == (5, 14)
    assert got.locator == {"para": 2}
    assert got.extracted_keys == {"domain": "acme.com"}
    assert got.context_embedding == pytest.approx(mention.context_embedding, abs=1e-6)
    assert got.resolution_status == "pending"
    assert got.resolved_entity_id is None


def test_pending_fact_model_mirrors_checks(tenant):
    with pytest.raises(ValidationError):
        PendingFact(tenant_id=tenant, subject_ref="m1", predicate="owns",
                    ontology_version=ONTOLOGY, source_document_id=1,
                    extractor="x", extractor_version="1")  # no object
    with pytest.raises(ValidationError):
        PendingFact(tenant_id=tenant, subject_ref="m1", predicate="owns",
                    object_ref="m2", ontology_version=ONTOLOGY,
                    extractor="x", extractor_version="1")  # no provenance
