-- ============================================================================
-- MIGRATION 010 — OPERATOR WRITE PATH (Build Prompt 19)
-- Applies ON TOP of 009. Additive only. Keep models.py in lock-step (same
-- commit): OperatorAudit; DispatchMessage.acknowledged_at/acknowledged_by.
-- ----------------------------------------------------------------------------
-- The operator/admin write API is the write-twin of the read choke point:
-- fixed named write operations, tenant scoped from the resolved principal,
-- deny-by-default roles, and EVERY action audited. This migration adds:
--
--   1. operator_audit — one row per attempted write action (applied /
--      refused / failed), carrying principal · action · target · params ·
--      the reversible-snapshot ref (e.g. 'entity_merges:<id>') · timestamp.
--      Review decisions ALSO land in `labels` (005) — one action, two
--      records: audit answers "who did what when", labels feed the flywheel.
--
--   2. Alert acknowledgement state on the two outbox queues. An "alert" is
--      an existing error condition (a queue item that has failed, a degraded
--      source) — not a new event stream. Acknowledging marks the queue row
--      seen; retrying or completing clears it naturally.
--
--   3. operator_alerts — the UI's alert list as a VIEW over real state:
--      unacknowledged failed queue items + degraded sources. Degraded
--      sources carry no ack state: they clear by being fixed
--      (resume_source / a healthy run), not by being dismissed.
-- ============================================================================

BEGIN;

-- ----------------------------------------------------------------------------
-- 1. AUDIT TRAIL — every operator action, including refused ones.
--    target is 'kind:id' ('match_candidate:12', 'source:fs-hr', ...).
--    snapshot_ref points at the domain's reversibility record when one
--    exists ('entity_merges:45') — the audit row never duplicates it.
-- ----------------------------------------------------------------------------
CREATE TABLE operator_audit (
    id            BIGSERIAL PRIMARY KEY,
    tenant_id     TEXT NOT NULL DEFAULT 'default',
    principal_id  TEXT NOT NULL,
    roles         TEXT[] NOT NULL DEFAULT '{}',
    action        TEXT NOT NULL,
    target        TEXT,
    params        JSONB NOT NULL DEFAULT '{}'::jsonb,  -- caller params; never credentials
    outcome       TEXT NOT NULL,
    error         TEXT,
    snapshot_ref  TEXT,
    result        JSONB,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_operator_audit_outcome CHECK (outcome IN
        ('applied', 'refused', 'failed'))
);
CREATE INDEX ix_operator_audit_tenant ON operator_audit(tenant_id, created_at);
CREATE INDEX ix_operator_audit_action ON operator_audit(tenant_id, action);

-- ----------------------------------------------------------------------------
-- 2. ALERT ACKNOWLEDGEMENT on the outboxes (002 dispatch, 004 extraction).
-- ----------------------------------------------------------------------------
ALTER TABLE dispatch_queue   ADD COLUMN acknowledged_at TIMESTAMPTZ;
ALTER TABLE dispatch_queue   ADD COLUMN acknowledged_by TEXT;
ALTER TABLE extraction_queue ADD COLUMN acknowledged_at TIMESTAMPTZ;
ALTER TABLE extraction_queue ADD COLUMN acknowledged_by TEXT;

-- ----------------------------------------------------------------------------
-- 3. THE ALERT LIST — a view over real state, nothing duplicated.
--    A queue item alerts while it carries an error, isn't done, and nobody
--    acknowledged it; retry (status -> queued + ack cleared) and completion
--    (done) both clear it. A degraded source alerts until it is healthy.
-- ----------------------------------------------------------------------------
CREATE VIEW operator_alerts AS
SELECT 'dispatch'::text AS kind, id AS ref_id, tenant_id,
       last_error AS detail, created_at
  FROM dispatch_queue
 WHERE last_error IS NOT NULL AND status <> 'done'
   AND acknowledged_at IS NULL
UNION ALL
SELECT 'extraction'::text, id, tenant_id, last_error, created_at
  FROM extraction_queue
 WHERE last_error IS NOT NULL AND status <> 'done'
   AND acknowledged_at IS NULL
UNION ALL
SELECT 'source'::text, id, tenant_id, status_reason, updated_at
  FROM source_registry
 WHERE status = 'degraded';

COMMIT;
