# Resolution — the tiered resolver (Build Prompt 5) — notes

Delivered: the `Scorer` seam in `knowledge_hub/interfaces.py` (+
BlockedCandidate / ScoredCandidate / ResolutionOutcome), the tiered
implementation `scoring_tiered.py` (T0 deterministic keys → T1
Splink/Fellegi-Sunter → T1b embedding+LLM with T1c graph corroboration,
banded via `resolution_policy`), the flow `resolution.py` (ResolutionService:
batch sweep, blocking, atomic apply, reversible merges, review decisions,
flywheel labels), `../migrations/005_resolution_flywheel.sql` (labels +
resolution_decisions; applied to the pilot DB, tracked in schema_migrations,
picked up by install-ubuntu.sh's glob), models for every table Stage D
touches, and 14 new tests (76 total, real Splink/DuckDB + live bge-m3 + live
qwen3.6 adjudication + Postgres/AGE, no mocks). check_stack.py gained check 7
(resolution). Package version 0.5.0. **This closes the vertical slice**: the
SOP-prose pilot now runs source → capture → parse/chunk/embed → extract →
resolve → facts in Postgres + the AGE graph, end to end.

**Read this first:** resolution quality is UNESTABLISHED. Green tests mean
the MACHINERY works — tiers route, policy bands, under-merge gates hold,
merges reverse, labels land, promotion replays idempotently. Whether the
resolver makes the RIGHT match/non-match calls is the ER benchmark's question
(Axis B), answered against a gold set that the labels store now accumulates.
Every threshold in `resolution_policy` and every weight/prior in
`scoring_tiered.py` (Splink m/u, cosine/name blend, corroboration boost,
adjudication score mapping) is a PLACEHOLDER until that calibration. Nothing
was tuned to make the pilot look good — see the sample run below, which
includes the resolver being confidently wrong and the machinery catching it.

## Cadence: a batch sweep, not a third outbox

T1+ wants batches (Splink is batch-oriented; blocking amortizes), and
`entity_mentions.resolution_status='pending'` already IS a queue with an
index on it (001's `ix_mentions_tenant`) — so no `resolution_queue` table.
`ResolutionService.sweep(tenant)` is re-runnable: each mention's verdict
applies in ONE transaction (candidate rows + decision row + mention update /
entity creation + labels), so a crash mid-sweep leaves the unprocessed rows
pending and the re-run picks them up; nothing double-applies. Mentions are
processed in id order so earlier resolutions feed later corroboration.
Assumption to revisit at scale: ONE sweeper per tenant (idempotency comes
from the per-mention transaction, not leases). Tier 0 also runs inline via
`resolve_mention()` — that path is what reversal re-resolution uses.
Resolution is ingestion-time and may lag extraction; reads never depend on
it (facts blocked by a review mention simply stay in pending_facts).

## Tier routing (cheapest correct tier wins)

* **T0 — deterministic keys.** Exact match on a strong `extracted_keys`
  value against `entities.attributes`. Strong = any confidently-extracted
  identifier (email / tax_id / customer_id / SoR key-columns like asset_id)
  EXCEPT `domain`, which is strong only for Organization — everyone at
  acme.com shares it. One hit → auto-resolve (score 1.0) + a
  `deterministic` er_match label, free ground truth. TWO+ hits → review AND
  the entity-entity pair is logged in match_candidates as a merge candidate:
  a key conflict means the registry itself probably holds duplicates.
* **T1 — Splink (DuckDB, link_only).** Mentions carrying any of the fixed
  comparison fields (email/domain/tax_id/customer_id). Scored per pair via
  `compare_two_records` against a per-tenant linker primed each sweep.
  Explicit m/u priors, NO training yet: u-estimation/EM belongs to benchmark
  calibration where the labels store supplies pairs — fixed priors keep the
  pilot deterministic and testable. The entity-side name is the best variant
  among canonical + aliases (aliases are earned surface forms).
* **T1b — embedding + name.** Thin/prose mentions: blend of pgvector cosine
  (P1's `ann_candidates`; the mention is embedded with bge-m3 on first touch
  and the vector persisted to `context_embedding`) and difflib name
  similarity. The GRAY residual only goes to local-LLM adjudication
  (qwen3.6, schema-constrained same_entity/confidence, temp 0, capped at 3
  calls per mention) — high and low bands never pay for a model call.
* **T1c — graph corroboration.** Distinct shared-edge neighbors between the
  candidate and entities co-resolved from the same document (counted over
  `facts` — relational is authoritative, the graph is its projection).
  Bounded boost (+0.05/edge, cap +0.10) and the gate for
  `requires_corroboration`.

Banding reads `resolution_policy` as DATA at sweep time — retuning is an
UPDATE. A missing policy row gets a conservative fallback that never
auto-merges.

## Bias to under-merge (structural, not aspirational)

Silent merges are reachable only through: T0 single-hit, or high band +
auto_merge_allowed + not-multiple-high + (key overlap OR corroboration OR
policy doesn't require it). Everything else — gray band, key conflict,
multiple high candidates, name-only-without-edge on high-stakes types,
auto_merge_allowed=false, unknown type — lands in new_entity or review.
Identifiers outrank names: only `key_overlap` (not name agreement, not the
adjudicator's opinion) satisfies the corroboration requirement short of an
actual graph edge. Consequence observed in tests: two legitimate same-name
"Zenith Widgets" Organizations coexist after a human `resolve_as_new` — the
registry tolerates homonyms rather than fusing them.

## Reversible merges

`merge_entities` snapshots the absorbed entity INTO `entity_merges` before
touching anything: the row (embedding included), its aliases, exactly which
aliases/attribute keys transferred to the survivor, the repointed mention
ids, fact ids (with side), and rewritten `entity:<id>` pending refs. Then it
moves aliases (dedup via ON CONFLICT), backfills attributes
(existing-survivor values win), repoints mentions/facts/pending refs,
re-roots any older merge whose survivor is now absorbed, deletes the row,
and fixes the graph (DETACH DELETE the absorbed vertex, re-project each
repointed fact through the AGE 1.5.0 MERGE-map workaround). All in one
transaction. `reverse_merge` restores the entity under its ORIGINAL id,
undoes exactly the transfer the snapshot recorded, repoints facts + graph
edges back, resets the absorbed side's mentions to pending and re-resolves
them against the split registry, and writes the `er_nonmatch` reversal
label — the hard negative. Known gaps, accepted and visible: (1) pending
facts that PROMOTE between merge and reversal carry the survivor's id and
are not repointed back; (2) mentions newly resolved to the survivor AFTER
the merge stay with the survivor — both are what the reversal label exists
to let the benchmark find.

## The flywheel (migration 005)

`labels` is written by: T0 auto-merges (er_match, source=deterministic,
authority 1.0), review decisions via `decide_match`/`resolve_as_new`
(er_match/er_nonmatch, human_review, 0.9), and merge reversals (er_nonmatch,
reversal, 0.95). "Build a gold set" is now `SELECT ... FROM labels`. Honest
observation from the pilot doc: **a prose-only corpus produces zero
deterministic labels** — SOP-014's mentions carry no strong keys, so the
free-positives stream only starts when keyed SoR sources land (or humans
decide reviews). The one label the pilot DB now holds is a human hard
negative, and it is a good one (below).

## Observability (the Axis-B signal)

`resolution_decisions`, one row per resolver pass per mention: tier, method,
score, band, decision, entity, winning candidate, resolver_version
(`tiered-0.1/splink-4.0.16/adj-qwen3.6`), wall_ms, and features carrying the
deterministic evidence (key_overlap / name_sim / cosine / corroboration /
Splink match_weight / adjudication verdict+confidence). `match_candidates`
keeps the pair-level log with the same features. Between them the benchmark
can replay every decision the resolver ever made, including the ones with no
candidates at all.

## AGE at volume (the P1 workaround, exercised)

This stage is the first to push the edge-write path hard: 26 promoted facts
(9 entity-entity edges projected), plus merge/reversal re-projection
delete/re-MERGE cycles in the tests. The MERGE-map workaround (edge props
inside the MERGE map, never SET) held throughout — no dropped properties
observed. Edge deletion by property map (`MATCH ()-[r:REL {fact_id: N,
tenant_id: t}]->() DELETE r`) and DETACH DELETE both behave on 1.5.0. Watch
item unchanged: fact rows are insert-only so the stable-map assumption
stays valid; anything that ever mutates a projected fact's predicate must
re-project.

## Sample run (pilot stack, tenant `default`, SOP-014 residue, 2026-07-22)

```
sweep: swept=23 resolved=22 (new_entities=22) review=1 errors=0 by_tier={none: 5, t1b: 18}
promotion: 24 facts -> facts + graph (9 entity-entity edges); 2 blocked by the review mention
entities: 22 created (SOP-014, Building A facility, QA reviewer, batch record, ...)
labels after sweep: NONE (prose corpus, no strong keys — see flywheel note)

the review item (mc[98], the machinery catching a confident wrong answer):
  mention 'release signature' (chunk 7)  vs  entity 'signed release certificate'
  name_sim=0.558 cosine=0.868 -> base ~0.71 -> GRAY -> adjudication
  qwen3.6: same_entity=TRUE confidence=0.95 -> score 0.975
  Document policy t_high=0.98 -> STILL GRAY -> review   (a signature is not
  the certificate — the adjudicator was plausibly, confidently wrong, and
  the band held the under-merge line)
human decision: decide_match(same=False) + resolve_as_new
  -> er_nonmatch label (human_review), mention -> new entity 23,
  -> remaining 2 facts promoted: 26/26 promoted, 0 pending
```

Note for calibration: the adjudication score mapping (0.5 + conf/2) tops out
at 1.0 but sits at 0.975 for conf 0.95 — under the placeholder Document
t_high of 0.98 the LLM can effectively never auto-merge a Document. Given
the verdict above was WRONG at conf 0.95, that accident currently works in
our favor; the benchmark should decide deliberately.

## What resolution revealed about upstream (feed to ontology + extraction)

* **Registry pollution by document parts.** "Appendix A", "limits table",
  "hold log", "completed review checklist" all became canonical Document
  entities. ER over these is ill-posed (every SOP has an Appendix A). The
  real ontology needs either a DocumentSection/Record type with its own
  (strict) policy row, or extraction guidance that document parts are not
  registry entities.
* **Role-shaped Person entities.** "QA reviewer", "production lead", "QA
  manager" are ROLES, not people. Matching "QA reviewer" across documents
  as one Person is wrong-by-design; the ontology needs a Role type (or
  Person mentions need name-shaped surfaces only).
* **Corroboration rarely fires on prose.** Every pilot decision had
  corroboration=0: role/duty facts came out as literals (P4's known issue),
  so there are few entity-entity edges to corroborate with. The
  requires_corroboration gate therefore currently routes most high-band
  name matches to review — correct posture, but the review queue will grow
  until the ontology gives relations entity-valued objects.
* **Near-collision surfaces are real.** 'release signature' vs 'signed
  release certificate' is exactly the shape of case the gray band + human
  loop exists for; expect more once multiple SOPs land.

## Reproducibility

New runtime deps splink/duckdb/pandas were already pinned in
requirements.lock.txt; `splink>=4` added to the package's pyproject (now
0.5.0). Migration 005 applied to the pilot DB + recorded in
schema_migrations; install-ubuntu.sh replays it by glob. check_stack.py
check 7 exercises the pieces most likely to differ on the Ubuntu boxes:
policy/labels tables present, Splink+DuckDB scoring with the shipped priors,
and one live schema-constrained adjudication call. `Settings` gained
`adjudication_model` (default qwen3.6, `OLLAMA_HOST`-routed like the rest).
