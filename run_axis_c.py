"""Axis C campaign runner — real (non-advisory) benchmark rounds.

Activates the reviewed retrieval gold sets (named human required), runs the
round's field against every retrieval track, then applies the FROZEN
methodology rules (v1.0 §4):

  * headline = Recall@10 (any-of), per track — never aggregated for decisions
  * displacement needs challenger - incumbent >= +0.02 absolute AND the 95%
    PAIRED bootstrap CI of the difference (1000 resamples, seed 42) excluding
    zero AND per-query latency p95 <= 300ms (instrumented since round 3 —
    load-bearing for dense-vs-hybrid, where fusion costs real milliseconds)
  * ties -> the incumbent stays
  * one-fleet rule: partial-track winners need >= +0.05 on their tracks

Rounds (all recorded; earlier rounds are never re-scored):
  1 — all models bare (SUPERSEDED: tilted toward the prefix-free incumbent)
  2 — prefix-aware dense rematch (DECIDED the model: bge-m3 stays)
  3 — bge-m3 dense (Ollama serving path, incumbent) vs bge-m3 HYBRID
      (dense + its OWN learned sparse via FlagEmbedding, RRF fusion), plus a
      dense-via-FlagEmbedding diagnostic that isolates the engine confound
      (fp16 HF weights vs Ollama GGUF) from the sparse contribution.

Usage:  python run_axis_c.py --activated-by "operator" --round 3
"""
from __future__ import annotations

import argparse
import random
import sys

from knowledge_hub.benchmark import BenchmarkError, BenchmarkRunner
from knowledge_hub.factstore_pg import PostgresFactStore
from knowledge_hub.goldsets import GoldSetError, GoldSetStore

TENANT = "bench-synth"
TRACKS = ("sop", "prose", "communication")
PINS = "pins-2026-07-v1"
MARGIN = 0.02
SPLIT_FLEET_MARGIN = 0.05
LATENCY_P95_MS = 300
BOOT_N, BOOT_SEED = 1000, 42


def _entry(config: dict, role: str) -> dict:
    return {"config": config, "role": role}


ROUNDS: dict[str, list[dict]] = {
    "1": [_entry({"embedder": m, "mode": "dense", "prompt_style": "none",
                  "label": m},
                 "incumbent" if m == "bge-m3" else "challenger")
          for m in ["bge-m3", "mxbai-embed-large", "snowflake-arctic-embed",
                    "nomic-embed-text", "qwen3-embedding"]],
    "2": [
        _entry({"embedder": "bge-m3", "mode": "dense", "prompt_style": "none",
                "label": "bge-m3 (incumbent, prefix-free)"}, "incumbent"),
        _entry({"embedder": "nomic-embed-text", "mode": "dense",
                "prompt_style": "nomic-search",
                "label": "nomic-embed-text [search prefixes]"}, "challenger"),
        _entry({"embedder": "qwen3-embedding", "mode": "dense",
                "prompt_style": "qwen3-instruct",
                "label": "qwen3-embedding [instruct]"}, "challenger"),
    ],
    "3": [
        _entry({"embedder": "bge-m3", "mode": "dense", "engine": "ollama",
                "prompt_style": "none",
                "label": "bge-m3 dense [ollama] (incumbent)"}, "incumbent"),
        _entry({"embedder": "bge-m3", "mode": "dense",
                "engine": "flagembedding", "prompt_style": "none",
                "label": "bge-m3 dense [flagembedding] (engine diagnostic)"},
               "diagnostic"),
        _entry({"embedder": "bge-m3", "mode": "hybrid",
                "engine": "flagembedding", "prompt_style": "none",
                "fusion_method": "rrf", "rrf_k": 60,
                "label": "bge-m3 hybrid rrf [flagembedding]"}, "challenger"),
    ],
}


def paired_diff_ci(store: PostgresFactStore, run_a: int, run_b: int
                   ) -> tuple[float, float, float]:
    """Mean and 95% CI of (challenger - incumbent) hit@10, paired per gold
    item (same items by construction: same gold set)."""
    with store.transaction(TENANT) as conn:
        rows = conn.execute(
            """SELECT a.gold_set_item_id,
                      (a.outcome->>'hit_any_at_10')::bool AS ha,
                      (b.outcome->>'hit_any_at_10')::bool AS hb
               FROM benchmark_run_items a
               JOIN benchmark_run_items b USING (gold_set_item_id)
               WHERE a.run_id=%s AND b.run_id=%s
               ORDER BY a.gold_set_item_id""", (run_a, run_b)).fetchall()
    diffs = [(1.0 if r["ha"] else 0.0) - (1.0 if r["hb"] else 0.0) for r in rows]
    n = len(diffs)
    mean = sum(diffs) / n
    rng = random.Random(BOOT_SEED)
    samples = sorted(sum(diffs[rng.randrange(n)] for _ in range(n)) / n
                     for _ in range(BOOT_N))
    return mean, samples[int(0.025 * BOOT_N)], samples[min(int(0.975 * BOOT_N),
                                                           BOOT_N - 1)]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--activated-by", required=True,
                    help="named human signing off the gold-set review")
    ap.add_argument("--version", default="0.1")
    ap.add_argument("--round", choices=sorted(ROUNDS), default="3")
    args = ap.parse_args()
    field = ROUNDS[args.round]

    store = PostgresFactStore()
    gold_store = GoldSetStore(store)
    runner = BenchmarkRunner(store)

    for track in TRACKS:
        try:
            gs = gold_store.activate(TENANT, "retrieval",
                                     f"synth-{track}-{args.version}",
                                     by=args.activated_by)
            print(f"activated {gs.version} (by {gs.activated_by})")
        except GoldSetError as e:
            if "no draft" not in str(e):
                raise
            print(f"synth-{track}-{args.version}: already active")

    results: dict[str, dict[str, dict]] = {}   # track -> label -> run info
    for track in TRACKS:
        results[track] = {}
        for entry in field:
            cfg, label = dict(entry["config"]), entry["config"]["label"]
            try:
                run = runner.run(TENANT, axis="c_embedder", config=cfg,
                                 gold_set_version=f"synth-{track}-{args.version}",
                                 pin_profile_name=PINS,
                                 notes=f"axis-C round {args.round}, track={track}")
            except BenchmarkError as e:
                print(f"  [{track}] {label}: {e}")
                continue
            m = run.metrics
            results[track][label] = {"run_id": run.id, "metrics": m,
                                     "role": entry["role"],
                                     "wall_ms": run.wall_ms}
            lat = (f" p95={m['latency_ms_p95']:.0f}ms"
                   if "latency_ms_p95" in m else "")
            print(f"  [{track}] {label:44s} R@10={m['recall_at_10_any']:.3f} "
                  f"MRR={m['mrr']:.3f} nDCG@10={m['ndcg_at_10']:.3f}{lat} "
                  f"wall={run.wall_ms}ms advisory={run.advisory}")

    # ---- frozen-rule verdicts, per track --------------------------------
    print("\n" + "=" * 72)
    print(f"VERDICTS (frozen v1.0 §4: +{MARGIN} margin, paired CI excluding "
          f"zero, latency p95 <= {LATENCY_P95_MS}ms; incumbent wins ties)")
    winners: dict[str, str] = {}
    for track in TRACKS:
        by_role = {info["role"]: (label, info)
                   for label, info in results[track].items()}
        if "incumbent" not in by_role:
            continue
        inc_label, inc = by_role["incumbent"]
        print(f"\n[{track}] incumbent {inc_label} "
              f"R@10={inc['metrics']['recall_at_10_any']:.3f} "
              f"p95={inc['metrics'].get('latency_ms_p95', float('nan')):.0f}ms")
        winner = inc_label
        for label, info in results[track].items():
            if info["role"] == "incumbent":
                continue
            mean, lo, hi = paired_diff_ci(store, info["run_id"], inc["run_id"])
            p95 = info["metrics"].get("latency_ms_p95")
            gates = {
                "margin": mean >= MARGIN,
                "ci": lo > 0,
                "latency": p95 is not None and p95 <= LATENCY_P95_MS,
            }
            displaces = all(gates.values()) and info["role"] == "challenger"
            failed = [g for g, ok in gates.items() if not ok]
            tag = ("DIAGNOSTIC" if info["role"] == "diagnostic" else
                   ("DISPLACES" if displaces else f"no ({'+'.join(failed) or 'role'})"))
            print(f"  {label:44s} diff={mean:+.3f} CI=[{lo:+.3f},{hi:+.3f}] "
                  f"p95={p95 if p95 is not None else float('nan'):.0f}ms -> {tag}")
            if displaces:
                winner = label
        winners[track] = winner
        print(f"  track winner: {winner}")

    print("\nFleet decision (one-fleet rule):")
    inc_labels = {label for t in results.values()
                  for label, i in t.items() if i["role"] == "incumbent"}
    if all(w in inc_labels for w in winners.values()):
        print("  incumbent holds every track — INCUMBENT STAYS. "
              "No pin-profile change.")
    else:
        print(f"  per-track winners: {winners} — apply §4 rule 3 (split fleet "
              f"only if the winning tracks clear +{SPLIT_FLEET_MARGIN}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
