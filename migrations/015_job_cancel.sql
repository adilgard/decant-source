-- ============================================================================
-- MIGRATION 015 — JOB CANCELLATION
-- Applies ON TOP of 014. Additive only. Keep factstore_pg.py, operator_jobs.py
-- and operator_http.py in lock-step (same commit).
-- ----------------------------------------------------------------------------
-- WHY. There was no way to stop a running job. On 2026-08-04 a full-title
-- ingest was started against the wrong folder (the corpus PARENT, with recurse
-- on, so it swept a 55MB whole-title file in alongside the 287 chapter files)
-- and the only way to stop it was to kill the process hosting the runner
-- thread. That leaves the job row stuck at 'running', which
-- requeue_stale_jobs() then flips back to 'queued' the moment a console
-- starts — so the wrong run comes back by itself, unattended. Pausing the
-- SOURCE looks like it should help and does not: pause_source sets a registry
-- status that future capture sweeps read, and a job already past capture never
-- looks at it again.
--
-- A FLAG, NOT A NEW STATUS. chk_job_status allows queued|running|done|failed
-- and those four stay exactly as they are; nothing that reads job status has
-- to learn a fifth value. A cancelled job ends 'failed' with its reason in
-- `error`, which is what it is: it did not finish its work.
--
-- COOPERATIVE, NOT A KILL. The runner reads this flag at each drain-pass
-- boundary and stops there, having written its counters. It is deliberately
-- NOT an interrupt: killing a worker mid-document is how you get a half-landed
-- document, a lease nobody will release, and counters that describe neither
-- the work done nor the work left. The cost is that cancellation lands within
-- one pass rather than instantly, which for a queue-drain pass is seconds.
-- A queued job that has not started needs no cooperation and is finished
-- immediately by the write op.
-- ============================================================================

BEGIN;

ALTER TABLE operator_jobs
    ADD COLUMN cancel_requested_at TIMESTAMPTZ,
    ADD COLUMN cancel_requested_by TEXT;

COMMENT ON COLUMN operator_jobs.cancel_requested_at IS
    'Set by the cancel_job write op. The runner checks it at each drain-pass '
    'boundary and stops there; the row then ends ''failed'' with the reason '
    'in error. Never an interrupt — a pass boundary is the only point where '
    'the counters and the queues agree.';

-- Find the jobs a runner still has to cooperate with, cheaply, on every pass.
CREATE INDEX ix_jobs_cancel_pending ON operator_jobs (tenant_id, id)
    WHERE cancel_requested_at IS NOT NULL AND status = 'running';

COMMIT;
