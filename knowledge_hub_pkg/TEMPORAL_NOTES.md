# Tombstone propagation (temporal axis wired) — notes

Delivered 2026-07-24 (pkg 0.18.0, migration 009, 245 tests green incl. 9 new,
check_stack 10/10; migrations 008+009 applied AND recorded on the local pilot
DB — 008 had been test-DB-only until now). Closes the §8.5 BP6 / §8.11
follow-on: a source deletion now carries raw_documents.deleted_at →
documents.valid_to → facts.valid_to in one transaction, and the serving layer
excludes non-current items at the choke point by default.

## The honest finding first

The kickoff said "re-versioning already uses valid_to to supersede a changed
document's old facts — find that path and extend it." **That path did not
exist.** valid_to was set by NOTHING anywhere in the codebase; it was only
ever *read* (per-op inline `valid_to IS NULL` predicates, the facts_current
view, entity merge retirement on the separate identity axis). §8.1g designed
re-version supersession; no build prompt had implemented it. So this build
created the shared retraction primitive rather than forking a phantom:
`Pipeline.tombstone_raw` is now THE valid_to writer, discriminated by
`retraction_reason` — `'source_tombstone'` (this trigger) vs `'superseded'`
(reserved; the re-version wire-up plugs into the same columns and the same
serve-side filter with zero new machinery). One mechanism, two triggers,
never a parallel deletion path — honored in spirit by making sure the SECOND
trigger has nothing left to invent.

## Propagation (delete + revive), pipeline.py

- One transaction, one shared timestamp: raw.deleted_at == documents.valid_to
  == facts.valid_to for a given tombstone. Nothing physical is deleted at any
  layer (WORM/retention owns bytes; rows stay for audit).
- **Per provenance link, structurally:** a facts row carries exactly ONE
  anchor (chk_provenance_present: source_document_id, else its chunk's
  document), and `write_facts` never dedups — a multi-source assertion IS
  sibling rows, one per source. Retracting the deleted doc's row leaves the
  assertion served through the survivor. **Tripwire:** if a future change
  ever dedups fact rows across sources, retraction must become
  link-set-aware (only retract when NO current source remains) — the test
  `test_multi_source_fact_survives_losing_one_source` is the guard that will
  go red.
- Revival reverses ONLY `retraction_reason='source_tombstone'` rows: valid_to
  set by any other writer survives a delete→revive cycle (tested with a
  pre-superseded row). Delete→revive is a proven identity on the served
  result.
- **Eager, not lazy — a justified divergence from §8.1g's lazy option:** the
  lazy/eager split was designed for re-EXTRACTION cost (LLM per item).
  Retraction is one UPDATE per layer with no per-item model cost, and a
  deleted doc still serving is a correctness/permissions hazard per §8.11 —
  so propagation is always immediate. There was also no existing re-version
  behavior to match (see honest finding).
- Chunks and mentions deliberately carry no temporal columns: a chunk's
  currency IS its document's (evidence templates gate on the joined
  document's valid_to), and mentions are audit/replay data. The pending_facts
  gate is a JOIN-time guard in promote_pending (a retracted anchor document
  blocks promotion; revival re-enables it) — no state mutated, reversible by
  construction.

## Serve-time filter (choke point), the {cur:} marker

- New marker, same discipline as {sec:}: `{cur:alias}` expands to
  `(alias.valid_to IS NULL)` by default. Every FROM/JOIN of a temporal table
  (facts, documents) must be aliased and carry its {cur:} marker — refused
  otherwise at BOTH registration (OperationRejected) and the runtime gateway
  (EnforcementRefused), so a temporally unfiltered read is unwritable, not
  merely unlikely. Checked after the finer-grained template errors so those
  keep their messages. (Same honest limitation as {sec:}: the check is
  per-alias-name presence, not per-scope — an alias reused across CTEs needs
  the marker at each join, which the house templates do.)
- **Audit escape:** `include_retracted` on RetrievalQuery, carried into the
  tamper-proof proof snapshot at enforce() (flipping it post-mint =
  UnenforcedQuery, tested). Under audit scope the {cur:} predicate collapses
  to TRUE while every {sec:} predicate applies unchanged — deletion is a
  TEMPORAL state, never a permission: permission-hidden items stay silently
  absent in both scopes; retracted items return only when explicitly asked,
  labeled `state='retracted'` (new UncertaintyState, top precedence in
  fact_state_from_row — an S1 vocabulary change made deliberately; the
  pinned-enum test moved from five to six with a comment).
- Every base op accepts `include_retracted` implicitly (reserved query-level
  param, popped in CompiledOperation.run, advertised in the catalog's public
  view; authors may not declare it as an op param). /v1/retrieve accepts it
  too; S4 enrichment forwards the query's temporal scope to facts_citing so
  grounded facts match the window the evidence was served from.
- get_facts's old `include_expired` op param was REMOVED as subsumed: with
  currency enforced at the choke point, an op-local flag could never widen
  the scope anyway — keeping it would have been a dead knob lying about
  what it does.
- entities.valid_to is the IDENTITY axis (merge retirement) and deliberately
  stays out of {cur:} — ops keep their explicit inline entity predicates.

## Known-stale / deferred

- **AGE edge valid_to goes stale on retraction.** The graph projection
  writes valid_to into the MERGE map at fact-write time; retraction mutates
  the relational row only ("fact rows are insert-only" in
  factstore_pg._project_fact is now stale-in-spirit for valid_to). Safe
  today: the serve path is SQL-only (S2 decision — AGE can't bind params)
  and internal corroboration (scoring_tiered) reads facts SQL with its own
  valid_to IS NULL filter, so retraction propagates to corroboration
  correctly. Folds into the existing §5 "AGE graph sync" open question; do
  NOT hack per-edge cypher SET (AGE 1.5.0 drops SET on MERGE-created edges).
  **RESOLVED 2026-07-24 (Build Prompt 9): the projection is RETIRED — frozen
  off, not synced; this staleness was the decisive argument. AGE_DORMANT.md.**
- **Composites don't plumb include_retracted v1** — a composite call
  rejects the param (unknown param, honest error); audit reads go through
  base ops. Plumbing it through CompositeSpec/ParamBinding is a small
  follow-on if audit composites become a real need.
- **Re-version supersession wire-up** (§8.1g): ~~detect version N+1
  processed → retract version N with reason='superseded'~~ — **BUILT
  2026-07-24 as BP8, see the section below** (pkg 0.19.0).
- **What only real deletion volume will exercise:** retraction UPDATE cost
  on very large fact sets per doc (fine at pilot scale; if a storm-scale
  bulk delete ever needs batching, batch the CAPTURE loop, not the
  propagation transaction); revival races with a concurrently-running
  resolution sweep (single-sweeper-per-tenant assumption holds today);
  audit-query volume (usage instrumentation will show whether
  state='retracted' branches are ever read — the fold-later rule applies).

## Test coverage (tests/test_tombstone_propagation.py, real stack)

single-source retraction (default hidden; audit serves state='retracted',
doc/raw timestamps equal) · multi-source survival via sibling provenance
row (LOAD-BEARING) · delete→revive identity round-trip · revival never
resurrects other-writer valid_to · {cur:}-less templates unwritable at
registration AND refused at the gateway (aliasing enforced) · reserved
param undeclarable · audit scope tamper-proof (proof snapshot) · evidence
path excludes retracted docs' chunks, audit reaches them · retracted
pending facts don't promote until revival · full capture→serve trigger
chain via a scripted tombstone adapter.

---

# BP8 — Re-version supersession (the primitive's second trigger)

Delivered 2026-07-24 (pkg 0.19.0, no new migration — BP7's columns were built
for this; 253 tests green incl. 8 new, check_stack 10/10). Closes the §8.1g
re-version follow-on: when version N+1's facts promote, version N's facts and
document retire through the SAME retraction machinery, reason='superseded'.
Both ways a fact stops being true — deleted and edited — now ride one
temporal spine, and the {cur:} serve filter needed zero changes.

## Where the trigger lives, and why

**Promotion, not processing.** Facts become current at
`Pipeline.promote_pending` (extraction only stages), so that is the only
place a cutover can be atomic. Promotion is now grouped per anchor document,
and each group is ONE transaction (nested store calls become psycopg
savepoints on the shared per-DSN connection): the group's facts become
current, the anchor's prior-version facts/documents retire, all under one
shared cutover timestamp — the outer commit is the single moment the served
world changes, and a failure anywhere rolls the WHOLE group back (proven
with a poison pending: nothing partial persists, redelivery re-runs it).
Prior-version lookup rides the existing raw_documents version chain
(source_system + native_id, version < current) — a query, not new
infrastructure. Idempotent by construction: priors are looked up
`valid_to IS NULL`, so the first promotion wave for a new version supersedes
and later waves find nothing to retire. A superseded doc's LATE-resolving
pendings are blocked forever by BP7's promotion guard — emergent, tested.

**Primitive honesty note:** BP7's writer was tombstone-shaped (logical-doc-
keyed, reason hardcoded). Its retraction core was EXTRACTED into
`_retract_facts_for_documents` (document-scoped, reason/timestamp/keep-set
parameterized) and both triggers now call it — a parameterization inside the
same module, not a fork; still exactly one facts.valid_to writer.

## DIFF, not wholesale (the decision, justified)

A cutover diffs the new version's promotable triples against the prior
versions' CURRENT triples (subject, predicate, object_entity|object_literal):

- **Unchanged assertion → the surviving row stays.** Same fact id, valid_to
  never set — no spurious blink in the temporal record, and no duplicate
  row in the default serve. The new version's staging row records the
  re-assertion (promoted_fact_id -> the surviving fact), so the audit trail
  keeps the corroboration without churn.
- **Dropped assertion → retracted** ('superseded', valid_to = cutover).
- **New assertion → inserted** with valid_from = cutover, so old validity
  ends exactly where new validity begins (asserted equal in tests).

Why diff: the audit trail is a selling point, and wholesale would make every
re-saved document look like a mass retraction+re-creation — churn that buries
real change and makes "when did this become true" unanswerable. Cost of diff,
recorded honestly: (a) the surviving row keeps the OLD version's provenance
anchor (spine cites v1's doc/chunk — the text that first asserted it, which
is retained; the re-assertion lives in pending_facts); (b) triple identity
ignores `attributes` — an attributes-only change reuses the old row (fact
attributes are essentially unused by extraction today); (c) duplicate
same-triple rows WITHIN one version still both promote (per-chunk provenance
is intentional — dedup only runs against PRIOR versions); (d) ER noise:
if the resolver maps v2's mention to a different entity, the triple won't
match and the fact legitimately retires+recreates — under-merge bias
propagates here, by design.

## Eager/lazy re-extraction (§8.1g's split, applied where it belongs)

Deletion was eager-only (retraction is one UPDATE). Supersession's expensive
half is RE-EXTRACTION — the new version through the LLM — which is exactly
what §8.1g's lazy option priced. The lever is the outbox:
`PostgresDispatcher.dispatch(..., delay=)` defers available_at, and
ProcessingService applies the policy at the extraction dispatch — version 1
of anything and every track not configured lazy extract immediately;
a RE-VERSION (raw.version > 1) of a track in `lazy_reextract_tracks`
batches behind `lazy_reextract_delay` (default 1h). The staleness window is
the chosen price: the old version keeps serving until the deferred
extraction promotes and cuts over. The track list is constructor DATA
(§8.1g: domain experts decide what is high-stakes; nothing hardcoded) and
the default is eager-for-everything. Idempotent re-dispatch never resets an
existing message's schedule.

## Interaction with deletion (reasons stay independent — tested)

- Supersede → delete: tombstone takes the CURRENT facts (including diff
  survivors anchored to superseded prior docs — this exposed a real bug:
  tombstone_raw originally collected only documents its own UPDATE touched,
  so survivors anchored to already-'superseded' docs escaped deletion; fact
  retraction now anchors on ALL of the logical doc's documents). The
  already-'superseded' rows are never double-stamped.
- Delete → revive: only 'source_tombstone' reverses (BP7); superseded rows
  and superseded documents stay retired — the pre-delete serve returns
  exactly.
- **Byte-identical revert limitation (known, recorded):** reverting a doc to
  a prior version's exact bytes is INVISIBLE to capture (content-hash
  idempotency: same bytes = replayed no-op, no new version, no re-extraction)
  — the superseded facts stay retired and the newer version's facts keep
  serving. A real revert almost always differs (metadata, timestamps); a
  byte-perfect one needs the eTag/cTag work or an operator re-ingest. Not a
  BP8 regression — a raw-layer semantic that predates it.

## What only real edit volume will exercise

- Diff computation loads the prior versions' current triples per cutover —
  fine at document scale (hundreds of facts); a pathological doc with tens
  of thousands of facts would want the index built server-side.
- Re-extraction cost under bulk edits is the whole point of the lazy knob —
  tune `lazy_reextract_delay` against real churn, and watch extraction_queue
  depth; nothing auto-coalesces multiple re-versions of the same doc inside
  one delay window yet (idempotent dispatch means the SECOND re-version
  rides the first's message — the latest landed version is what extraction
  processes, so coalescing is actually free — but a v2-extract racing a v3
  landing re-extracts twice; harmless, priced).
- Group-transactional promotion holds every group's writes in one
  transaction — enormous single-document extractions widen the transaction;
  the sweep remains single-writer per tenant (BP7 note unchanged).

## Test coverage (tests/test_reversion_supersession.py, real stack)

diff cutover (changed retracts / unchanged same-row continuous / new
valid_from == old valid_to / no duplicates / staging rows record
re-assertion / audit serves state='retracted') · atomic rollback on poison
pending, then cure completes the identical cutover · multi-prior healing
(v1+v2 both current → v3 retires both) · supersede→delete→revive reason
independence (found the survivor-anchor bug) · evidence serves only the
current version's chunks (the pre-BP8 both-versions gap, closed + shown) ·
late v1 pendings never promote · eager/lazy policy + deferred dispatch not
claimable until available_at.
