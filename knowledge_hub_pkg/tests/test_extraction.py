"""Extraction stage end-to-end, against the real stack and LIVE qwen3.6 —
no mocks.

Prose SOPs land through the real capture path, run Stage B (Docling + live
bge-m3), get handed to the extraction_queue by the wired ProcessingService,
and are consumed by ExtractionService with the live qwen3.6 joint strategy.
Assertions check persisted rows (pending_facts, entity_mentions,
quarantined_extractions, extraction_runs, review_queue), not in-memory
objects.

Live-LLM tests assert MACHINERY properties (staged refs are mention-refs,
predicates are ontology-bound, envelopes are stamped, runs are recorded) —
never specific fact content beyond strongly-cued smoke checks. Deterministic
paths (conformance, grounding, structured map) are driven synthetically so
their assertions are exact. Passing here means the machinery works; whether
the model extracts the RIGHT facts is the benchmark's question.
"""
from __future__ import annotations

import re
import uuid

import pytest

from knowledge_hub.capture import CaptureService
from knowledge_hub.extraction import ExtractionService
from knowledge_hub.extraction_llm import _OutEntity, _OutFact, _Output
from knowledge_hub.grounding import SpanGrounder, find_span
from knowledge_hub.interfaces import ExtractionStats, ExtractionUnit
from knowledge_hub.models import PROSE_TRACK, RawDocument
from knowledge_hub.sources_fs import FilesystemSourceAdapter

MENTION_REF = re.compile(r"^mention:\d+$")

# ---------------------------------------------------------------------------
# Test documents. The SOP is written with blatant, ontology-shaped
# assertions — that is machinery smoke, not extraction tuning: the tests on
# it assert staging/envelope/grounding properties, not that specific tuples
# came out.
# ---------------------------------------------------------------------------
SOP_MD = b"""# Solvent Handling SOP

The QA Team owns the Solvent Handling SOP. The Solvent Handling SOP governs \
the solvent storage process. Contact Dana Reyes at \
dana.reyes@diversifiedbotanics.com with questions about this document.

## Records

Retain solvent logs for three years in the QA records cabinet. The Solvent \
Handling SOP references the cleaning log. Logs are quality records and may \
not be discarded early.
"""

COREF_MD = b"""# Incident Review SOP

Dr. Elena Marsh owns the Incident Review SOP. The Incident Review SOP \
governs the incident review process.

## Escalation

She owns the incident escalation checklist. Any deviation goes to the \
maintenance lead within fifteen minutes.
"""

CSV_BYTES = (b"asset_id,asset_name,site,status\n"
             b"A-1,Mixer M-3,Building A,ok\n"
             b"A-2,Labeler L-1,Building B,maintenance\n")

STRUCTURED_MAP = {
    "entity_type": "Asset",
    "subject_column": "asset_name",
    "key_columns": {"asset_id": "asset_id"},
    "columns": {
        # part_of is ontology-bound; has_status is deliberately NOT — it must
        # land in quarantine (the signal that grows the ontology), never in
        # facts.
        "site": {"predicate": "part_of", "object_entity_type": "Location"},
        "status": {"predicate": "has_status"},
    },
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def land_file(store, pipeline, raw_store, dispatcher, tenant, rel, content,
              config=None, source_ref="fs-extract"):
    """Land one file through the REAL capture path; returns raw_documents.id."""
    capture = CaptureService(pipeline, raw_store, dispatcher)
    if config is not None:
        capture.registry.register(tenant, source_ref, "filesystem",
                                  config=config)
    root = land_file.root
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    adapter = FilesystemSourceAdapter(source_ref=source_ref, root=root)
    result = capture.run_source(tenant, adapter, mode="backfill")
    assert result.landed + result.replayed >= 1
    with store.transaction(tenant) as conn:
        row = conn.execute(
            "SELECT id FROM raw_documents"
            " WHERE tenant_id = %s AND source_native_id = %s"
            " ORDER BY version DESC LIMIT 1",
            (tenant, rel)).fetchone()
    return row["id"]


def rows(store, tenant, table, where="", params=()):
    with store.transaction(tenant) as conn:
        return conn.execute(
            f"SELECT * FROM {table} WHERE tenant_id = %s {where} ORDER BY id",
            (tenant, *params)).fetchall()


def run_pipeline(store, full_processing, extraction, tenant):
    """Consume the dispatch queue (Stage B) then the extraction queue."""
    processed = full_processing.consume(tenant, limit=10)
    extracted = extraction.consume(tenant, limit=10)
    return processed, extracted


# ---------------------------------------------------------------------------
# the landed + processed + extracted SOP every read-only test shares
# (module-scoped: docling + live bge-m3 + live qwen3.6 run once)
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def sop_case(store, pipeline, raw_store, dispatcher, parser, chunker,
             embedder, binding, llm_strategy, structured_strategy, grounder,
             extraction_dispatcher, tmp_path_factory):
    from knowledge_hub.processing import ProcessingService
    tenant = f"t-{uuid.uuid4().hex[:12]}"
    land_file.root = tmp_path_factory.mktemp("extract-sops")
    raw_id = land_file(
        store, pipeline, raw_store, dispatcher, tenant,
        "sops/solvent_handling.md", SOP_MD,
        config={"data_track": "prose", "doc_type": "sop"})
    processing = ProcessingService(
        pipeline, raw_store, parser, chunker, embedder,
        dispatcher=dispatcher, extraction_dispatcher=extraction_dispatcher)
    extraction = ExtractionService(
        pipeline, raw_store, binding, llm_strategy, structured_strategy,
        grounder, dispatcher=extraction_dispatcher)

    processed = processing.consume(tenant, limit=10)
    assert len(processed) == 1 and processed[0].status == "processed"
    extracted = extraction.consume(tenant, limit=10)
    assert len(extracted) == 1, f"expected one extraction delivery: {extracted}"
    return {"tenant": tenant, "raw_id": raw_id, "summary": extracted[0],
            "document_id": processed[0].document_id, "service": extraction}


# ---------------------------------------------------------------------------
# 1. an SOP parent -> conformant tuples staged into pending_facts with
#    mention-refs, tenant-scoped, fully enveloped — and NOT in facts
# ---------------------------------------------------------------------------
def test_sop_parents_stage_conformant_pending_facts(sop_case, store, binding,
                                                    llm_strategy):
    tenant, summary = sop_case["tenant"], sop_case["summary"]
    assert summary.status == "extracted"
    assert summary.facts >= 1, "the machinery should stage at least one fact"

    parents = rows(store, tenant, "chunks", "AND level = 'parent'")
    parent_ids = {p["id"] for p in parents}
    pending = rows(store, tenant, "pending_facts")
    assert pending, "facts must stage in pending_facts"
    for f in pending:
        # Pre-resolution refs, never entity ids (the resolver promotes later).
        assert MENTION_REF.match(f["subject_ref"])
        assert f["object_ref"] is None or MENTION_REF.match(f["object_ref"])
        assert f["resolution_status"] == "pending"
        # Ontology conformance + the envelope.
        assert binding.is_predicate(f["predicate"])
        assert f["ontology_version"] == "baseline-0.1"
        assert f["extractor"] == "llm_joint"
        assert f["extractor_version"] == llm_strategy.version
        assert f["extractor_version"].startswith("qwen3.6@")
        assert f["confidence"] is not None
        assert f["security_label_id"] is None  # inherited: source had none
        # Provenance: chunk-anchored, tenant-scoped.
        assert f["source_chunk_id"] in parent_ids
        assert f["grounding"] in ("pass", "span_missing", "components_missing")
        assert f["serialized_lines"] and f["serialized_lines"] > 0

    # Grounded facts carry real char offsets into the document text.
    grounded = [f for f in pending if f["grounding"] == "pass"]
    assert grounded, "at least one fact should ground cleanly"
    for f in grounded:
        assert f["char_start"] is not None and f["char_end"] > f["char_start"]
        assert (f["attributes"] or {}).get("evidence")

    # Mentions staged alongside, same tenant, pending resolution.
    mentions = rows(store, tenant, "entity_mentions")
    assert mentions
    for m in mentions:
        assert m["resolution_status"] == "pending"
        assert binding.is_entity_type(m["entity_type"])
        assert m["source_system"] == "filesystem"

    # NOTHING went into facts — staging is pre-resolution by design.
    assert rows(store, tenant, "facts") == []

    # The landing record moved on: parsed -> extracted.
    assert store.get_raw_document(tenant, sop_case["raw_id"]).status == "extracted"


# ---------------------------------------------------------------------------
# 2. observability: one ok run per parent with token/wall numbers
# ---------------------------------------------------------------------------
def test_extraction_runs_record_observability(sop_case, store, binding,
                                              llm_strategy):
    tenant = sop_case["tenant"]
    parents = rows(store, tenant, "chunks", "AND level = 'parent'")
    runs = rows(store, tenant, "extraction_runs")
    assert len(runs) == len(parents) == sop_case["summary"].units
    by_chunk = {r["source_chunk_id"]: r for r in runs}
    for p in parents:
        run = by_chunk[p["id"]]
        assert run["status"] == "ok"
        assert run["strategy"] == "llm_joint"
        assert run["extractor_version"] == llm_strategy.version
        assert run["ontology_version"] == binding.version
        assert run["unit_hash"] == p["content_hash"]
        assert run["prompt_tokens"] > 0 and run["output_tokens"] > 0
        assert run["wall_ms"] > 0
        assert run["repairs"] <= 1  # the cap is policy, not luck


# ---------------------------------------------------------------------------
# 3. deterministic conformance: an off-ontology predicate / entity type
#    quarantines (with the raw output) and reaches the review feeder;
#    it never becomes a fact
# ---------------------------------------------------------------------------
@pytest.fixture()
def mini_doc(store, pipeline, tenant):
    """A persisted document + one parent chunk, built directly through
    Prompt 1 persistence (no Docling/LLM) for synthetic-output tests."""
    from knowledge_hub.models import Chunk, ChunkLevel, DocType, Document
    text = ("The reactor R-2 was calibrated by Acme Instruments. "
            "The QA Team owns the calibration log.")
    raw = RawDocument(tenant_id=tenant, source_system="test",
                      content_hash=f"mini-{tenant}", raw_uri="test://mini")
    pipeline.ingest_raw(raw)
    doc = Document(tenant_id=tenant, raw_document_id=raw.id,
                   doc_type=DocType.sop, title="Mini SOP",
                   metadata={"data_track": PROSE_TRACK})
    store.insert_document(doc)
    chunk = Chunk(tenant_id=tenant, document_id=doc.id,
                  level=ChunkLevel.parent, seq=0, content=text,
                  content_hash=f"mini-parent-{tenant}", char_start=0,
                  char_end=len(text))
    store.insert_chunks([chunk])
    return {"raw": raw, "document": doc, "chunk": chunk, "text": text}


def synthetic_output():
    return _Output(
        entities=[
            _OutEntity(key="n1", name="reactor R-2", type="Gadget"),  # unbound type
            _OutEntity(key="n2", name="Acme Instruments", type="Organization"),
            _OutEntity(key="n3", name="QA Team", type="Organization"),
            _OutEntity(key="n4", name="the calibration log", type="Document"),
        ],
        facts=[
            _OutFact(subject="n1", predicate="calibrated_by", object="n2",
                     evidence="The reactor R-2 was calibrated by Acme "
                              "Instruments.", confidence=0.95),
            # 'log owned by QA Team' — the alias must normalize to owns AND
            # swap the arguments so the owner becomes the subject.
            _OutFact(subject="n4", predicate="owned by", object="n3",
                     evidence="The QA Team owns the calibration log.",
                     confidence=1.0),
        ])


def test_off_ontology_quarantines_not_facts(mini_doc, extraction,
                                            llm_strategy, store, tenant):
    unit = ExtractionUnit(document=mini_doc["document"], source_system="test",
                          chunk=mini_doc["chunk"], text=mini_doc["text"])
    result = llm_strategy._conform(unit, synthetic_output(), ExtractionStats())

    # The unbound entity type AND the fact leaning on it both quarantined;
    # the unbound predicate never reached the fact list either way.
    reasons = sorted(q.reason for q in result.quarantined)
    assert "unbound_entity_type" in reasons
    assert len(result.facts) == 1  # only the ownership fact survived

    # 'owned by' normalized to owns WITH the arguments swapped: the owner
    # (QA Team) is now the subject, the log the object.
    fact = result.facts[0]
    assert fact.predicate == "owns"
    assert (fact.subject_key, fact.object_key) == ("n3", "n4")

    from knowledge_hub.extraction import _DocDigest, ExtractSummary
    summary = ExtractSummary(tenant_id=tenant, raw_document_id=mini_doc["raw"].id,
                             document_id=mini_doc["document"].id,
                             status="extracted", units=1)
    extraction._finalize(unit, result, _DocDigest(),
                         mini_doc["chunk"].content_hash, llm_strategy,
                         extraction.binding, summary)

    quarantined = rows(store, tenant, "quarantined_extractions")
    assert {q["reason"] for q in quarantined} >= {"unbound_entity_type"}
    for q in quarantined:
        assert q["status"] == "open"
        assert q["raw_output"], "the raw model output must be preserved"
        assert q["extractor_version"] == llm_strategy.version

    # ...and they surfaced in the unified review queue as 'quarantine' items.
    queue = rows(store, tenant, "quarantined_extractions")
    with store.transaction(tenant) as conn:
        feeder = conn.execute(
            "SELECT ref_id FROM review_queue"
            " WHERE tenant_id = %s AND kind = 'quarantine'",
            (tenant,)).fetchall()
    assert {r["ref_id"] for r in feeder} == {q["id"] for q in queue}

    # No off-ontology predicate ever reached pending_facts.
    for f in rows(store, tenant, "pending_facts"):
        assert f["predicate"] not in ("calibrated_by", "has_status")


def test_llm_unbound_predicate_quarantines(mini_doc, llm_strategy):
    unit = ExtractionUnit(document=mini_doc["document"], source_system="test",
                          chunk=mini_doc["chunk"], text=mini_doc["text"])
    out = _Output(
        entities=[_OutEntity(key="n1", name="Acme Instruments",
                             type="Organization"),
                  _OutEntity(key="n2", name="reactor room", type="Location")],
        facts=[_OutFact(subject="n1", predicate="certified_by", object="n2",
                        evidence="irrelevant", confidence=0.9),
               # deterministically junk: X --rel--> X
               _OutFact(subject="n1", predicate="owns", object="n1",
                        evidence="irrelevant", confidence=0.9)])
    result = llm_strategy._conform(unit, out, ExtractionStats())
    assert result.facts == []
    assert sorted(q.reason for q in result.quarantined) == \
        ["unbound_predicate", "validation_failure"]
    by_reason = {q.reason: q for q in result.quarantined}
    assert by_reason["unbound_predicate"].detail == "certified_by"
    assert by_reason["unbound_predicate"].raw_output["predicate"] == "certified_by"
    assert "self-referential" in by_reason["validation_failure"].detail


# ---------------------------------------------------------------------------
# 4. grounding: a span that doesn't contain its components -> confidence
#    lowered + flagged for review (never hard-rejected)
# ---------------------------------------------------------------------------
def test_grounder_verdicts_are_deterministic():
    g = SpanGrounder()
    text = "The QA Team owns the log.\nRetain records for three years."
    exact = g.ground("The QA Team owns the log.", ["QA Team", "the log"], text)
    assert exact.status == "pass" and text[exact.char_start:exact.char_end] \
        == "The QA Team owns the log."
    fuzzy = g.ground("the qa  team OWNS the log", ["QA Team"], text,
                     base_offset=100)
    assert fuzzy.status == "pass" and fuzzy.char_start >= 100
    assert g.ground("This sentence is not there.", [], text).status \
        == "span_missing"
    partial = g.ground("Retain records for three years", ["QA Team"], text)
    assert partial.status == "components_missing"
    assert find_span("QA TEAM", text) == (4, 11)


def test_grounding_failure_lowers_confidence_and_flags(mini_doc, extraction,
                                                       llm_strategy, store,
                                                       tenant):
    unit = ExtractionUnit(document=mini_doc["document"], source_system="test",
                          chunk=mini_doc["chunk"], text=mini_doc["text"])
    out = _Output(
        entities=[_OutEntity(key="n1", name="QA Team", type="Organization"),
                  _OutEntity(key="n2", name="the calibration log",
                             type="Document")],
        facts=[
            # evidence exists but does NOT contain the object surface
            _OutFact(subject="n1", predicate="owns", object="n2",
                     evidence="The reactor R-2 was calibrated by Acme "
                              "Instruments.", confidence=0.8),
            # evidence not in the text at all
            _OutFact(subject="n1", predicate="governs", object="n2",
                     evidence="Entirely fabricated sentence.",
                     confidence=0.6),
        ])
    result = llm_strategy._conform(unit, out, ExtractionStats())
    assert len(result.facts) == 2

    from knowledge_hub.extraction import _DocDigest, ExtractSummary
    summary = ExtractSummary(tenant_id=tenant, raw_document_id=mini_doc["raw"].id,
                             document_id=mini_doc["document"].id,
                             status="extracted", units=1)
    extraction._finalize(unit, result, _DocDigest(),
                         f"ground-{tenant}", llm_strategy,
                         extraction.binding, summary)
    assert summary.grounding_flags == 2

    flagged = {f["grounding"]: f for f in rows(store, tenant, "pending_facts")}
    missing_comp = flagged["components_missing"]
    assert missing_comp["confidence"] == pytest.approx(0.8 * 0.5)
    assert missing_comp["needs_review"] is True
    assert missing_comp["char_start"] is not None  # span itself was found
    missing_span = flagged["span_missing"]
    assert missing_span["confidence"] == pytest.approx(0.6 * 0.5)
    assert missing_span["needs_review"] is True
    assert missing_span["char_start"] is None

    with store.transaction(tenant) as conn:
        feeder = conn.execute(
            "SELECT ref_id, context FROM review_queue"
            " WHERE tenant_id = %s AND kind = 'pending_fact'",
            (tenant,)).fetchall()
    assert {r["ref_id"] for r in feeder} == \
        {missing_comp["id"], missing_span["id"]}


# ---------------------------------------------------------------------------
# 5. SoR rows -> deterministic facts with cell locators, no LLM call
# ---------------------------------------------------------------------------
def test_sor_rows_map_deterministically(store, pipeline, raw_store,
                                        dispatcher, full_processing,
                                        extraction, structured_strategy,
                                        binding, tenant, tmp_path):
    land_file.root = tmp_path
    raw_id = land_file(store, pipeline, raw_store, dispatcher, tenant,
                       "exports/assets.csv", CSV_BYTES,
                       config={"data_track": "structured",
                               "structured_map": STRUCTURED_MAP})
    processed, extracted = run_pipeline(store, full_processing, extraction,
                                        tenant)
    assert processed[0].status == "no_chunks"  # P3's router hook, consumed
    assert len(extracted) == 1 and extracted[0].status == "extracted"

    pending = rows(store, tenant, "pending_facts")
    assert len(pending) == 2  # one part_of per row; has_status quarantined
    for f in pending:
        assert f["predicate"] == "part_of"
        assert f["extractor"] == "structured_map"
        assert f["grounding"] == "construction"
        assert f["needs_review"] is False
        assert f["confidence"] == 1.0
        assert f["source_chunk_id"] is None  # no chunks exist by design
        assert f["locator"]["col"] == "site" and f["locator"]["row"] in (1, 2)
        assert MENTION_REF.match(f["subject_ref"])
        assert MENTION_REF.match(f["object_ref"])

    mentions = {m["surface_text"]: m for m in
                rows(store, tenant, "entity_mentions")}
    assert mentions["Mixer M-3"]["entity_type"] == "Asset"
    assert mentions["Mixer M-3"]["extracted_keys"] == {"asset_id": "A-1"}
    assert mentions["Building A"]["entity_type"] == "Location"

    # The unbound column quarantined ONCE (the mapping is wrong, not the rows).
    quarantined = rows(store, tenant, "quarantined_extractions")
    assert len(quarantined) == 1
    assert quarantined[0]["reason"] == "unbound_predicate"
    assert quarantined[0]["detail"] == "has_status"

    # No LLM was involved: the run row is the structured strategy's, with no
    # model tokens to report.
    runs = rows(store, tenant, "extraction_runs")
    assert len(runs) == 1
    assert runs[0]["strategy"] == "structured_map"
    assert runs[0]["extractor_version"] == structured_strategy.version
    assert runs[0]["prompt_tokens"] is None and runs[0]["output_tokens"] is None
    assert runs[0]["source_chunk_id"] is None


# ---------------------------------------------------------------------------
# 6. idempotency + poison redelivery
# ---------------------------------------------------------------------------
def test_reextraction_is_idempotent_by_content_hash(sop_case, store):
    tenant, service = sop_case["tenant"], sop_case["service"]
    before_facts = [r["id"] for r in rows(store, tenant, "pending_facts")]
    before_mentions = [r["id"] for r in rows(store, tenant, "entity_mentions")]
    before_runs = [r["id"] for r in rows(store, tenant, "extraction_runs")]

    again = service.extract(tenant, sop_case["raw_id"])

    assert again.status == "replayed"
    assert again.units_replayed == again.units == sop_case["summary"].units
    assert [r["id"] for r in rows(store, tenant, "pending_facts")] == before_facts
    assert [r["id"] for r in rows(store, tenant, "entity_mentions")] == before_mentions
    assert [r["id"] for r in rows(store, tenant, "extraction_runs")] == before_runs


def test_poison_document_nacks_and_redelivers(extraction, pipeline,
                                              extraction_dispatcher, tenant):
    # A landed raw doc that processing never touched: no documents row, so
    # extraction cannot proceed — the message must survive, not vanish.
    raw = RawDocument(tenant_id=tenant, source_system="test",
                      content_hash=f"poison-{tenant}",
                      raw_uri="test://poison")
    pipeline.ingest_raw(raw)
    extraction_dispatcher.dispatch(tenant, raw.id)

    results = extraction.consume(tenant, limit=10)

    assert results == []  # the only delivery failed -> nothing extracted
    message = extraction_dispatcher.pending_for(tenant, raw.id)
    assert message.status == "queued"          # nacked: lease redelivers
    assert message.attempts == 1
    assert "ExtractionError" in message.last_error


# ---------------------------------------------------------------------------
# 7. coreference: a pronoun subject in a later parent resolves to the entity
#    established earlier (the digest works)
# ---------------------------------------------------------------------------
def test_coreference_resolves_to_earlier_mention(store, pipeline, raw_store,
                                                 dispatcher, full_processing,
                                                 extraction, tenant, tmp_path):
    land_file.root = tmp_path
    land_file(store, pipeline, raw_store, dispatcher, tenant,
              "sops/incident_review.md", COREF_MD,
              config={"data_track": "prose", "doc_type": "sop"})
    processed, extracted = run_pipeline(store, full_processing, extraction,
                                        tenant)
    assert extracted[0].status == "extracted"

    parents = rows(store, tenant, "chunks", "AND level = 'parent'")
    assert len(parents) >= 2
    first_parent, later_parents = parents[0], {p["id"] for p in parents[1:]}

    mentions = {m["id"]: m for m in rows(store, tenant, "entity_mentions")}
    # The digest rule: pronouns never become entities.
    for m in mentions.values():
        assert m["surface_text"].strip().lower() not in ("she", "her", "he",
                                                         "it", "they")

    # Dr. Elena Marsh was established in the FIRST parent...
    marsh = [m for m in mentions.values() if "Marsh" in m["surface_text"]]
    assert marsh and all(m["source_chunk_id"] == first_parent["id"]
                         for m in marsh)
    marsh_refs = {f"mention:{m['id']}" for m in marsh}

    # ...and at least one fact extracted from a LATER parent (the "She ..."
    # section) has her as its subject — coreference resolved in-pass.
    later_facts = [f for f in rows(store, tenant, "pending_facts")
                   if f["source_chunk_id"] in later_parents]
    assert later_facts, "the later parent should assert something"
    assert any(f["subject_ref"] in marsh_refs for f in later_facts), (
        "a fact from the 'She ...' parent should resolve its subject to the "
        f"Dr. Elena Marsh mention: {[(f['subject_ref'], f['predicate']) for f in later_facts]}")
