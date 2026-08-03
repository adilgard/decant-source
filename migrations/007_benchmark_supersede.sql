-- ============================================================================
-- MIGRATION 007 — BENCHMARK RUN SUPERSESSION (Build Prompt 7)
-- Applies ON TOP of 006. Additive only. Keep models.py in lock-step (same
-- commit): BenchmarkRun.superseded_by_run_id / superseded_note.
-- ----------------------------------------------------------------------------
-- Axis-C round 1 fed raw text to every embedder; only the incumbent (bge-m3)
-- is prefix-free by design, so the round was structurally tilted and its
-- verdict is PROVISIONAL. Runs are never deleted or re-scored — their config
-- JSONB honestly records what executed — but a superseded run must not be
-- readable off the leaderboard as a current decision. Hence:
--   * superseded_by_run_id — the replacement run (same model+track, corrected
--     config), where one exists;
--   * superseded_note — why, for runs with no direct replacement (e.g. a
--     model not fielded in the rematch).
-- The leaderboard view gains a `superseded` flag; report labels append it.
-- ============================================================================

BEGIN;

ALTER TABLE benchmark_runs
    ADD COLUMN superseded_by_run_id BIGINT REFERENCES benchmark_runs(id);
ALTER TABLE benchmark_runs
    ADD COLUMN superseded_note TEXT;

-- Rebuild the view (a view's SELECT list is frozen at CREATE time — same
-- reason 001/003/004 rebuilt review_queue).
DROP VIEW benchmark_leaderboard;
CREATE VIEW benchmark_leaderboard AS
SELECT r.tenant_id,
       r.axis,
       r.gold_set_id,
       g.kind             AS gold_set_kind,
       g.version          AS gold_set_version,
       r.pin_profile_name,
       r.id               AS run_id,
       coalesce(r.config->>'label',
                r.config->>'embedder',
                r.config->>'index',
                r.config->>'scorer',
                r.config->>'model')          AS config_label,
       r.metrics->>'headline_name'           AS headline_name,
       (r.metrics->>'headline_value')::real  AS headline_value,
       r.advisory,
       (r.superseded_by_run_id IS NOT NULL
        OR r.superseded_note IS NOT NULL)    AS superseded,
       r.superseded_by_run_id,
       r.wall_ms,
       r.finished_at
FROM benchmark_runs r
JOIN gold_sets g ON g.id = r.gold_set_id
WHERE r.status = 'ok'
ORDER BY r.axis, r.gold_set_id, r.pin_profile_name,
         headline_value DESC NULLS LAST;

COMMIT;
