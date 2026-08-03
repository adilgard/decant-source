"""THE VERTICAL SLICE, end to end against the live stack: a folder of SOPs
-> capture (SeaweedFS WORM + outbox) -> parse/chunk/embed (Docling + bge-m3)
-> extract (qwen3.6 joint pass) -> resolve (tiered scorer) -> facts in
Postgres + the AGE graph, with match_candidates, review_queue rows, and
flywheel labels populated along the way.

Assertions are machinery invariants (every mention leaves 'pending';
promoted facts carry canonical ids and project into the graph; review
decisions apply and label), never specific extracted content — extraction
and resolution QUALITY are the benchmarks' questions.
"""
from __future__ import annotations

import uuid

import pytest

from factories import ONTOLOGY
from knowledge_hub.capture import CaptureService
from knowledge_hub.extraction import ExtractionService
from knowledge_hub.sources_fs import FilesystemSourceAdapter

# Two SOPs sharing an Organization by exact name (and nothing else that
# would corroborate): the second document's "QA Team" mention must find the
# first document's entity as a candidate and — Organization policy requires
# corroboration for name-only matches — land in review, giving the human
# decision path something real to decide.
SOP_ONE = b"""# Batch Release SOP

The QA Team owns the Batch Release SOP. The Batch Release SOP governs the \
batch release process. Contact Dana Reyes at \
dana.reyes@diversifiedbotanics.com with questions about this document.

## Records

The Batch Release SOP references the batch record log. Retain batch records \
for five years in the QA archive.
"""

SOP_TWO = b"""# Sanitation SOP

The QA Team owns the Sanitation SOP. The Sanitation SOP governs the \
sanitation process for the production floor.
"""


def rows(store, tenant, table, where="", params=()):
    with store.transaction(tenant) as conn:
        return conn.execute(
            f"SELECT * FROM {table} WHERE tenant_id = %s {where} ORDER BY id",
            (tenant, *params)).fetchall()


@pytest.fixture(scope="module")
def slice_case(store, pipeline, raw_store, dispatcher, parser, chunker,
               embedder, binding, llm_strategy, structured_strategy, grounder,
               extraction_dispatcher, scorer, tmp_path_factory):
    """Run the whole slice once; every test below reads its residue."""
    from knowledge_hub.processing import ProcessingService
    from knowledge_hub.resolution import ResolutionService

    tenant = f"t-{uuid.uuid4().hex[:12]}"
    root = tmp_path_factory.mktemp("slice-sops")
    for rel, content in (("sops/batch_release.md", SOP_ONE),
                         ("sops/sanitation.md", SOP_TWO)):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    capture = CaptureService(pipeline, raw_store, dispatcher)
    capture.registry.register(tenant, "fs-slice", "filesystem",
                              config={"data_track": "prose",
                                      "doc_type": "sop"})
    adapter = FilesystemSourceAdapter(source_ref="fs-slice", root=root)
    landed = capture.run_source(tenant, adapter, mode="backfill")
    assert landed.landed == 2

    processing = ProcessingService(
        pipeline, raw_store, parser, chunker, embedder,
        dispatcher=dispatcher, extraction_dispatcher=extraction_dispatcher)
    processed = processing.consume(tenant, limit=10)
    assert len(processed) == 2
    extraction = ExtractionService(
        pipeline, raw_store, binding, llm_strategy, structured_strategy,
        grounder, dispatcher=extraction_dispatcher)
    extracted = extraction.consume(tenant, limit=10)
    assert len(extracted) == 2
    assert all(s.status == "extracted" for s in extracted)
    assert sum(s.mentions for s in extracted) >= 2

    resolution = ResolutionService(pipeline, scorer, embedder)
    summary = resolution.sweep(tenant)
    return {"tenant": tenant, "summary": summary, "resolution": resolution}


def test_slice_resolves_every_mention_and_promotes(slice_case, store):
    tenant, summary = slice_case["tenant"], slice_case["summary"]
    mentions = rows(store, tenant, "entity_mentions")
    assert mentions and summary.swept == len(mentions)
    assert summary.errors == 0

    # Resolution is decisive: nothing stays pending after a sweep — every
    # mention is resolved (to an existing or new entity) or owned by review.
    assert {m["resolution_status"] for m in mentions} <= {"resolved", "review"}
    for m in mentions:
        if m["resolution_status"] == "resolved":
            assert m["resolved_entity_id"] is not None
            assert m["resolver_version"].startswith("tiered-")

    # One observability row per swept mention (Axis B's raw signal).
    decisions = rows(store, tenant, "resolution_decisions")
    assert len(decisions) == len(mentions)

    # Promotion rewrote refs to canonical ids for every fully-resolved fact;
    # facts whose mentions sit in review stay pending (resolution may lag,
    # reads never depend on it).
    pending = rows(store, tenant, "pending_facts")
    assert pending, "extraction should have staged facts"
    promoted = [p for p in pending if p["resolution_status"] == "promoted"]
    assert summary.promoted_facts and \
        len(promoted) == len(summary.promoted_facts)
    facts = {f["id"]: f for f in rows(store, tenant, "facts")}
    for p in promoted:
        fact = facts[p["promoted_fact_id"]]
        assert fact["subject_entity_id"] is not None
        assert fact["ontology_version"] == ONTOLOGY
        assert fact["extractor"] == "llm_joint"

    # Entities were seeded from mentions: embedding present -> future
    # mentions can block against them.
    entities = rows(store, tenant, "entities")
    assert entities
    assert all(e["embedding"] is not None for e in entities)


def test_slice_writes_no_graph_projection(slice_case, store):
    """The AGE projection is RETIRED (BP9): the full slice runs source ->
    facts -> serve entirely relationally — promoted entity-entity facts
    exist as rows and produce ZERO graph edges."""
    import json
    tenant = slice_case["tenant"]
    entity_facts = [f for f in rows(store, tenant, "facts")
                    if f["object_entity_id"] is not None]
    assert entity_facts  # the slice did promote relationships — relationally
    ((val,),) = store.run_cypher(
        tenant,
        f"MATCH ()-[r:REL {{tenant_id: '{tenant}'}}]->() RETURN count(r)")
    assert json.loads(str(val)) == 0


def test_slice_surfaces_cross_document_match_for_review(slice_case, store):
    """The second document's 'QA Team' found the first document's entity:
    a high name-only Organization match without corroboration -> review, in
    both the pair log and the unified review queue."""
    tenant = slice_case["tenant"]
    candidates = rows(store, tenant, "match_candidates")
    assert candidates, "cross-document repetition must produce scored pairs"
    review_rows = [c for c in candidates if c["decision"] == "review"]
    assert review_rows, "the repeated Organization should await review"

    with store.transaction(tenant) as conn:
        queue = conn.execute(
            "SELECT kind, ref_id FROM review_queue WHERE tenant_id = %s",
            (tenant,)).fetchall()
    kinds = {q["kind"] for q in queue}
    assert "match" in kinds
    review_mentions = [m for m in rows(store, tenant, "entity_mentions")
                       if m["resolution_status"] == "review"]
    if review_mentions:
        assert "mention" in kinds


def test_slice_human_decision_closes_flywheel_loop(slice_case, store):
    """Approving one review pair resolves its mention and writes the
    human_review label — the gold set grows as the pilot runs."""
    tenant, resolution = slice_case["tenant"], slice_case["resolution"]
    mention_reviews = [
        c for c in rows(store, tenant, "match_candidates")
        if c["decision"] == "review" and c["left_type"] == "mention"]
    assert mention_reviews
    pick = mention_reviews[0]

    resolution.decide_match(tenant, pick["id"], same=True, reviewer="operator")

    mention = store.get_mention(tenant, pick["left_id"])
    assert mention.resolution_status == "resolved"
    assert mention.resolved_entity_id == pick["right_id"]
    labels = rows(store, tenant, "labels")
    assert any(l["label_type"] == "er_match"
               and l["source"] == "human_review" for l in labels)

    # ...and the newly resolved mention unblocks its facts on the next pass.
    promoted_again = resolution.pipeline.promote_pending(tenant)
    for fact_id in promoted_again:
        assert store.get_fact(tenant, fact_id) is not None


def test_slice_is_idempotent_under_resweep(slice_case, store):
    tenant, resolution = slice_case["tenant"], slice_case["resolution"]
    before = (len(rows(store, tenant, "facts")),
              len(rows(store, tenant, "entities")),
              len(rows(store, tenant, "labels")))
    summary = resolution.sweep(tenant)
    # Only mentions still awaiting review remain unswept; nothing new lands.
    assert summary.swept == 0
    assert summary.promoted_facts == []
    after = (len(rows(store, tenant, "facts")),
             len(rows(store, tenant, "entities")),
             len(rows(store, tenant, "labels")))
    assert before == after
