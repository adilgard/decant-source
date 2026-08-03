# AGE graph projection — RETIRED (Build Prompt 9, 2026-07-24, pkg 0.20.0)

The Apache AGE projection of entities+facts is **frozen off**:
`settings.project_to_age = False` (the default, and the deployed state).
`write_facts`, merge/reversal maintenance (`project_fact`,
`delete_fact_edge`, `delete_entity_vertex`) are gated no-ops. The code is
kept intact as frozen reference — this is a retirement, not a teardown.
The `age` extension and the `knowledge_hub` graph stay installed (harmless
unused; check_stack's extension probe still passes), and the
ag_catalog-LAST `search_path` ordering stays (still correct).

## Why (in decisive order)

1. **The projection is KNOWN-STALE, not merely unused.** The temporal spine
   (BP7 tombstone propagation, BP8 re-version supersession) retracts facts
   by mutating `facts.valid_to` relationally. The projection wrote edge
   `valid_to` once, at fact-write time, and was never updated on
   retraction — so any pre-freeze edges carry WRONG temporal data. A
   projection that would serve incorrect data to any future reader is
   worse than no projection.
2. **Nothing reads it** (grep-verified at BP9, and structural since S2):
   serve-path traversals are recursive SQL over `facts` through the choke
   point — AGE `cypher()` cannot take bind parameters, so it could never
   transit the gate; resolver Tier-1c corroboration counts over the
   relational `facts` table with its own `valid_to IS NULL` filter (so it
   respects retraction correctly, which the graph never could). The only
   remaining `run_cypher` callers are tests/diagnostics.
3. So the projection was pure write-path cost: one cypher MERGE round-trip
   per entity-entity fact (plus merge-time edge/vertex maintenance),
   maintaining the AGE 1.5.0 MERGE-map workaround, to produce edges that
   were already wrong.

Graph *algorithms* (shortest-path, clustering, visualization) are a later,
different project's concern — out of scope for this ingest-and-serve
system, and nothing about this freeze forecloses them.

## What got lighter

Per entity-entity fact write: one `ag_catalog.cypher()` statement (two
vertex MERGEs + one edge MERGE with escaped literals) — gone. Per merge:
one edge-delete + one vertex-delete cypher round-trip each, plus
re-projection of every repointed fact — gone. No relational behavior
changed anywhere; `facts` was authoritative before and after.

## Resurrection is a PROJECT, not a toggle

The flag exists so the frozen code stays runnable as reference, **not** so
anyone flips it on. The projection has diverged from the architecture;
re-enabling requires, at minimum:

1. **Rebuild from facts.** Existing edges are stale by construction —
   never trust them; `TRUNCATE`-equivalent the graph and re-project from
   the authoritative rows (facts are the source of truth, so a rebuild is
   always safe and deterministic).
2. **Wire the temporal spine to edges.** Retraction/supersession
   (`pipeline._retract_facts_for_documents` and revival) must update edge
   `valid_to` — which reopens the AGE 1.5.0 bug (a `SET` on a
   MERGE-created edge is silently dropped; the workaround was
   properties-in-the-MERGE-map, which only works because writes were
   insert-only — updates need a delete+re-MERGE or a fixed AGE).
3. **Solve cypher parameterization for the choke point** (the S2 finding)
   before any serve-path reader exists — otherwise the graph stays
   internal-only and point 2 is maintenance without a consumer.

Until someone signs up for all three, `project_to_age` stays False.
