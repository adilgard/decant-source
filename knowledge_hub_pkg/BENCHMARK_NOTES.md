# Benchmark recording harness (Build Prompt 6) — notes

Delivered against the APPROVED methodology
(`.Progress Docs/Ongoing/KnowledgeHub_Benchmark_Methodology_v0.1_2026-07-22.md`,
stamped v1.0 on approval): migration `006_benchmark_harness.sql` (gold_sets /
gold_set_items / pin_profiles / benchmark_runs / benchmark_run_items + the
`benchmark_leaderboard` view; applied to the pilot DB, tracked in
schema_migrations, picked up by install-ubuntu.sh's glob), models in
lock-step (GoldSet / GoldSetItem / PinProfile / BenchmarkRun /
BenchmarkRunItem), `knowledge_hub/goldsets.py` (versioning layer + four
generators), `knowledge_hub/benchmark.py` (the runner + retrieval evaluator),
`../benchmark_dryrun.py` (Deliverable 4), check_stack check 8, and 9 new
tests (85 total, real stack + live bge-m3 + live qwen3.6, no mocks).
Package version 0.6.0.

**Read this first:** nothing here measures anything real yet. This stage's
product is the RECORDING — the campaign phase (installing VectorChord/Zingg/
alt models, generating real gold sets, running configs) trusts it only
because the dry-run proved, on a trivial synthetic run, that every
provenance field lands and every aggregate recomputes from the per-item
rows. The methodology's decision rules are frozen; nothing in this code
embodies a preference between configs.

## Structural, not disciplinary

The methodology's failure mode is post-hoc rationalization, so the rules are
checks, not conventions:

* **One axis at a time** — each axis declares a knob schema; an off-axis
  config key is refused before anything runs (`config keys ['index'] are not
  c_embedder knobs`).
* **Pinned means recorded** — `pin_profiles` stores the frozen non-varying
  axes as data (seeded with the §0.3 incumbents as `pins-2026-07-v1`); the
  profile snapshot is denormalized onto every run, and an explicitly pinned
  model digest is verified against the served model.
* **Gold sets are immutable and reviewed** — re-registering a version is a
  refusal; the runner only accepts ACTIVE sets, and activation requires a
  named human (`by=`). Below the §6.2 floors, runs are stamped
  `advisory` and can never decide a winner.
* **No phantom runs** — the run row is written `status='running'` before
  execution; a crash finalizes it as `error` (tested: a corpus-mismatch
  failure leaves a visible error row).
* **No accidental duplicates** — an identical (axis, config, gold set,
  pins) 'ok' run is refused; `force=True` records a distinct run.
* **Corpus integrity** — retrieval gold sets pin `corpus_chunk_ids` + a
  hash over the chunks' content hashes; the evaluator re-verifies at run
  time, so a mutated corpus can't impersonate the ground truth.

## What's implemented vs deferred

Only the **c_embedder** evaluator ships now (exact brute-force cosine in
numpy — §4 of the methodology keeps ANN noise out of Axis C; Axis A will
reuse the same gold sets with index modes). `a_index` / `b_er` /
`d_extraction` evaluators arrive with the campaign; asking for them is a
clean refusal, not an error row. Bootstrap CIs: 1000 resamples, seed 42,
percentile 95%, over gold items.

### Campaign update (2026-07-23) — the re-embed correctness fix

The evaluator now **re-embeds the corpus content in memory with the config's
own embedder** on every run, instead of reading the stored (incumbent bge-m3)
vectors. Comparing an mxbai query against bge-m3 corpus vectors is
cross-space nonsense; the original code only happened to be correct for the
incumbent. Chunk rows are now just the source of content + the integrity
hash — nothing is read from or written to their `embedding` column.
Dimension is **auto-detected** per model (`detect_embedding_dim`), so
`nomic-embed-text` (768) and `bge-m3` (1024) coexist with no config change.
`mode` other than `dense` is a clean refusal (sparse/hybrid ride in with the
retrieval serving path). `metrics.embedding_dim` records the resolved dim.

**Advisory shake-out (tenant `bench-dryrun`, NOT a result):** bge-m3 /
mxbai-embed-large / snowflake-arctic-embed / nomic-embed-text all recorded
against `retrieval/dryrun-0.1` and land on one leaderboard group. Every
metric is 1.00 because the synthetic 5-query set is trivially easy and k=10
exceeds the 5-chunk corpus — this validates multi-config recording and
comparison, and decides nothing (all rows `advisory`). Real numbers need a
real gold set (below).

**Recalc robustness:** `metrics_report.py` now clears its own report's stale
`.~lock.<file>#` before recalc (a killed soffice orphans one and LibreOffice
then silently refuses to open the file) and removes its temp UserInstallation
profile afterward (they were accreting in %TEMP%).

### FIRST REAL AXIS-C VERDICT (2026-07-23) — bge-m3 holds every track

Synthetic corpus (121 docs, 499 chunks, tenant `bench-synth`; see the corpus
spec) → three floors-meeting retrieval gold sets (60 leakage-guarded queries
each incl. 6 multi-hop, reviewed + activated by the operator) → 15 non-advisory runs
(5 embedders × 3 tracks) → frozen rules applied (`run_axis_c.py`: +0.02
margin, paired bootstrap CI excluding zero, incumbent wins ties):

| track | bge-m3 R@10 | best challenger | diff | verdict |
|---|---|---|---|---|
| sop | 0.917 | nomic-embed-text 0.933 | +0.017, CI [0.000,+0.050] | below margin — no |
| prose | 0.967 | nomic-embed-text 0.983 | +0.017, CI [0.000,+0.050] | below margin — no |
| communication | 0.900 | (none better) | — | no |

**INCUMBENT STAYS. No pin-profile change.** Honest observations recorded, not
acted on:
- **nomic-embed-text is the real contender**: within noise of (or a hair
  above) bge-m3 on 2 of 3 tracks at 768 dims and ~35% less wall time. It did
  NOT clear the frozen displacement margin. Note the doc's "cheap wins among
  within-2pt configs" clause is in tension with the incumbency rule here; the
  incumbency reading governed (per §0.3). A rematch consideration, not a
  verdict change.
- **snowflake-arctic-embed's collapse (0.37–0.60) is likely a usage
  artifact**: arctic (and nomic) officially want asymmetric query/document
  task prefixes; the evaluator ran all models bare/symmetric. Per-model
  prefix support is a legitimate new config knob for a future round — it
  would be a NEW config, compared under the same rules, not a re-score.
- bge-m3 ran dense-only; its hybrid (dense+sparse) "best self" awaits the
  retrieval serving path. qwen3-embedding (4096-dim) underperformed and costs
  ~2× wall — no case for it here.
- The gold sets are model-generated (sme_authored: 0, recorded in spec);
  The operator activated after sample review. A 0.2 set with SME-authored queries
  strengthens any future rematch.

### ROUND 2 — prefix-aware rematch (Build Prompt 7, 2026-07-23). ROUND 1 SUPERSEDED.

Round 1 fed raw text to every embedder; only bge-m3 is prefix-free by design,
so 4 of 5 challengers ran off-label and the round was structurally tilted
toward the incumbent. Round 1's 15 runs are NOT deleted or re-scored — their
config JSONB honestly records `bare/dense` — but all now carry
`superseded_by_run_id` (9, linked to same-model round-2 runs) or note-only
supersession (6: arctic + mxbai, not fielded this round), and the leaderboard
flags them `(superseded)` (migration 007).

**The prompt_style knob:** per-model OFFICIAL retrieval formats, sourced from
the model cards (exact strings + source URLs in `benchmark.PROMPT_STYLES`;
denormalized into every run's config as `prompt_style_detail` — caller-
supplied detail is refused, it derives from the registry):

| style | query side | doc side | source |
|---|---|---|---|
| none (bge-m3) | — | — | BAAI/bge-m3: prefix-free by design |
| nomic-search | `search_query: ` | `search_document: ` | HF nomic-ai/nomic-embed-text-v1.5 |
| qwen3-instruct | `Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery: {query}` | plain | HF Qwen/Qwen3-Embedding-0.6B + QwenLM GitHub |
| arctic-query | `Represent this sentence for searching relevant passages: ` | plain | HF Snowflake/snowflake-arctic-embed-m-v1.5 (in vocabulary; not fielded) |
| mxbai-query | same as arctic | plain | HF mixedbread-ai/mxbai-embed-large-v1 (in vocabulary; not fielded) |

**Field (dense-only, same gold sets synth-*-0.1, same frozen rules):** bge-m3
(none) vs nomic-embed-text (nomic-search) vs qwen3-embedding (qwen3-instruct).
bge-m3 hybrid deliberately NOT built: dense-vs-dense is the fair fight; hybrid
only becomes necessary if a challenger beats bge-m3 dense (it didn't).

**Results (R@10; paired diff CI vs incumbent):**

| track | bge-m3 | nomic [prefixed] | qwen3 [instruct] | verdict |
|---|---|---|---|---|
| sop | 0.917 | 0.850 (−.067, CI[−.133,−.017]) | **0.950** (+.033, CI[+.000,+.083]) | margin met but CI touches 0 — no |
| prose | 0.967 | 0.917 (−.050) | 0.950 (−.017) | no |
| communication | 0.900 | 0.867 (−.033) | 0.867 (−.033) | no |

**VERDICT: bge-m3 HOLDS ALL TRACKS — this time in a fair fight. Incumbent
stays; no pin-profile change.**

Honest observations:
- **qwen3-embedding's instruct template is worth +5 to +13 points** over its
  bare round-1 runs (0.883→0.950 sop, 0.817→0.950 prose, 0.750→0.867 comms) —
  round 1 was measuring prompts, not models, exactly as suspected. It is now
  the closest challenger, at 4096 dims and ~1.5–2.5× bge-m3's wall time; on
  sop it cleared the +0.02 margin but the paired CI touched zero (60 queries
  — a larger gold set could resolve it either way).
- **nomic's official prefixes made it WORSE on this corpus** (sop 0.933→0.850,
  prose 0.983→0.917 vs bare). The strings match its card exactly (both
  configs recorded; compare `prompt_style_detail`). No evidence of harness
  misconfiguration — but "official usage" is evidently not universally better
  on terse-query/markdown-chunk synthetic data. Open observation, not a
  verdict input: neither nomic config clears the margin in either direction's
  reading.
- Mis-configuration check, per model: bge-m3 correct (prefix-free); qwen3 now
  per-card; nomic per-card (see caveat above); arctic/mxbai still only have
  bare superseded runs — if ever fielded again, use their query prompts.
- Still not instrumented: per-query latency p95 (the §4 constraint) — runs
  record wall_ms only. Carried TODO.
- No new dependencies; nothing bundle-affecting beyond migration 007 (picked
  up by install-ubuntu.sh's glob) and package 0.6.0 code.

### ROUND 3 — bge-m3 HYBRID vs DENSE (Build Prompt 8, 2026-07-23)

**Verdict: DENSE HOLDS all three tracks. Hybrid recorded as tested-and-not-
adopted — a measured "we didn't need it," and on sop a measured "it actively
hurt." No pin-profile change; rounds 1–2 untouched (this was a follow-on,
not a supersession).**

**Sparse source (the honesty requirement):** Ollama's embed API is dense-only
(verified: `EmbedResponse` has no lexical field), so bge-m3's LEARNED sparse
comes from **FlagEmbedding 1.4.0** (BAAI's own library, fp16, pinned to one
GPU — multi-device spawns a multiprocessing pool that deadlocks under Windows
spawn, observed and worked around). New deps recorded in requirements.txt +
lock; Windows needs torch==2.13.0+cu130 from the pytorch index (docling's
resolution yields +cpu, which would have rigged the latency gate against
hybrid by timing its sparse on CPU vs the incumbent on GPU).

**Field (per track):** ollama-dense (incumbent) · flagembedding-dense
(engine diagnostic) · flagembedding-hybrid (RRF, k=60, mode+fusion params in
config JSONB). Weighted fusion exists behind the knob (`sparse_weight`),
deliberately NOT tuned this round.

**Results (R@10; paired diff CI vs incumbent; p95 per-query latency —
instrumented this round, both engines timed the same way):**

| track | dense | hybrid rrf | diff CI | p95 dense/hybrid |
|---|---|---|---|---|
| sop | 0.917 | **0.717** | [−.317,−.100] — significantly WORSE | 144ms / 18ms |
| prose | 0.967 | 0.950 | [−.083,+.033] — noise | 148ms / 18ms |
| communication | 0.900 | 0.917 | [−.067,+.100] — noise | 149ms / 17ms |

- **Latency was NOT the deciding factor on any track** — accuracy was.
  Hybrid's p95 (17–18ms in-process) is far under the 300ms gate; the
  incumbent's own p95 (~145ms) is the Ollama HTTP hop, also comfortably in.
- **Engine confound: zero.** The diagnostic (fp16 HF dense vs Ollama GGUF
  dense) produced IDENTICAL hit@10 on every item of every track
  (diff CI [0.000, 0.000]) — the hybrid delta is entirely the sparse fusion.
- **Why hybrid lost on sop (hypothesis, recorded not asserted):** the corpus
  is identifier-heavy AND each identifier recurs across many chunks of the
  same SOP (285 sop chunks from 20 documents — every step of a cleaning SOP
  mentions its EQ-*). Sparse therefore boosts whole families of same-
  identifier sibling chunks, and under RRF that crowd displaces the one
  relevant (paraphrased) chunk from the top-10. Note also the structural
  bias the other way: gold queries are leakage-guarded (near-verbatim
  rejected), which suppresses exactly the lexical overlap sparse rewards —
  real user queries that paste verbatim identifiers may behave differently.
  Hybrid's MRR/nDCG were actually *better* on prose and comms, so sparse
  helps ordering when it doesn't flood.
- Honest caveats: RRF with k=60 was the only fusion tried (per the build
  prompt — no dial-tuning); a low-`sparse_weight` weighted fusion or an
  identifier-aware sparse filter are future configs, to be run under the
  same rules only if retrieval quality on REAL data ever motivates it.
- The smoke test's prior (sparse rewards exact identifiers 40:1 on a toy
  pair) was true AND the conclusion still went the other way at corpus
  scale — which is exactly why priors don't decide rounds here.

### The campaign's real blocker — a representative corpus (RESOLVED 2026-07-23)

The harness is ready; ground truth is not. The pilot holds ONE document
(SOP-014), so no gold set can meet the §6.2 floors (retrieval ≥ 50 queries
per track, ER ≥ 200 pairs per high-stakes type, extraction ≥ 30 parents).
Until a representative multi-document corpus per data track is loaded, every
run is honestly `advisory`. Sourcing that corpus is the gating decision for
real Axis-C/D numbers (Axis A also needs the 1M-vector synthetic inflation;
Axis B needs keyed SoR sources for T0 label positives).

## Gold-set generators (privacy fork intact)

Real tenant data only ever meets LOCAL models; this stage generated on
synthetic seed only.

* `SyntheticRetrievalGenerator` — 5-chunk deterministic corpus through the
  REAL tables (raw_documents → documents → parent → embedded children, live
  bge-m3), queries asserted past the leakage guard at generation time.
* `LLMQueryGenerator` — local-model realistic queries with the Jaccard ≥ 0.6
  leakage rejection + retry cap (tested live against qwen3.6 on a synthetic
  chunk).
* `ERGoldGenerator` — `from_labels()` exports the flywheel (the pilot's one
  human hard negative comes through with source + authority);
  `corruption()` makes suffix/abbreviation/typo positives + same-first-token
  hard negatives, deterministic under an explicit seed.
* `ExtractionGoldDrafter` — drafts expected-facts items from pending_facts +
  quarantine so the SME reviews rather than authors; drafted items carry
  `reviewed: false` and live on a DRAFT set until a human activates.

## The dry-run (Deliverable 4) — recorded on the pilot DB

Tenant `bench-dryrun`, gold set `retrieval/dryrun-0.1` (5 items, floors NOT
met → advisory by construction), one c_embedder run of the incumbent:

```
run 2 recorded (wall 2281 ms)
model digests {'bge-m3': '790764642607', 'qwen3.6': '07d35212591f'}
package 0.6.0, code_hash 0054bfe6ae37…, pg 16.14, 2x RTX PRO 6000
recall_at_10_any=1.0 (5-vector corpus; k >= corpus size makes this a
recording check, not a result), CI [1.0, 1.0]
aggregates recompute from benchmark_run_items exactly
leaderboard returns the run under (gold set, pin profile)
second invocation: duplicate refused as designed
```

**Provenance catch worth keeping:** the first dry-run recorded
`package_version=0.0.1` — the editable install's metadata predated the real
pyproject. Fixed by reinstalling (uv) at 0.6.0 and re-recording; the
harness was honest, the environment was stale. On the Ubuntu boxes,
install-ubuntu.sh installs the package fresh, so metadata matches by
construction.

## Reporting

`metrics_report.py` gained Raw_BenchmarkRuns + Raw_Leaderboard sheets and a
**Benchmarks** dashboard sheet: one chart per (axis, gold set, pin profile)
group — the comparability rule made physical; configs are never charted
across conditions. Advisory runs are labeled on the bar. New campaign runs
appear on re-run of the report with zero code changes.

## Reproducibility

No new dependencies (numpy rides in with pandas; openpyxl/psutil were
promoted to direct in requirements.txt earlier today). Migration 006 applied
to the pilot DB + tracked; install-ubuntu.sh replays it by glob.
check_stack.py check 8 verifies tables + seeded pin profile + computable
code_hash/fingerprint + a queryable leaderboard. The infra folder is still
not a git repo — runs pin code identity by package version + source-tree
hash; `git init` remains the better answer eventually.
