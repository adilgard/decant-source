-- ============================================================================
-- MIGRATION 018 — FACTS SOURCE-CHUNK INDEX
-- Applies ON TOP of 017. Additive only. 017's sibling: promotion's
-- by-document reads carry an OR branch for chunk-anchored facts
-- (source_document_id IS NULL AND source_chunk_id IN (...the document's
-- chunks...)). With only 017 in place that branch still forces the scan —
-- the planner needs BOTH sides of the OR indexed to BitmapOr them.
--
-- One statement, CONCURRENTLY, own file — the same runner constraint
-- documented in 016/017. If a concurrent build fails: DROP INDEX IF
-- EXISTS ix_facts_source_chunk; re-run apply.
-- ============================================================================

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_facts_source_chunk
    ON facts (source_chunk_id);
