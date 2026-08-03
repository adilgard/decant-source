-- ============================================================================
-- MIGRATION 001 — PERSISTENCE ADDENDA (Build Prompt 1)
-- Applies ON TOP of knowledge_hub_baseline_schema.sql (v0.2). Additive only —
-- no baseline table is redefined. Keep models.py in lock-step (same commit).
-- ----------------------------------------------------------------------------
-- Reconciles three gaps between the persistence spec and baseline v0.2:
--   1. TENANCY — spec requires every query to filter by tenant_id (row-level
--      for now, swappable to schema/DB-per-tenant later). Baseline had no
--      tenant column. Added to all core tables; idempotency indexes become
--      per-tenant (two tenants may legitimately ingest identical bytes).
--   2. RAW VERSIONING — _next_version needs prior versions by source_system +
--      source_native_id. Baseline had no version column. Added to raw_documents.
--   3. PENDING FACTS — extraction emits facts whose subject/object are still
--      mention-keys. facts.subject_entity_id is NOT NULL FK, so candidates
--      CANNOT live in facts until resolution. pending_facts is the staging
--      table; _rewrite_refs promotes rows into facts once mentions resolve.
-- ============================================================================

BEGIN;

-- ----------------------------------------------------------------------------
-- 1. TENANCY (row-level; default tenant keeps existing rows + local pilot valid)
-- ----------------------------------------------------------------------------
ALTER TABLE raw_documents    ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE documents        ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE chunks           ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE entities         ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE entity_aliases   ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE facts            ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE entity_mentions  ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE match_candidates ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE entity_merges    ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';

-- Idempotency becomes per-tenant: the same bytes arriving for two tenants are
-- two legitimate rows; within a tenant they are still a no-op.
DROP INDEX ux_raw_content_hash;
CREATE UNIQUE INDEX ux_raw_content_hash ON raw_documents(tenant_id, content_hash);
DROP INDEX ux_chunk_hash;
CREATE UNIQUE INDEX ux_chunk_hash ON chunks(tenant_id, content_hash);

-- Hot filter paths for the resolver and retrieval.
CREATE INDEX ix_entities_tenant_type ON entities(tenant_id, entity_type);
CREATE INDEX ix_facts_tenant         ON facts(tenant_id);
CREATE INDEX ix_mentions_tenant      ON entity_mentions(tenant_id, resolution_status);
CREATE INDEX ix_chunks_tenant        ON chunks(tenant_id);
CREATE INDEX ix_documents_tenant     ON documents(tenant_id);

-- ----------------------------------------------------------------------------
-- 2. RAW VERSIONING (same source doc, new bytes -> new row, version + 1)
-- ----------------------------------------------------------------------------
ALTER TABLE raw_documents ADD COLUMN version INT NOT NULL DEFAULT 1;
CREATE INDEX ix_raw_source_version
    ON raw_documents(tenant_id, source_system, source_native_id, version);

-- ----------------------------------------------------------------------------
-- 3. ALIAS IDEMPOTENCY (re-observing a surface form is a no-op, not a dup row)
-- ----------------------------------------------------------------------------
CREATE UNIQUE INDEX ux_alias_entity_alias ON entity_aliases(entity_id, alias);

-- ----------------------------------------------------------------------------
-- 4. PENDING FACTS (extraction handoff staging; pre-resolution refs)
--    subject_ref / object_ref grammar:  'mention:<entity_mentions.id>'
--                                       'entity:<entities.id>'   (keyed sources)
-- ----------------------------------------------------------------------------
CREATE TABLE pending_facts (
    id                 BIGSERIAL PRIMARY KEY,
    tenant_id          TEXT NOT NULL DEFAULT 'default',

    subject_ref        TEXT NOT NULL,
    predicate          TEXT NOT NULL,
    object_ref         TEXT,
    object_literal     TEXT,
    attributes         JSONB NOT NULL DEFAULT '{}'::jsonb,
    ontology_version   TEXT NOT NULL REFERENCES ontology_versions(version),

    valid_from         TIMESTAMPTZ,
    valid_to           TIMESTAMPTZ,

    -- Same provenance envelope as facts.
    source_document_id BIGINT REFERENCES documents(id),
    source_chunk_id    BIGINT REFERENCES chunks(id),
    char_start         INT,
    char_end           INT,
    locator            JSONB,
    extractor          TEXT NOT NULL,
    extractor_version  TEXT NOT NULL,
    confidence         REAL,
    security_label_id  BIGINT REFERENCES security_labels(id),

    resolution_status  TEXT NOT NULL DEFAULT 'pending',  -- pending|promoted|rejected
    promoted_fact_id   BIGINT REFERENCES facts(id),      -- set when promoted
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT chk_pending_object
      CHECK (object_ref IS NOT NULL OR object_literal IS NOT NULL),
    CONSTRAINT chk_pending_provenance
      CHECK (source_chunk_id IS NOT NULL OR source_document_id IS NOT NULL)
);
CREATE INDEX ix_pending_facts_status ON pending_facts(tenant_id, resolution_status);

-- ----------------------------------------------------------------------------
-- 5. RECREATE VIEWS — both were defined before tenant_id existed. A view's
--    SELECT f.* is expanded at CREATE time, so facts_current would silently
--    lack tenant_id, and review_queue consumers couldn't scope to a tenant.
-- ----------------------------------------------------------------------------
DROP VIEW facts_current;
CREATE VIEW facts_current AS
SELECT f.*
FROM facts f
WHERE f.valid_to IS NULL;

DROP VIEW review_queue;
CREATE VIEW review_queue AS
SELECT 'mention'::text AS kind, id AS ref_id, entity_type AS context, created_at, tenant_id
  FROM entity_mentions WHERE resolution_status = 'review'
UNION ALL
SELECT 'match'::text,   id, match_method,      created_at, tenant_id
  FROM match_candidates WHERE decision = 'review'
UNION ALL
SELECT 'oversized_fact'::text, id, predicate,  ingested_at, tenant_id
  FROM facts WHERE oversized;

COMMIT;
