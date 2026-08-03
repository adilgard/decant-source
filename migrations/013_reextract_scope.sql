-- ============================================================================
-- MIGRATION 013 — SCOPED RE-EXTRACTION (d.s Stage 3)
-- Applies ON TOP of 012. Additive only. Keep factstore_pg.py and
-- operator_jobs.py in lock-step (same commit).
-- ----------------------------------------------------------------------------
-- operator_job_documents is the per-document progress ledger for jobs that
-- iterate a FIXED document population (kind='reextract_scope'; Stage 2's
-- folder_ingest doesn't need one — capture's cursor + content hash already
-- make it resumable). The population is materialized ONCE per job
-- (INSERT ... SELECT ... ON CONFLICT DO NOTHING, so a resumed job never
-- re-scopes — the scope was fixed at creation, and a document ingested
-- AFTER the job started is never silently swept in), then each document is
-- processed and marked in its own transaction: a crash at document N
-- resumes at N+1 by claiming the rows still 'pending'.
--
-- Re-running a completed or partial job never double-writes: extraction is
-- keyed by (unit, extractor, model, ontology) in extraction_runs — the same
-- document under the same target version replays as a no-op.
--
-- No new retraction machinery here: superseding the OLD facts reuses the
-- reserved temporal axis (facts.valid_to + retraction_reason, migration
-- 009) with the third trigger value 'ontology_superseded' — written by
-- Pipeline._promote_document_group at promotion cutover, through the same
-- primitive as tombstones and re-version supersession. retraction_reason
-- carries no CHECK constraint by design (009), so the new value is data,
-- not DDL. Superseded facts are RETAINED and queryable ("what did this
-- document yield under version A vs B" = filter facts.ontology_version;
-- current vs superseded = valid_to IS NULL). Purging them is explicitly a
-- later build.
-- ============================================================================

BEGIN;

CREATE TABLE operator_job_documents (
    job_id           BIGINT NOT NULL REFERENCES operator_jobs(id),
    tenant_id        TEXT NOT NULL DEFAULT 'default',
    raw_document_id  BIGINT NOT NULL REFERENCES raw_documents(id),
    status           TEXT NOT NULL DEFAULT 'pending',
    error            TEXT,
    finished_at      TIMESTAMPTZ,
    PRIMARY KEY (job_id, raw_document_id),
    CONSTRAINT chk_job_doc_status CHECK (status IN
        ('pending', 'done', 'failed'))
);

-- The runner's resume scan: a job's still-pending documents, id order.
CREATE INDEX ix_job_docs_pending ON operator_job_documents(job_id,
    raw_document_id) WHERE status = 'pending';

COMMIT;
