-- ============================================================================
-- MIGRATION 008 — TOMBSTONES (connector hardening: Graph as forcing function)
-- Applies ON TOP of 007. Additive only. Keep models.py in lock-step (same
-- commit): RawDocument.deleted_at.
-- ----------------------------------------------------------------------------
-- §8.1g deletion handling, capture side: an EXPLICIT delete signal from a
-- source (Graph delta @removed, CDC delete, webhook) soft-deletes the logical
-- document — deleted_at stamped on every version row of
-- (tenant, source_system, source_native_id). Never a hard delete: bytes stay
-- in the WORM store (retention/erasure policy owns their physical fate), the
-- rows stay for audit, and a re-observed upsert clears the stamp (revival).
-- Downstream propagation (facts valid_to, serving-time filtering on
-- deleted_at) is deliberately NOT here — capture records the authoritative
-- signal; reacting to it is the processing/serving layers' follow-on.
-- ============================================================================

BEGIN;

ALTER TABLE raw_documents ADD COLUMN deleted_at TIMESTAMPTZ;

-- Logical-identity index: tombstone/revival stamping and _next_version both
-- walk (tenant, source_system, source_native_id).
CREATE INDEX ix_raw_documents_logical
    ON raw_documents(tenant_id, source_system, source_native_id);

COMMIT;
