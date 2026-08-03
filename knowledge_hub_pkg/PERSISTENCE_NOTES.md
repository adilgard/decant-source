# Persistence layer (Build Prompt 1) — reconciliation notes

Delivered: `knowledge_hub/interfaces.py` (FactStore contract), `knowledge_hub/factstore_pg.py`
(psycopg3 implementation), `knowledge_hub/pipeline.py` (persistence stubs),
`../migrations/001_persistence_addenda.sql`, and `tests/` (19 tests, real Postgres —
they rebuild a throwaway `kh_factstore_test` DB and never touch the pilot DB).

## models.py / schema mismatches reconciled (none silent)

1. **No `tenant_id` anywhere.** The spec requires every query to filter by tenant, but
   neither baseline v0.2 nor models.py had a tenant column. Migration 001 adds
   `tenant_id TEXT NOT NULL DEFAULT 'default'` to all core tables (additive; existing
   rows keep working). Idempotency keys widen to `(tenant_id, content_hash)` on
   raw_documents and chunks — the same bytes for two tenants are two rows, within a
   tenant still a no-op. The DB-per-tenant swap point is `PostgresFactStore._dsn_for`.

2. **No `version` column.** `_next_version` implies raw-doc versioning; migration 001
   adds `raw_documents.version` (default 1) + an index on
   `(tenant_id, source_system, source_native_id, version)`.

3. **Pre-resolution facts can't live in `facts`.** `facts.subject_entity_id` is a
   NOT NULL FK, but extraction hands over facts whose refs are still mention-keys.
   Migration 001 adds a `pending_facts` staging table (same provenance envelope +
   `subject_ref`/`object_ref` with grammar `mention:<id>` | `entity:<id>`).
   `stage_pending` rewrites extraction-local keys to `mention:<id>`;
   `Pipeline._rewrite_refs` + `promote_pending` move fully-resolved rows into `facts`.

4. **Views predated tenancy.** `facts_current` (`SELECT f.*` expands at CREATE time)
   and `review_queue` would have lacked `tenant_id`; migration 001 recreates both.

5. **models.py was a partial mirror.** Added the schema columns the placeholder
   dropped: Fact `char_start/char_end/locator` + `serialized_lines/oversized`;
   Chunk `speaker/event_time/embedding_version`; Entity `embedding_model/created_at`;
   EntityMention `char_start/char_end/locator/resolver_version/resolved_at`;
   Document `ingested_at`. Plus new models `EntityAlias` (Entity carries a transient
   `aliases` list persisted to `entity_aliases`, now unique per `(entity_id, alias)`)
   and `PendingFact` (mirrors the staging CHECKs).

## Implementation notes

- **AGE 1.5.0 gotcha — DOWNGRADED (BP9: projection RETIRED, off behind
  `settings.project_to_age`; see AGE_DORMANT.md).** Only relevant if you re-enable the
  projection: a `SET` on a MERGE-created edge is silently dropped (verified live —
  RETURN in the same statement even shows the value), so edge properties go **inside
  the MERGE map**: `[:REL {fact_id, predicate, tenant_id[, valid_to]}]`, keyed by
  `fact_id`. Temporal logic HAS landed since (BP7/BP8: retraction/supersession mutate
  facts.valid_to relationally) and never updated edges — which is exactly why the
  projection was retired as known-stale rather than synced.
- Graph projection (when enabled) runs in the same transaction as the fact insert —
  relational row and projection commit or roll back together. Relational is
  authoritative — always was; since BP9 it is also the ONLY live structure.
- Vectors travel as pgvector text literals + `::vector` casts (no extra client dep);
  `cypher()` can't take bind params, so graph statements are escaped literals in a
  `$kh$…$kh$` block.
- Oversized soft alarm: `serialized_lines` = pretty-printed JSON line count, flagged
  above 70 (spec says ~60–80), written intact, surfaces in `review_queue`.
- `install-ubuntu.sh` now applies `migrations/*.sql` idempotently (tracked in
  `schema_migrations`); migration 001 is already applied to the local pilot DB the
  same way. `check_stack.py` still all green.
- `_enqueue_review` feeds the three review_queue feeders: mention → status `review`;
  match → decision `review` (+ reason); fact → `oversized = true`.
