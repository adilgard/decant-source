"""Resolution Stage D against the real stack — real Splink/DuckDB, real
pgvector blocking, real Postgres + AGE, live Ollama where a tier calls it.
No mocks.

Deterministic tiers (T0 keys, T1 Splink with the shipped priors, T1c
corroboration gates, merge/reversal mechanics, promotion) are driven
synthetically with hand-built mentions/entities and fixed embeddings, so
their assertions are exact. The one live-LLM test (gray-band adjudication)
asserts MACHINERY properties — the adjudicator ran, evidence was recorded,
under-merge bias held — never what the model decided.

Green here means the machinery is correct: tiers route, policy bands, under-
merge gates hold, merges reverse, labels land, facts promote. Whether the
resolver makes the RIGHT calls is the ER benchmark's question (Axis B, gold
set from the labels this stage emits) — the shipped thresholds and priors
are placeholders.
"""
from __future__ import annotations

import json

import pytest

from factories import ONTOLOGY, land_document, make_chunk, make_entity, sha
from conftest import mix_vec, unit_vec
from knowledge_hub.models import EntityAlias, EntityMention, Fact, PendingFact

RESOLVER = "tiered-"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def rows(store, tenant, table, where="", params=()):
    with store.transaction(tenant) as conn:
        return conn.execute(
            f"SELECT * FROM {table} WHERE tenant_id = %s {where} ORDER BY id",
            (tenant, *params)).fetchall()


def edge_count(store, tenant, fact_id, subject_id=None, object_id=None):
    """REL edges in the AGE projection for one fact, optionally pinned to
    exact endpoints."""
    src = f"(s:Entity {{id: {subject_id}, tenant_id: '{tenant}'}})" \
        if subject_id else "()"
    dst = f"(o:Entity {{id: {object_id}, tenant_id: '{tenant}'}})" \
        if object_id else "()"
    ((val,),) = store.run_cypher(
        tenant,
        f"MATCH {src}-[r:REL {{fact_id: {fact_id}, tenant_id: '{tenant}'}}]"
        f"->{dst} RETURN count(r)")
    return json.loads(str(val))


def doc_with_chunk(pipeline, store, tenant):
    doc = land_document(pipeline, store, tenant)
    chunk = make_chunk(tenant, doc.id, char_start=0, char_end=400,
                       content="Test parent content mentioning the entities "
                               "under resolution in this document.")
    store.insert_chunks([chunk])
    return doc, chunk


def stage_mention(store, doc, chunk, surface, entity_type, keys=None,
                  vec=None, **overrides):
    """Stage one mention through the real extraction handoff."""
    m = EntityMention(
        tenant_id=doc.tenant_id, surface_text=surface,
        entity_type=entity_type, source_system="test",
        source_document_id=doc.id, source_chunk_id=chunk.id if chunk else None,
        extracted_keys=keys or {}, context_embedding=vec, **overrides)
    store.stage_pending({"k": m}, [])
    return m


def entity_of(store, tenant, entity_id):
    e = store.get_entity(tenant, entity_id)
    assert e is not None
    return e


# ---------------------------------------------------------------------------
# 1. Tier 0: an exact strong-key match auto-resolves and writes the
#    deterministic-positive flywheel label
# ---------------------------------------------------------------------------
def test_tier0_key_match_resolves_and_labels(store, pipeline, resolution,
                                             scorer, tenant):
    doc, chunk = doc_with_chunk(pipeline, store, tenant)
    dana = store.upsert_entity(make_entity(
        tenant, "Dana Reyes", "Person",
        attributes={"email": "dana.reyes@diversifiedbotanics.com"},
        embedding=unit_vec(0), embedding_model="bge-m3"))
    # Different surface AND a deliberately different embedding: only the key
    # can be responsible for this match.
    m = stage_mention(store, doc, chunk, "D. Reyes", "Person",
                      keys={"email": "dana.reyes@diversifiedbotanics.com"},
                      vec=unit_vec(1))

    summary = resolution.sweep(tenant)

    assert summary.swept == 1 and summary.resolved == 1
    assert summary.by_tier == {"t0": 1}
    got = store.get_mention(tenant, m.id)
    assert got.resolution_status == "resolved"
    assert got.resolved_entity_id == dana
    assert got.resolver_version == scorer.version
    assert got.resolved_at is not None

    # The surface form became an earned alias.
    assert "D. Reyes" in {a.alias for a in entity_of(store, tenant, dana).aliases}

    # Pair-level log: deterministic_key, high band, applied as auto_merge.
    (mc,) = rows(store, tenant, "match_candidates")
    assert mc["left_type"] == "mention" and mc["left_id"] == m.id
    assert mc["right_id"] == dana
    assert mc["match_method"] == "deterministic_key"
    assert mc["band"] == "high" and mc["decision"] == "auto_merge"
    assert mc["features"]["key_overlap"] == {
        "email": "dana.reyes@diversifiedbotanics.com"}

    # Per-mention observability (the Axis-B signal).
    (d,) = rows(store, tenant, "resolution_decisions")
    assert d["mention_id"] == m.id and d["tier"] == "t0"
    assert d["method"] == "deterministic_key"
    assert d["decision"] == "resolved" and d["entity_id"] == dana
    assert d["resolver_version"].startswith(RESOLVER)
    assert d["match_candidate_id"] == mc["id"]

    # The free flywheel label: a deterministic positive, full authority.
    (label,) = rows(store, tenant, "labels")
    assert label["label_type"] == "er_match"
    assert label["source"] == "deterministic" and label["authority"] == 1.0
    assert label["payload"]["left"] == {"type": "mention", "id": m.id}
    assert label["payload"]["right"] == {"type": "entity", "id": dana}


# ---------------------------------------------------------------------------
# 2. Tier 0: the same strong key on TWO registry entities is a conflict ->
#    review (never a coin-flip merge) + the entity-entity pair is logged as
#    a merge candidate for a human
# ---------------------------------------------------------------------------
def test_tier0_key_conflict_routes_to_review(store, pipeline, resolution,
                                             tenant):
    doc, chunk = doc_with_chunk(pipeline, store, tenant)
    a = store.upsert_entity(make_entity(
        tenant, "Acme Corp", attributes={"domain": "acme.example"},
        embedding=unit_vec(2), embedding_model="bge-m3"))
    b = store.upsert_entity(make_entity(
        tenant, "Acme Corporation", attributes={"domain": "acme.example"},
        embedding=unit_vec(3), embedding_model="bge-m3"))
    m = stage_mention(store, doc, chunk, "Acme", "Organization",
                      keys={"domain": "acme.example"}, vec=unit_vec(2))

    summary = resolution.sweep(tenant)

    assert summary.review == 1 and summary.resolved == 0
    assert store.get_mention(tenant, m.id).resolution_status == "review"

    candidates = rows(store, tenant, "match_candidates")
    mention_rows = [c for c in candidates if c["left_type"] == "mention"]
    entity_rows = [c for c in candidates if c["left_type"] == "entity"]
    assert {c["right_id"] for c in mention_rows} == {a, b}
    assert all(c["decision"] == "review" for c in mention_rows)
    # The registry-duplicate pair surfaced for human merge review.
    (pair,) = entity_rows
    assert {pair["left_id"], pair["right_id"]} == {a, b}
    assert pair["decision"] == "review"
    assert pair["match_method"] == "deterministic_key"

    (d,) = rows(store, tenant, "resolution_decisions")
    assert d["decision"] == "review"
    assert d["features"]["reason"] == "key_conflict"

    # Both feeders visible in the unified review queue.
    with store.transaction(tenant) as conn:
        kinds = {r["kind"] for r in conn.execute(
            "SELECT kind FROM review_queue WHERE tenant_id = %s",
            (tenant,)).fetchall()}
    assert kinds == {"mention", "match"}


# ---------------------------------------------------------------------------
# 3. Tier 1 (Splink): a structured mention scores probabilistically and
#    bands against resolution_policy — an exact name alone (prior 0.01,
#    shipped placeholder m/u) lands in the GRAY band -> review
# ---------------------------------------------------------------------------
def test_tier1_splink_gray_band_routes_to_review(store, pipeline, resolution,
                                                 tenant):
    doc, chunk = doc_with_chunk(pipeline, store, tenant)
    zenith = store.upsert_entity(make_entity(
        tenant, "Zenith Widgets", embedding=unit_vec(4),
        embedding_model="bge-m3"))
    # An unseen email makes the mention Tier-1 eligible without giving Tier 0
    # anything to match; the name is identical, no other field agrees.
    m = stage_mention(store, doc, chunk, "Zenith Widgets", "Organization",
                      keys={"email": "sales@zenithwidgets.example",
                            "domain": "zenithwidgets.example"},
                      vec=unit_vec(4))

    resolution.sweep(tenant)

    got = store.get_mention(tenant, m.id)
    assert got.resolution_status == "review", \
        "exact-name-only under the shipped priors must NOT auto-merge"
    (d,) = rows(store, tenant, "resolution_decisions")
    assert d["tier"] == "t1" and d["method"] == "probabilistic"
    assert d["band"] == "gray" and d["decision"] == "review"
    # Organization policy: t_low 0.50 < score < t_high 0.95.
    assert 0.50 < d["score"] < 0.95

    (mc,) = [c for c in rows(store, tenant, "match_candidates")
             if c["right_id"] == zenith]
    assert mc["match_method"] == "probabilistic"
    # The Fellegi-Sunter evidence is recorded engine-neutrally.
    assert "match_weight" in mc["features"]
    assert mc["features"]["name_used"] == "Zenith Widgets"
    assert mc["features"]["key_overlap"] == {}
    assert mc["features"]["tier"] == "t1"


# ---------------------------------------------------------------------------
# 4. Tier 1 (Splink): name agreement + a shared key push the score over
#    t_high, and the key overlap satisfies the corroboration gate ->
#    auto-merge (NO ground-truth label: a probabilistic match is a model
#    opinion, not a labeled pair)
# ---------------------------------------------------------------------------
def test_tier1_high_band_with_key_overlap_auto_merges(store, pipeline,
                                                      resolution, tenant):
    doc, chunk = doc_with_chunk(pipeline, store, tenant)
    # domain is NOT a strong key for Person (everyone at the org shares it),
    # so this cannot short-circuit at Tier 0 — it must earn the merge
    # probabilistically.
    dana = store.upsert_entity(make_entity(
        tenant, "Dana Reyes", "Person",
        attributes={"domain": "diversifiedbotanics.com"},
        embedding=unit_vec(5), embedding_model="bge-m3"))
    m = stage_mention(store, doc, chunk, "Dana Reyes", "Person",
                      keys={"domain": "diversifiedbotanics.com"},
                      vec=unit_vec(5))

    summary = resolution.sweep(tenant)

    assert summary.resolved == 1 and summary.by_tier == {"t1": 1}
    got = store.get_mention(tenant, m.id)
    assert got.resolution_status == "resolved"
    assert got.resolved_entity_id == dana
    (d,) = rows(store, tenant, "resolution_decisions")
    assert d["tier"] == "t1" and d["band"] == "high"
    assert d["score"] >= 0.93  # Person t_high
    (mc,) = rows(store, tenant, "match_candidates")
    assert mc["features"]["key_overlap"] == {"domain": "diversifiedbotanics.com"}
    # Probabilistic merges are NOT ground truth — no label.
    assert rows(store, tenant, "labels") == []


# ---------------------------------------------------------------------------
# 5. Tier 1b + 1c: identifiers outrank names. A name-only high-band match on
#    a requires_corroboration type merges ONLY with a corroborating shared
#    edge; the same match without one goes to review (bias to under-merge).
# ---------------------------------------------------------------------------
def test_name_only_match_needs_graph_corroboration(store, pipeline,
                                                   resolution, tenant):
    # Registry: Dana reports_to the QA Team (the shared edge).
    dana = store.upsert_entity(make_entity(
        tenant, "Dana Reyes", "Person", embedding=unit_vec(6),
        embedding_model="bge-m3"))
    qa = store.upsert_entity(make_entity(
        tenant, "QA Team", attributes={"domain": "qa.example"},
        embedding=unit_vec(7), embedding_model="bge-m3"))
    doc0, _ = doc_with_chunk(pipeline, store, tenant)
    store.write_facts([Fact(
        tenant_id=tenant, subject_entity_id=dana, predicate="reports_to",
        object_entity_id=qa, ontology_version=ONTOLOGY,
        source_document_id=doc0.id, extractor="test",
        extractor_version="t1")])

    # Document B mentions the QA Team (Tier-0 resolvable via domain) and
    # then Dana by name only -> corroborated by the co-resolved QA Team edge.
    doc_b, chunk_b = doc_with_chunk(pipeline, store, tenant)
    stage_mention(store, doc_b, chunk_b, "QA Team", "Organization",
                  keys={"domain": "qa.example"}, vec=unit_vec(7))
    m_corr = stage_mention(store, doc_b, chunk_b, "Dana Reyes", "Person",
                           vec=unit_vec(6))
    # Document C mentions Dana by name only with NO co-resolved neighbors.
    doc_c, chunk_c = doc_with_chunk(pipeline, store, tenant)
    m_bare = stage_mention(store, doc_c, chunk_c, "Dana Reyes", "Person",
                           vec=unit_vec(6))

    resolution.sweep(tenant)

    corroborated = store.get_mention(tenant, m_corr.id)
    assert corroborated.resolution_status == "resolved"
    assert corroborated.resolved_entity_id == dana
    bare = store.get_mention(tenant, m_bare.id)
    assert bare.resolution_status == "review", \
        "a name-only Person match with no corroborating edge must NOT merge"

    decisions = {d["mention_id"]: d for d in
                 rows(store, tenant, "resolution_decisions")}
    d_corr, d_bare = decisions[m_corr.id], decisions[m_bare.id]
    assert d_corr["tier"] == "t1b" and d_corr["decision"] == "resolved"
    assert d_corr["features"]["corroboration"] == 1
    assert d_bare["band"] == "high" and d_bare["decision"] == "review"
    assert d_bare["features"]["corroboration"] == 0
    assert d_bare["features"]["reason"] == "needs_corroboration"


# ---------------------------------------------------------------------------
# 6. Tier 1b gray band: the LLM adjudicator runs on the ambiguous residual
#    (LIVE model). Machinery assertions only — whatever the model answered,
#    the evidence is recorded and under-merge bias holds.
# ---------------------------------------------------------------------------
def test_gray_band_llm_adjudication_records_evidence(store, pipeline,
                                                     resolution, tenant):
    doc, chunk = doc_with_chunk(pipeline, store, tenant)
    acme = store.upsert_entity(make_entity(
        tenant, "Acme Corporation", embedding=unit_vec(8),
        embedding_model="bge-m3"))
    # name_sim ("Acme Corp" vs "Acme Corporation") ~= 0.72, cosine ~= 0.97:
    # base ~= 0.85 -> gray for Organization (0.50, 0.95) -> adjudication.
    m = stage_mention(store, doc, chunk, "Acme Corp", "Organization",
                      vec=mix_vec(8, 9, 0.8, 0.2))

    resolution.sweep(tenant)

    (mc,) = [c for c in rows(store, tenant, "match_candidates")
             if c["right_id"] == acme]
    adj = mc["features"].get("adjudication")
    assert adj is not None, "gray band must be adjudicated"
    if not adj.get("error"):
        assert mc["match_method"] == "llm"
        assert isinstance(adj["same_entity"], bool)
        assert 0.0 <= adj["confidence"] <= 1.0

    # Under-merge bias: Organization requires corroboration for name-only
    # matches, and there is none — the mention may become a new entity or go
    # to review, but it must never silently attach to the registry entity.
    got = store.get_mention(tenant, m.id)
    assert not (got.resolution_status == "resolved"
                and got.resolved_entity_id == acme)
    (d,) = rows(store, tenant, "resolution_decisions")
    assert d["decision"] in ("review", "new_entity")


# ---------------------------------------------------------------------------
# 7. Promotion closes the slice: resolved refs rewrite to canonical ids,
#    facts land in `facts` + the AGE graph, and a re-run is a no-op. A later
#    mention with the same SoR key Tier-0s onto the entity the first sweep
#    created (the flywheel starts feeding itself).
# ---------------------------------------------------------------------------
def test_promotion_projects_graph_and_replays_idempotently(store, pipeline,
                                                           resolution,
                                                           tenant):
    doc, chunk = doc_with_chunk(pipeline, store, tenant)
    mixer = EntityMention(
        tenant_id=tenant, surface_text="Mixer M-3", entity_type="Asset",
        source_system="test", source_document_id=doc.id,
        extracted_keys={"asset_id": "A-1"}, context_embedding=unit_vec(10))
    building = EntityMention(
        tenant_id=tenant, surface_text="Building A", entity_type="Location",
        source_system="test", source_document_id=doc.id,
        context_embedding=unit_vec(11))
    store.stage_pending(
        {"m1": mixer, "m2": building},
        [PendingFact(tenant_id=tenant, subject_ref="m1", predicate="part_of",
                     object_ref="m2", ontology_version=ONTOLOGY,
                     source_document_id=doc.id, extractor="test",
                     extractor_version="t1", confidence=1.0)])

    summary = resolution.sweep(tenant)

    # Empty registry -> both mentions became new entities; the fact promoted.
    assert summary.new_entities == 2
    assert len(summary.promoted_facts) == 1
    (fact_id,) = summary.promoted_facts
    fact = store.get_fact(tenant, fact_id)
    m1 = store.get_mention(tenant, mixer.id)
    m2 = store.get_mention(tenant, building.id)
    assert fact.subject_entity_id == m1.resolved_entity_id
    assert fact.object_entity_id == m2.resolved_entity_id
    assert fact.predicate == "part_of"
    (pf,) = rows(store, tenant, "pending_facts")
    assert pf["resolution_status"] == "promoted"
    assert pf["promoted_fact_id"] == fact_id
    # The AGE projection is RETIRED (BP9): promotion writes NO edge — the
    # relational row above is the whole truth.
    assert edge_count(store, tenant, fact_id, fact.subject_entity_id,
                      fact.object_entity_id) == 0

    # New entities inherit the mention's identity: keys, embedding, alias.
    asset = entity_of(store, tenant, m1.resolved_entity_id)
    assert asset.attributes == {"asset_id": "A-1"}
    assert asset.embedding is not None
    assert asset.canonical_name == "Mixer M-3"

    # A later mention with the same SoR key resolves deterministically to
    # the entity the FIRST sweep created, labeling as it goes.
    doc2, chunk2 = doc_with_chunk(pipeline, store, tenant)
    again = stage_mention(store, doc2, chunk2, "Mixer M3", "Asset",
                          keys={"asset_id": "A-1"}, vec=unit_vec(10))
    summary2 = resolution.sweep(tenant)
    assert summary2.by_tier == {"t0": 1}
    assert store.get_mention(tenant, again.id).resolved_entity_id \
        == m1.resolved_entity_id
    assert [l["label_type"] for l in rows(store, tenant, "labels")] \
        == ["er_match"]

    # Idempotency: a third sweep + promotion pass changes nothing.
    before = (len(rows(store, tenant, "facts")),
              len(rows(store, tenant, "entities")),
              len(rows(store, tenant, "resolution_decisions")))
    summary3 = resolution.sweep(tenant)
    assert summary3.swept == 0 and summary3.promoted_facts == []
    after = (len(rows(store, tenant, "facts")),
             len(rows(store, tenant, "entities")),
             len(rows(store, tenant, "resolution_decisions")))
    assert before == after
    assert edge_count(store, tenant, fact_id) == 0  # projection retired (BP9)


# ---------------------------------------------------------------------------
# 8. Merges are reversible: the snapshot restores the absorbed entity, undoes
#    the transfer, repoints facts/refs/graph back, re-resolves the absorbed
#    side's mentions, and writes the er_nonmatch hard negative.
# ---------------------------------------------------------------------------
def test_merge_writes_snapshot_and_reversal_restores(store, pipeline,
                                                     resolution, tenant):
    doc, chunk = doc_with_chunk(pipeline, store, tenant)
    a = store.upsert_entity(make_entity(
        tenant, "Acme Corp", embedding=unit_vec(12), embedding_model="bge-m3"))
    b = store.upsert_entity(make_entity(
        tenant, "Acme Corporation",
        attributes={"tax_id": "99-1234567"}, embedding=unit_vec(13),
        embedding_model="bge-m3",
        aliases=[EntityAlias(tenant_id=tenant, alias="ACME", source="test")]))
    c = store.upsert_entity(make_entity(
        tenant, "Cleaning SOP", "Document", embedding=unit_vec(14),
        embedding_model="bge-m3"))
    (fact_id,) = store.write_facts([Fact(
        tenant_id=tenant, subject_entity_id=b, predicate="governs",
        object_entity_id=c, ontology_version=ONTOLOGY,
        source_document_id=doc.id, extractor="test", extractor_version="t1")])
    mention = stage_mention(store, doc, chunk, "Acme Corporation",
                            "Organization", vec=unit_vec(13),
                            resolution_status="resolved",
                            resolved_entity_id=b)
    pending = PendingFact(
        tenant_id=tenant, subject_ref=f"entity:{b}", predicate="owns",
        object_literal="the cleaning contract", ontology_version=ONTOLOGY,
        source_document_id=doc.id, extractor="test", extractor_version="t1")
    store.stage_pending({}, [pending])

    # ---- merge B into A -----------------------------------------------------
    merge_id = resolution.merge_entities(
        tenant, surviving_id=a, merged_id=b, merged_by="operator",
        method="deterministic_key", score=1.0)

    assert store.get_entity(tenant, b) is None, "absorbed row is gone"
    survivor = entity_of(store, tenant, a)
    assert {al.alias for al in survivor.aliases} \
        >= {"ACME", "Acme Corporation"}
    assert survivor.attributes["tax_id"] == "99-1234567"
    assert store.get_fact(tenant, fact_id).subject_entity_id == a
    assert store.get_mention(tenant, mention.id).resolved_entity_id == a
    assert store.get_pending_fact(tenant, pending.id).subject_ref \
        == f"entity:{a}"
    # The retired projection stayed silent through the merge: no edges, no
    # vertices — relational repointing above is the whole story (BP9).
    assert edge_count(store, tenant, fact_id, a, c) == 0
    ((b_vertices,),) = store.run_cypher(
        tenant, f"MATCH (v:Entity {{id: {b}, tenant_id: '{tenant}'}})"
                " RETURN count(v)")
    assert json.loads(str(b_vertices)) == 0

    merge = store.get_entity_merge(tenant, merge_id)
    snap = merge.merged_snapshot
    assert snap["entity"]["canonical_name"] == "Acme Corporation"
    assert snap["mention_ids"] == [mention.id]
    assert snap["fact_sides"] == [{"id": fact_id, "side": "subject"}]
    assert merge.reversed_at is None

    # ---- reverse ------------------------------------------------------------
    restored = resolution.reverse_merge(tenant, merge_id, reversed_by="operator")

    assert restored == b
    revived = entity_of(store, tenant, b)
    assert revived.canonical_name == "Acme Corporation"
    assert revived.attributes == {"tax_id": "99-1234567"}
    # Alias ROWS restore exactly; the canonical name is a column, not a row.
    assert {al.alias for al in revived.aliases} == {"ACME"}
    survivor = entity_of(store, tenant, a)
    assert "ACME" not in {al.alias for al in survivor.aliases}
    assert "tax_id" not in survivor.attributes
    assert store.get_fact(tenant, fact_id).subject_entity_id == b
    assert store.get_pending_fact(tenant, pending.id).subject_ref \
        == f"entity:{b}"
    assert edge_count(store, tenant, fact_id, b, c) == 0  # projection retired
    assert edge_count(store, tenant, fact_id, a, c) == 0
    assert store.get_entity_merge(tenant, merge_id).reversed_at is not None

    # The absorbed side's mention was re-resolved against the SPLIT registry:
    # an exact-name Organization match without corroboration lands in review
    # — and above all it no longer claims the survivor.
    m = store.get_mention(tenant, mention.id)
    assert m.resolution_status == "review"
    assert m.resolved_entity_id is None

    # The reversal wrote the hard negative.
    labels = [l for l in rows(store, tenant, "labels")
              if l["source"] == "reversal"]
    assert len(labels) == 1
    assert labels[0]["label_type"] == "er_nonmatch"
    assert labels[0]["payload"]["left"] == {"type": "entity", "id": a}
    assert labels[0]["payload"]["right"] == {"type": "entity", "id": b}
    assert labels[0]["payload"]["merge_id"] == merge_id

    # A second reversal must refuse.
    with pytest.raises(ValueError):
        resolution.reverse_merge(tenant, merge_id, reversed_by="operator")


# ---------------------------------------------------------------------------
# 9. Human review closes the loop: approving a gray match resolves the
#    mention and labels the pair; rejecting everything makes a new entity
#    and labels the hard negatives. Both are flywheel feeders.
# ---------------------------------------------------------------------------
def test_review_decisions_apply_and_label(store, pipeline, resolution,
                                          tenant):
    doc, chunk = doc_with_chunk(pipeline, store, tenant)
    zenith = store.upsert_entity(make_entity(
        tenant, "Zenith Widgets", embedding=unit_vec(15),
        embedding_model="bge-m3"))
    m1 = stage_mention(store, doc, chunk, "Zenith Widgets", "Organization",
                       keys={"email": "sales@zw.example"}, vec=unit_vec(15))
    resolution.sweep(tenant)
    assert store.get_mention(tenant, m1.id).resolution_status == "review"

    (mc,) = [c for c in rows(store, tenant, "match_candidates")
             if c["decision"] == "review" and c["left_id"] == m1.id]
    resolution.decide_match(tenant, mc["id"], same=True, reviewer="operator")

    got = store.get_mention(tenant, m1.id)
    assert got.resolution_status == "resolved"
    assert got.resolved_entity_id == zenith
    reviewed = store.get_match_candidate(tenant, mc["id"])
    assert reviewed.decision == "applied"
    assert reviewed.reviewed_by == "operator" and reviewed.reviewed_at is not None
    (label,) = rows(store, tenant, "labels")
    assert label["label_type"] == "er_match"
    assert label["source"] == "human_review"
    assert label["payload"]["candidate_id"] == mc["id"]

    # Second same-name mention -> review again -> the human says it is a
    # DIFFERENT Zenith Widgets: new entity + er_nonmatch for each open pair.
    doc2, chunk2 = doc_with_chunk(pipeline, store, tenant)
    # domain-only: Tier-0 finds nothing (the entity has no domain), and the
    # entity-side NULL keeps the Splink email/domain comparisons neutral, so
    # the exact name lands this in the gray band again.
    m2 = stage_mention(store, doc2, chunk2, "Zenith Widgets", "Organization",
                       keys={"domain": "other-zw.example"},
                       vec=unit_vec(15))
    resolution.sweep(tenant)
    assert store.get_mention(tenant, m2.id).resolution_status == "review"

    new_id = resolution.resolve_as_new(tenant, m2.id, reviewer="operator")

    got2 = store.get_mention(tenant, m2.id)
    assert got2.resolution_status == "resolved"
    assert got2.resolved_entity_id == new_id and new_id != zenith
    nonmatches = [l for l in rows(store, tenant, "labels")
                  if l["label_type"] == "er_nonmatch"]
    assert nonmatches and all(l["source"] == "human_review"
                              for l in nonmatches)
    assert any(l["payload"]["left"] == {"type": "mention", "id": m2.id}
               for l in nonmatches)
    # Two same-name Organizations now coexist — under-merge means the
    # registry tolerates legitimate homonyms.
    assert entity_of(store, tenant, new_id).canonical_name == "Zenith Widgets"
