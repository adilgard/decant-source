-- ============================================================================
-- MIGRATION 012 — OPERATOR JOBS: console-triggered background work (d.s Stage 2)
-- Applies ON TOP of 011. Additive only. Keep factstore_pg.py in lock-step
-- (same commit): insert_job / claim_next_job / update_job / get_job /
-- list_jobs / requeue_stale_jobs.
-- ----------------------------------------------------------------------------
-- This is the pull-request queue operator_http.py bookmarked as NOT built
-- ("start_pull ... needs a pull-request queue the capture runner consumes").
-- An operator action (audited, via the write gate) INSERTS a job row; a
-- single background runner (operator_jobs.JobRunner, plain deterministic
-- code — the LLM appears only inside per-document extraction) claims and
-- executes it, writing progress back onto the row. The console polls the
-- row. Orchestration state lives HERE, in Postgres — never in process
-- memory — so a crashed runner resumes by re-claiming, and every claim is
-- FOR UPDATE SKIP LOCKED (the house outbox pattern from 002/004).
--
-- Stage 2 ships kind='folder_ingest'. Stage 3 adds kind='reextract_scope'
-- (plus its per-document progress table) — which is why `kind` carries NO
-- CHECK constraint: registered kinds are validated in code, so a new kind
-- is an INSERT-side extension, not an ALTER (extend, never modify).
--
-- params is the job's FIXED contract, resolved at creation time: for
-- folder_ingest it includes the ontology_version the operator chose (or
-- the active one at creation), which the runner stamps onto every document
-- it lands. A job never reads a global "current" setting mid-run — the
-- split-brain defense Stage 3 depends on, built where Stage 2 needs it.
-- ============================================================================

BEGIN;

CREATE TABLE operator_jobs (
    id           BIGSERIAL PRIMARY KEY,
    tenant_id    TEXT NOT NULL DEFAULT 'default',
    kind         TEXT NOT NULL,                 -- 'folder_ingest' (012); Stage 3 adds more
    params       JSONB NOT NULL DEFAULT '{}'::jsonb,  -- fixed at creation, never mutated
    status       TEXT NOT NULL DEFAULT 'queued',
    counts       JSONB,                         -- run summary, written by the runner
    error        TEXT,                          -- terminal failure, if any
    created_by   TEXT,                          -- operator principal (audit has the rest)
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at   TIMESTAMPTZ,
    finished_at  TIMESTAMPTZ,
    CONSTRAINT chk_job_status CHECK (status IN
        ('queued', 'running', 'done', 'failed'))
);

-- The runner's claim scan: oldest queued first, one at a time.
CREATE INDEX ix_jobs_claim ON operator_jobs(id) WHERE status = 'queued';
-- The console's listing: a tenant's recent jobs, newest first.
CREATE INDEX ix_jobs_tenant ON operator_jobs(tenant_id, id DESC);

COMMIT;
