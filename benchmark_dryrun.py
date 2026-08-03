"""Recording dry-run (methodology Deliverable 4) — validate the harness, not
measure anything.

Run from the bundle folder with the venv active:  python benchmark_dryrun.py

Creates (once) a tiny SYNTHETIC retrieval gold set under the dedicated tenant
'bench-dryrun' (per-tenant isolation keeps it invisible to real tenants),
executes ONE c_embedder run with the incumbent config against it, and then
PROVES the recording:

  * run row 'ok' with every provenance field populated
  * stored aggregates recompute exactly from benchmark_run_items
  * bootstrap CI present; advisory=true by construction (5 items < the
    50-query floor — this run can never decide anything)
  * the leaderboard view returns it under its comparability keys
  * re-invoking with the identical config is refused (methodology §8)

This is the ONLY kind of run allowed before the campaign phase.
"""
from __future__ import annotations

import sys

from knowledge_hub.benchmark import (BenchmarkError, BenchmarkRunner,
                                     RetrievalEvaluator)
from knowledge_hub.embedding_ollama import OllamaEmbedder
from knowledge_hub.factstore_pg import PostgresFactStore
from knowledge_hub.goldsets import GoldSetError, GoldSetStore, \
    SyntheticRetrievalGenerator

TENANT = "bench-dryrun"
GOLD_VERSION = "dryrun-0.1"
PINS = "pins-2026-07-v1"
INCUMBENT = {"embedder": "bge-m3", "mode": "dense", "label": "bge-m3 incumbent"}

PASS, FAIL = "[ OK ]", "[FAIL]"


def main() -> int:
    store = PostgresFactStore()
    gold_store = GoldSetStore(store)
    runner = BenchmarkRunner(store)
    failures = 0

    def check(name: str, ok: bool, detail: str = ""):
        nonlocal failures
        print(f"{PASS if ok else FAIL} {name}" + (f": {detail}" if detail else ""))
        failures += (not ok)

    # 1. Gold set: create once, reuse thereafter (the set is immutable).
    try:
        gold, items = gold_store.get(TENANT, "retrieval", GOLD_VERSION)
        print(f"gold set {GOLD_VERSION} exists (id {gold.id}, "
              f"{gold.item_count} items) — reusing")
    except GoldSetError:
        gen = SyntheticRetrievalGenerator(store, OllamaEmbedder())
        gen_items, spec = gen.generate(TENANT)
        gold_store.register(TENANT, "retrieval", GOLD_VERSION, gen_items,
                            generator="synthetic", spec=spec)
        gold = gold_store.activate(TENANT, "retrieval", GOLD_VERSION,
                                   by="operator (dry-run script)")
        gold, items = gold_store.get(TENANT, "retrieval", GOLD_VERSION)
        print(f"gold set {GOLD_VERSION} created + activated "
              f"(id {gold.id}, {gold.item_count} items)")

    # 2. One recorded run (or prove the duplicate refusal if it exists).
    try:
        run = runner.run(TENANT, axis="c_embedder", config=INCUMBENT,
                         gold_set_version=GOLD_VERSION, pin_profile_name=PINS,
                         notes="deliverable-4 recording dry-run")
        print(f"run {run.id} recorded (wall {run.wall_ms} ms)")
    except BenchmarkError as e:
        if "already recorded" not in str(e):
            raise
        print(f"duplicate refused as designed: {e}")
        with store.transaction(TENANT) as conn:
            row = conn.execute(
                """SELECT * FROM benchmark_runs WHERE tenant_id=%s AND
                   status='ok' ORDER BY id DESC LIMIT 1""", (TENANT,)).fetchone()
        from knowledge_hub.models import BenchmarkRun
        run = BenchmarkRun(**row)

    # 3. Validation: the whole point.
    check("status ok", run.status == "ok")
    check("advisory (below floors, cannot decide)", run.advisory is True)
    check("model digest recorded",
          bool(run.model_digests) and run.model_digests.get("bge-m3") not in
          (None, "", "unknown"), str(run.model_digests))
    check("package version", run.package_version not in (None, "unknown"),
          run.package_version)
    check("code hash (sha256, no-git fallback)", len(run.code_hash or "") == 64,
          (run.code_hash or "")[:12] + "…")
    check("hardware fingerprint sees postgres",
          bool(run.hardware) and run.hardware.get("postgres") != "unknown",
          f"pg {run.hardware.get('postgres')}, gpus {run.hardware.get('gpus')}")
    check("gold-set hash pinned", run.gold_set_hash == gold.content_hash)

    m = run.metrics or {}
    check("headline metric", m.get("headline_name") == "recall_at_10_any",
          f"{m.get('headline_name')}={m.get('headline_value')}")
    check("bootstrap CI present", "recall_at_10_any_ci95" in m,
          str(m.get("recall_at_10_any_ci95")))

    with store.transaction(TENANT) as conn:
        rows = conn.execute(
            """SELECT outcome FROM benchmark_run_items WHERE run_id=%s
               ORDER BY gold_set_item_id""", (run.id,)).fetchall()
        board = conn.execute(
            """SELECT * FROM benchmark_leaderboard WHERE tenant_id=%s""",
            (TENANT,)).fetchall()
    recomputed = RetrievalEvaluator.aggregate([r["outcome"] for r in rows])
    drift = [k for k, v in recomputed.items() if m.get(k) != v]
    check("aggregates recompute from per-item rows", not drift,
          f"{len(rows)} items" + (f", drift in {drift}" if drift else ""))
    check("leaderboard sees the run",
          any(b["run_id"] == run.id for b in board),
          f"{len(board)} row(s) for tenant {TENANT}")

    print("-" * 44)
    if failures:
        print(f"{FAIL} dry-run: {failures} validation(s) failing")
        return 1
    print(f"{PASS} recording harness validated — the campaign can trust it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
