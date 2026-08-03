-- ============================================================================
-- MIGRATION 009 — TOMBSTONE PROPAGATION (retraction: the temporal axis wired)
-- Applies ON TOP of 008. Additive only. Keep models.py in lock-step (same
-- commit): Document.valid_to / Document.retraction_reason /
-- Fact.retraction_reason.
-- ----------------------------------------------------------------------------
-- Closes the §8.5 BP6 / §8.11 follow-on: capture tombstones a deleted source
-- doc (raw_documents.deleted_at, migration 008) but derived facts stayed
-- servable. Propagation reuses the RESERVED temporal axis (§2 #8): a
-- retracted fact/document gets valid_to = the deletion timestamp — retained
-- for audit, never physically deleted (the raw layer's tombstone-don't-erase
-- discipline, one level down).
--
-- retraction_reason is the reversibility discriminator: 'source_tombstone'
-- rows were retracted by tombstone propagation and are REVIVED (valid_to
-- cleared) if the source document reappears; rows whose valid_to was set by
-- any other writer (future 'superseded' re-version supersession, manual
-- temporal edits) carry a different/NULL reason and revival never touches
-- them. Documents get valid_to too: a chunk's servability IS its document's
-- currency (chunks stay column-free; the serve-path evidence templates gate
-- on the joined document's valid_to via the {cur:} marker).
-- ============================================================================

BEGIN;

ALTER TABLE documents ADD COLUMN valid_to TIMESTAMPTZ;
ALTER TABLE documents ADD COLUMN retraction_reason TEXT;
ALTER TABLE facts     ADD COLUMN retraction_reason TEXT;

-- Propagation walks raw_documents -> documents by raw_document_id.
CREATE INDEX ix_documents_raw ON documents(raw_document_id);

COMMIT;
