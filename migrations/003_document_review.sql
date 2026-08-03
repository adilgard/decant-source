-- ============================================================================
-- MIGRATION 003 — DOCUMENT REVIEW FEEDER (Build Prompt 3)
-- Applies ON TOP of 002. Additive only. Keep models.py in lock-step (same
-- commit): Document.review_status / Document.review_reason.
-- ----------------------------------------------------------------------------
-- Why: §8.1a (tag-as-claim) — a source's declared data_track is a CLAIM, not
-- ground truth. Stage B runs cheap per-document shape detection against it;
-- a CONFIDENT disagreement must be flagged to review_queue (never silently
-- auto-override the human tag, never blindly obey it). The existing feeders
-- (entity_mentions / match_candidates / facts) all flag rows that exist for
-- other reasons; a track mismatch is a property of the DOCUMENT, so the
-- documents table needs the feeder columns the review_queue view filters on.
-- ============================================================================

BEGIN;

ALTER TABLE documents ADD COLUMN review_status TEXT NOT NULL DEFAULT 'none';
ALTER TABLE documents ADD COLUMN review_reason TEXT;
ALTER TABLE documents ADD CONSTRAINT chk_document_review
    CHECK (review_status IN ('none', 'review', 'resolved'));
CREATE INDEX ix_documents_review ON documents(tenant_id)
    WHERE review_status = 'review';

-- Rebuild the unified review queue with the document feeder included
-- (a view's SELECT list is frozen at CREATE time — same reason 001 rebuilt it).
DROP VIEW review_queue;
CREATE VIEW review_queue AS
SELECT 'mention'::text AS kind, id AS ref_id, entity_type AS context, created_at, tenant_id
  FROM entity_mentions WHERE resolution_status = 'review'
UNION ALL
SELECT 'match'::text,   id, match_method,      created_at, tenant_id
  FROM match_candidates WHERE decision = 'review'
UNION ALL
SELECT 'oversized_fact'::text, id, predicate,  ingested_at, tenant_id
  FROM facts WHERE oversized
UNION ALL
SELECT 'document'::text, id, review_reason,    ingested_at, tenant_id
  FROM documents WHERE review_status = 'review';

COMMIT;
