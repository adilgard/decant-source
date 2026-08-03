"""Benchmark recording harness (Build Prompt 6) — real stack, live bge-m3.

This file IS the methodology's Deliverable-4 dry-run, expressed as tests:
prove the harness records a trivial synthetic run end-to-end with correct
provenance, recomputable aggregates, and structural one-axis enforcement —
before any real benchmark config exists.
"""
from __future__ import annotations

import json

import pytest

from knowledge_hub.benchmark import (PROMPT_STYLES, BenchmarkError,
                                     BenchmarkRunner, RetrievalEvaluator)
from knowledge_hub.goldsets import (ERGoldGenerator, ExtractionGoldDrafter,
                                    GoldSetError, GoldSetStore,
                                    LLMQueryGenerator,
                                    SyntheticRetrievalGenerator, jaccard)

ONTOLOGY = "baseline-0.1"
PINS = "pins-2026-07-v1"
INCUMBENT = {"embedder": "bge-m3", "mode": "dense", "label": "bge-m3 incumbent"}


@pytest.fixture()
def gold_store(store):
    return GoldSetStore(store)


@pytest.fixture()
def runner(store):
    return BenchmarkRunner(store)


@pytest.fixture()
def synthetic_gold(store, embedder, gold_store, tenant):
    """A registered + activated synthetic retrieval gold set (5 queries)."""
    items, spec = SyntheticRetrievalGenerator(store, embedder).generate(tenant)
    gold_store.register(tenant, "retrieval", "dryrun-0.1", items,
                        generator="synthetic", spec=spec)
    return gold_store.activate(tenant, "retrieval", "dryrun-0.1", by="test-suite")


# ---------------------------------------------------------------------------
# Gold-set machinery
# ---------------------------------------------------------------------------
def test_goldset_register_activate_immutable(store, embedder, gold_store, tenant):
    items, spec = SyntheticRetrievalGenerator(store, embedder).generate(tenant)
    gs = gold_store.register(tenant, "retrieval", "v0.1", items,
                             generator="synthetic", spec=spec)
    assert gs.status == "draft"
    assert gs.item_count == len(items) == 5
    assert not gs.floors_met            # 5 < the 50-query floor -> advisory runs
    assert gs.content_hash and len(gs.content_hash) == 64

    # Immutable: same version again is a refusal, not an upsert.
    with pytest.raises(GoldSetError, match="immutable"):
        gold_store.register(tenant, "retrieval", "v0.1", items,
                            generator="synthetic", spec=spec)
    # Activation demands a named human.
    with pytest.raises(GoldSetError, match="named reviewer"):
        gold_store.activate(tenant, "retrieval", "v0.1", by="  ")
    active = gold_store.activate(tenant, "retrieval", "v0.1", by="operator")
    assert active.status == "active" and active.activated_by == "operator"

    # Every synthetic query passed the leakage guard at generation time.
    _, stored = gold_store.get(tenant, "retrieval", "v0.1")
    assert all(i.item["leakage_jaccard"] < 0.6 for i in stored)


# ---------------------------------------------------------------------------
# Structural refusals (nothing recorded)
# ---------------------------------------------------------------------------
def test_runner_refuses_offaxis_and_unpinned(runner, synthetic_gold, store, tenant):
    # Off-axis knob: index params on the embedder axis.
    with pytest.raises(BenchmarkError, match="one axis at a time"):
        runner.run(tenant, axis="c_embedder",
                   config={"embedder": "bge-m3", "index": "vectorchord"},
                   gold_set_version="dryrun-0.1", pin_profile_name=PINS)
    # Missing required knob.
    with pytest.raises(BenchmarkError, match="required"):
        runner.run(tenant, axis="c_embedder", config={"mode": "dense"},
                   gold_set_version="dryrun-0.1", pin_profile_name=PINS)
    # Unknown pin profile.
    with pytest.raises(BenchmarkError, match="pin profile"):
        runner.run(tenant, axis="c_embedder", config=INCUMBENT,
                   gold_set_version="dryrun-0.1", pin_profile_name="nope")
    # Campaign-phase axis: clean refusal, no error row.
    with pytest.raises(BenchmarkError, match="campaign"):
        runner.run(tenant, axis="a_index", config={"index": "pgvector-hnsw"},
                   gold_set_version="dryrun-0.1", pin_profile_name=PINS)
    with store.transaction(tenant) as conn:
        n = conn.execute("SELECT count(*) AS n FROM benchmark_runs "
                         "WHERE tenant_id=%s", (tenant,)).fetchone()["n"]
    assert n == 0, "refusals must not leave run rows"


def test_runner_refuses_draft_gold_set(runner, store, embedder, gold_store, tenant):
    items, spec = SyntheticRetrievalGenerator(store, embedder).generate(tenant)
    gold_store.register(tenant, "retrieval", "draft-0.1", items,
                        generator="synthetic", spec=spec)
    with pytest.raises(BenchmarkError, match="ACTIVE"):
        runner.run(tenant, axis="c_embedder", config=INCUMBENT,
                   gold_set_version="draft-0.1", pin_profile_name=PINS)


# ---------------------------------------------------------------------------
# The dry-run itself: record everything, prove it recomputes
# ---------------------------------------------------------------------------
def test_dryrun_records_provenance_and_recomputes(runner, synthetic_gold,
                                                  store, tenant):
    run = runner.run(tenant, axis="c_embedder", config=INCUMBENT,
                     gold_set_version="dryrun-0.1", pin_profile_name=PINS,
                     notes="deliverable-4 dry-run")
    assert run.status == "ok"
    assert run.advisory is True          # floors not met -> cannot decide anything

    # Provenance: every field present and shaped (methodology §7.1).
    assert run.model_digests.get("bge-m3") not in (None, "")
    assert run.package_version not in (None, "unknown")
    assert len(run.code_hash) == 64
    assert run.runner_version
    assert run.hardware and run.hardware.get("postgres") != "unknown"
    assert run.gold_set_hash == synthetic_gold.content_hash
    assert run.pin_profile["c_embedder"]["embedder"] == "bge-m3"

    # Metrics sane on a 5-vector corpus with live bge-m3: the right chunk
    # should sit at/near the top for every paraphrased query.
    m = run.metrics
    assert m["n_queries"] == 5
    assert m["recall_at_10_any"] == 1.0   # k=10 >= corpus size: must be total
    assert m["recall_at_3_any"] >= 0.6
    assert 0 < m["mrr"] <= 1.0
    lo, hi = m["recall_at_10_any_ci95"]
    assert lo <= m["recall_at_10_any"] <= hi
    assert m["headline_name"] == "recall_at_10_any"

    # Recompute-from-items: stored aggregates == aggregate(stored outcomes).
    with store.transaction(tenant) as conn:
        rows = conn.execute(
            """SELECT outcome FROM benchmark_run_items WHERE run_id=%s
               ORDER BY gold_set_item_id""", (run.id,)).fetchall()
    assert len(rows) == 5
    recomputed = RetrievalEvaluator.aggregate([r["outcome"] for r in rows])
    for key, val in recomputed.items():
        assert m[key] == pytest.approx(val), f"aggregate {key} does not recompute"

    # Leaderboard sees it, with the comparability keys riding along.
    with store.transaction(tenant) as conn:
        board = conn.execute(
            "SELECT * FROM benchmark_leaderboard WHERE tenant_id=%s",
            (tenant,)).fetchall()
    assert len(board) == 1
    row = board[0]
    assert row["config_label"] == "bge-m3 incumbent"
    assert row["pin_profile_name"] == PINS
    assert row["headline_value"] == pytest.approx(1.0)
    assert row["advisory"] is True


def test_challenger_embedder_runs_in_its_own_space(runner, synthetic_gold,
                                                   store, tenant):
    """A non-incumbent embedder must re-embed the corpus (its own space) and
    auto-detect its dimension — the campaign's Axis-C mechanism."""
    challenger = {"embedder": "snowflake-arctic-embed", "mode": "dense",
                  "label": "snowflake-arctic-embed"}
    run = runner.run(tenant, axis="c_embedder", config=challenger,
                     gold_set_version="dryrun-0.1", pin_profile_name=PINS)
    assert run.status == "ok" and run.advisory is True
    assert run.metrics["embedding_dim"] == 1024
    assert run.model_digests.get("snowflake-arctic-embed") not in (
        None, "", "unknown")
    # Same gold set + pins as the incumbent -> same leaderboard group.
    with store.transaction(tenant) as conn:
        board = conn.execute(
            "SELECT config_label FROM benchmark_leaderboard WHERE tenant_id=%s",
            (tenant,)).fetchall()
    labels = {b["config_label"] for b in board}
    assert "snowflake-arctic-embed" in labels
    # Hybrid without a sparse-capable engine is a clean refusal, not an
    # error row (Build Prompt 8: Ollama's embed API is dense-only).
    with pytest.raises(BenchmarkError, match="dense vectors only"):
        runner.run(tenant, axis="c_embedder",
                   config={"embedder": "bge-m3", "mode": "hybrid"},
                   gold_set_version="dryrun-0.1", pin_profile_name=PINS)


def test_prompt_style_applied_and_recorded(runner, synthetic_gold, store,
                                           tenant):
    """Build Prompt 7: asymmetric per-model prompt styles are a real config
    knob — the exact strings ride in the recorded config, and a styled run is
    a DISTINCT config from a bare one (no duplicate refusal)."""
    bare = runner.run(tenant, axis="c_embedder",
                      config={"embedder": "nomic-embed-text", "mode": "dense",
                              "label": "nomic bare"},
                      gold_set_version="dryrun-0.1", pin_profile_name=PINS)
    styled = runner.run(tenant, axis="c_embedder",
                        config={"embedder": "nomic-embed-text", "mode": "dense",
                                "prompt_style": "nomic-search",
                                "label": "nomic prefixed"},
                        gold_set_version="dryrun-0.1", pin_profile_name=PINS)
    assert styled.id != bare.id
    # Exact strings denormalized into provenance, straight from the registry.
    assert styled.config["prompt_style_detail"] == PROMPT_STYLES["nomic-search"]
    assert styled.config["prompt_style_detail"]["query_prefix"] == "search_query: "
    assert styled.config["prompt_style_detail"]["document_prefix"] == "search_document: "
    assert bare.config["prompt_style"] == "none"
    # The prefix genuinely changes the embedding space: at least one per-item
    # ranking differs between bare and styled runs on the same gold items.
    with store.transaction(tenant) as conn:
        rows = conn.execute(
            """SELECT a.outcome->>'ranked_chunk_ids' AS ra,
                      b.outcome->>'ranked_chunk_ids' AS rb
               FROM benchmark_run_items a
               JOIN benchmark_run_items b USING (gold_set_item_id)
               WHERE a.run_id=%s AND b.run_id=%s""",
            (bare.id, styled.id)).fetchall()
    assert any(r["ra"] != r["rb"] for r in rows), \
        "prefixed embeddings produced identical rankings — prefix not applied?"
    # Unknown style: clean refusal, no row.
    with pytest.raises(BenchmarkError, match="unknown prompt_style"):
        runner.run(tenant, axis="c_embedder",
                   config={"embedder": "bge-m3", "prompt_style": "nope"},
                   gold_set_version="dryrun-0.1", pin_profile_name=PINS)
    # Caller-supplied detail that contradicts the registry: refused.
    with pytest.raises(BenchmarkError, match="derived from the registry"):
        runner.run(tenant, axis="c_embedder",
                   config={"embedder": "bge-m3", "prompt_style": "none",
                           "prompt_style_detail": {"query_prefix": "haxx: "}},
                   gold_set_version="dryrun-0.1", pin_profile_name=PINS)


def test_hybrid_mode_gates_and_run(runner, synthetic_gold, store, tenant):
    """Build Prompt 8: hybrid = bge-m3 dense + its OWN learned sparse
    (FlagEmbedding — Ollama's API is dense-only), RRF-fused, latency-timed."""
    # Ollama cannot source sparse: hybrid+ollama is a documented refusal.
    with pytest.raises(BenchmarkError, match="dense vectors only"):
        runner.run(tenant, axis="c_embedder",
                   config={"embedder": "bge-m3", "mode": "hybrid",
                           "engine": "ollama"},
                   gold_set_version="dryrun-0.1", pin_profile_name=PINS)
    # Fusion knobs are hybrid-only.
    with pytest.raises(BenchmarkError, match="hybrid-only"):
        runner.run(tenant, axis="c_embedder",
                   config={"embedder": "bge-m3", "mode": "dense",
                           "rrf_k": 60},
                   gold_set_version="dryrun-0.1", pin_profile_name=PINS)

    run = runner.run(tenant, axis="c_embedder",
                     config={"embedder": "bge-m3", "mode": "hybrid",
                             "engine": "flagembedding",
                             "label": "hybrid test"},
                     gold_set_version="dryrun-0.1", pin_profile_name=PINS)
    assert run.status == "ok"
    # Fusion defaults denormalized into provenance.
    assert run.config["fusion_method"] == "rrf"
    assert run.config["rrf_k"] == 60
    assert run.config["engine"] == "flagembedding"
    # Latency instrumentation (the p95 gate's evidence) present + sane.
    m = run.metrics
    assert 0 < m["latency_ms_p50"] <= m["latency_ms_p95"] <= m["latency_ms_p99"]
    assert m["embedding_dim"] == 1024
    # On the 5-chunk synthetic set the right chunk must still surface.
    assert m["recall_at_10_any"] == 1.0
    # Recompute-from-items holds for hybrid outcomes too (incl. latency keys).
    with store.transaction(tenant) as conn:
        rows = conn.execute(
            "SELECT outcome FROM benchmark_run_items WHERE run_id=%s "
            "ORDER BY gold_set_item_id", (run.id,)).fetchall()
    recomputed = RetrievalEvaluator.aggregate([r["outcome"] for r in rows])
    for key, val in recomputed.items():
        assert m[key] == pytest.approx(val), f"{key} does not recompute"


def test_superseded_flag_reaches_leaderboard(runner, synthetic_gold, store,
                                             tenant):
    """Migration 007: a superseded run stays recorded but is flagged on the
    leaderboard so it can't read as a current decision."""
    run = runner.run(tenant, axis="c_embedder",
                     config={"embedder": "bge-m3", "mode": "dense",
                             "label": "to-supersede"},
                     gold_set_version="dryrun-0.1", pin_profile_name=PINS)
    with store.transaction(tenant) as conn:
        conn.execute(
            "UPDATE benchmark_runs SET superseded_note=%s WHERE id=%s",
            ("test supersession", run.id))
        row = conn.execute(
            "SELECT superseded FROM benchmark_leaderboard WHERE run_id=%s",
            (run.id,)).fetchone()
    assert row["superseded"] is True


def test_duplicate_refused_force_records(runner, synthetic_gold, tenant):
    first = runner.run(tenant, axis="c_embedder", config=INCUMBENT,
                       gold_set_version="dryrun-0.1", pin_profile_name=PINS)
    with pytest.raises(BenchmarkError, match="already recorded"):
        runner.run(tenant, axis="c_embedder", config=INCUMBENT,
                   gold_set_version="dryrun-0.1", pin_profile_name=PINS)
    second = runner.run(tenant, axis="c_embedder", config=INCUMBENT,
                        gold_set_version="dryrun-0.1", pin_profile_name=PINS,
                        force=True)
    assert second.id != first.id
    assert second.metrics["headline_value"] == first.metrics["headline_value"]


def test_crash_leaves_visible_error_row(runner, store, embedder, gold_store,
                                        tenant):
    # A gold set whose corpus points at chunks that don't exist: the run
    # starts (row written) and then fails -> status='error', never a phantom.
    items = [{"query": "anything", "relevant_chunk_ids": [999999901],
              "multi_hop": False}]
    spec = {"track": "prose", "synthetic": True,
            "corpus_chunk_ids": [999999901], "corpus_hash": "x" * 64}
    gold_store.register(tenant, "retrieval", "broken-0.1", items,
                        generator="synthetic", spec=spec)
    gold_store.activate(tenant, "retrieval", "broken-0.1", by="test-suite")
    with pytest.raises(BenchmarkError, match="recorded as error"):
        runner.run(tenant, axis="c_embedder", config=INCUMBENT,
                   gold_set_version="broken-0.1", pin_profile_name=PINS)
    with store.transaction(tenant) as conn:
        row = conn.execute(
            """SELECT status, error FROM benchmark_runs
               WHERE tenant_id=%s ORDER BY id DESC LIMIT 1""",
            (tenant,)).fetchone()
    assert row["status"] == "error"
    assert "corpus mismatch" in row["error"]


# ---------------------------------------------------------------------------
# Gold-set generators
# ---------------------------------------------------------------------------
def test_er_generator_labels_and_corruption(store, tenant):
    with store.transaction(tenant) as conn:
        conn.execute(
            """INSERT INTO labels (tenant_id, label_type, payload, source,
                                   authority, ontology_version)
               VALUES (%s,'er_nonmatch',%s,'human_review',0.9,%s)""",
            (tenant, json.dumps({"left": {"type": "mention", "id": 1},
                                 "right": {"type": "entity", "id": 2}}),
             ONTOLOGY))
    gen = ERGoldGenerator(store)
    from_labels = gen.from_labels(tenant)
    assert len(from_labels) == 1
    assert from_labels[0]["match"] is False
    assert from_labels[0]["source"] == "human_review"
    assert from_labels[0]["authority"] == pytest.approx(0.9)

    entities = [("Acme Corp", "Organization"), ("Acme LLC", "Organization"),
                ("Zenith Widgets Ltd", "Organization")]
    a = gen.corruption(entities, seed=7)
    b = gen.corruption(entities, seed=7)
    assert a == b, "corruption must be deterministic under a seed"
    positives = [i for i in a if i["match"]]
    negatives = [i for i in a if not i["match"]]
    assert len(positives) == 6           # 2 variants x 3 entities
    assert all(i["left_name"] != i["right_name"] for i in positives)
    assert any(i.get("hard_negative") for i in negatives), \
        "Acme Corp vs Acme LLC must yield a hard negative"


def test_llm_query_generator_live_leakage_guard():
    # Live local model on a SYNTHETIC chunk (privacy fork: this suite never
    # touches tenant data with a model).
    chunk = ("Deviation reports are submitted within one business day of "
             "discovery and assigned a severity classification by the "
             "investigation lead.")
    out = LLMQueryGenerator().generate_query(chunk)
    assert out["question"].strip()
    assert out["leakage_jaccard"] < 0.6
    assert jaccard(out["question"], chunk) == pytest.approx(out["leakage_jaccard"], abs=1e-3)


def test_extraction_drafter_drafts_for_review(store, tenant):
    with store.transaction(tenant) as conn:
        raw = conn.execute(
            """INSERT INTO raw_documents (tenant_id, source_system, content_hash,
                   raw_uri, status)
               VALUES (%s,'test','h-draft','synthetic://draft','parsed')
               RETURNING id""", (tenant,)).fetchone()
        doc = conn.execute(
            """INSERT INTO documents (tenant_id, raw_document_id, doc_type)
               VALUES (%s,%s,'prose') RETURNING id""",
            (tenant, raw["id"])).fetchone()
        chunk = conn.execute(
            """INSERT INTO chunks (tenant_id, document_id, level, seq, content,
                   content_hash)
               VALUES (%s,%s,'parent',0,'text','h-chunk-draft') RETURNING id""",
            (tenant, doc["id"])).fetchone()
        conn.execute(
            """INSERT INTO pending_facts (tenant_id, subject_ref, predicate,
                   object_literal, ontology_version, source_document_id,
                   source_chunk_id, extractor, extractor_version, grounding)
               VALUES (%s,'mention:1','owns','the release decision',%s,%s,%s,
                       'llm_joint','qwen3.6@test/p2','pass')""",
            (tenant, ONTOLOGY, doc["id"], chunk["id"]))
        conn.execute(
            """INSERT INTO quarantined_extractions (tenant_id, document_id,
                   source_chunk_id, reason, detail, extractor,
                   extractor_version, ontology_version)
               VALUES (%s,%s,%s,'unbound_predicate','retained_for','llm_joint',
                       'qwen3.6@test/p2',%s)""",
            (tenant, doc["id"], chunk["id"], ONTOLOGY))

    drafts = ExtractionGoldDrafter(store).draft(tenant, doc["id"])
    assert len(drafts) == 1
    d = drafts[0]
    assert d["parent_chunk_id"] == chunk["id"]
    assert d["reviewed"] is False        # SME reviews; drafter never authors truth
    assert d["expected_facts"][0]["predicate"] == "owns"
    assert d["off_ontology"][0]["detail"] == "retained_for"
