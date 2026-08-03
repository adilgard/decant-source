-- ============================================================================
-- MIGRATION 002 — CAPTURE PATH ADDENDA (Build Prompt 2)
-- Applies ON TOP of 001. Additive only. Keep models.py in lock-step (same
-- commit): RawDocument.native_metadata, SourceRegistryEntry, DispatchMessage.
-- ----------------------------------------------------------------------------
-- Adds the three pieces the capture path needs that no baseline table carries:
--   1. NATIVE METADATA — the spec says capture source metadata generously at
--      acquisition (it's irreplaceable). source_acl only holds permissions;
--      everything else (absolute path, size, timestamps, owner, adapter info)
--      needs a home on the raw landing row.
--   2. SOURCE REGISTRY — one row per (tenant, source): connector config
--      (NEVER secrets — those live in OpenBao under
--      tenants/<tenant>/sources/<source_ref>), health status so a
--      missing/denied credential degrades ONE source without touching the
--      tenant, and the acquisition checkpoints that make large pulls
--      resumable (backfill_cursor mid-pull, cursor across incremental runs).
--   3. DISPATCH QUEUE — the capture->processing handoff. Rows carry a
--      REFERENCE (raw_document_id), never payload. Enqueue is idempotent per
--      landed doc; delivery is at-least-once via lease expiry (a claim that
--      is never acked becomes claimable again when available_at passes).
-- ============================================================================

BEGIN;

-- ----------------------------------------------------------------------------
-- 1. NATIVE METADATA (generous, adapter-shaped; source_acl stays ACL-only)
-- ----------------------------------------------------------------------------
ALTER TABLE raw_documents ADD COLUMN native_metadata JSONB;

-- ----------------------------------------------------------------------------
-- 2. SOURCE REGISTRY (per-tenant connector registration + health + checkpoints)
-- ----------------------------------------------------------------------------
CREATE TABLE source_registry (
    id               BIGSERIAL PRIMARY KEY,
    tenant_id        TEXT NOT NULL DEFAULT 'default',
    source_ref       TEXT NOT NULL,              -- registry key; also the OpenBao path leaf
    source_system    TEXT NOT NULL,              -- adapter kind: 'filesystem','sftp','gdrive',...
    config           JSONB NOT NULL DEFAULT '{}'::jsonb,  -- adapter config; NEVER credentials

    status           TEXT NOT NULL DEFAULT 'active',      -- active|degraded|disabled
    status_reason    TEXT,                       -- why degraded/disabled (no secret values)

    -- Acquisition checkpoints (opaque adapter cursor tokens).
    cursor           TEXT,                       -- incremental high-water mark (completed runs)
    backfill_cursor  TEXT,                       -- mid-backfill resume point; NULL when done
    backfill_done    BOOLEAN NOT NULL DEFAULT false,

    last_run_at      TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT ux_source_registry UNIQUE (tenant_id, source_ref),
    CONSTRAINT chk_source_status CHECK (status IN ('active','degraded','disabled'))
);
CREATE INDEX ix_source_registry_status ON source_registry(tenant_id, status);

-- ----------------------------------------------------------------------------
-- 3. DISPATCH QUEUE (reference-only outbox; at-least-once delivery)
-- ----------------------------------------------------------------------------
CREATE TABLE dispatch_queue (
    id               BIGSERIAL PRIMARY KEY,
    tenant_id        TEXT NOT NULL DEFAULT 'default',
    raw_document_id  BIGINT NOT NULL REFERENCES raw_documents(id),

    status           TEXT NOT NULL DEFAULT 'queued',   -- queued|inflight|done|error
    attempts         INT NOT NULL DEFAULT 0,
    available_at     TIMESTAMPTZ NOT NULL DEFAULT now(),  -- claimable when <= now()
    claimed_at       TIMESTAMPTZ,
    acked_at         TIMESTAMPTZ,
    last_error       TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- One queue record per landed doc: enqueue is idempotent (a re-landing
    -- no-op or a crash-resume re-dispatch never duplicates the record);
    -- at-least-once comes from lease redelivery, not duplicate rows.
    CONSTRAINT ux_dispatch_doc UNIQUE (tenant_id, raw_document_id),
    CONSTRAINT chk_dispatch_status CHECK (status IN ('queued','inflight','done','error'))
);
CREATE INDEX ix_dispatch_claimable ON dispatch_queue(tenant_id, status, available_at);

COMMIT;
