-- ============================================================================
-- MIGRATION 004 — EXTRACTION (Build Prompt 4)
-- Applies ON TOP of 003. Additive only. Keep models.py in lock-step (same
-- commit): PendingFact.grounding/needs_review/serialized_lines/oversized,
-- QuarantinedExtraction, ExtractionRun.
-- ----------------------------------------------------------------------------
-- Four things arrive with the extraction stage:
--   1. QUARANTINE — the constraint posture is "constrain structure, validate
--      meaning" (§8.2c/f/g): the JSON *shape* is grammar-enforced at decode
--      time, but the predicate/entity-type vocabulary is deliberately NOT —
--      off-ontology attempts must SURVIVE to a reviewable place, because they
--      are the signal that grows the ontology. quarantined_extractions holds
--      them (with the raw model output), and review_queue gains a
--      'quarantine' feeder the same way 003 added the 'document' feeder.
--   2. GROUNDING FLAGS — deterministic span verification can fail on
--      legitimate paraphrase, so failure lowers confidence and flags for
--      review instead of rejecting. pending_facts gains the grounding result
--      (observability: the benchmark's signal) + needs_review, and
--      review_queue gains a 'pending_fact' feeder.
--   3. EXTRACTION RUNS — per-unit observability (token counts, wall-clock,
--      counts, model version) AND the idempotency ledger: a completed run is
--      keyed by (tenant, unit content_hash, extractor, extractor_version,
--      ontology_version), so re-extracting the same parent replays as a
--      no-op instead of staging duplicates.
--   4. EXTRACTION QUEUE — the capture->processing outbox pattern repeated one
--      stage downstream (processing->extraction). Same shape, same
--      at-least-once lease semantics, consumed by ExtractionService.
-- Plus one DATA change (not schema): the baseline-0.1 ontology definition
-- gains per-type/per-predicate examples and a predicate alias map. Prompt
-- examples and surface-variant normalization are ontology DATA, so a new
-- ontology row swaps them with no code change.
-- ============================================================================

BEGIN;

-- ----------------------------------------------------------------------------
-- 1. PENDING FACTS — grounding result + review flag + size soft-alarm
--    (envelope parity with facts; computed at stage time)
-- ----------------------------------------------------------------------------
ALTER TABLE pending_facts ADD COLUMN grounding TEXT;
    -- pass | span_missing | components_missing | construction (SoR facts)
ALTER TABLE pending_facts ADD COLUMN needs_review BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE pending_facts ADD COLUMN serialized_lines INT;
ALTER TABLE pending_facts ADD COLUMN oversized BOOLEAN NOT NULL DEFAULT false;
CREATE INDEX ix_pending_review ON pending_facts(tenant_id) WHERE needs_review;

-- ----------------------------------------------------------------------------
-- 2. QUARANTINE — off-ontology / won't-validate extractions, WITH the raw
--    model output (the flywheel's labels; never silently dropped)
-- ----------------------------------------------------------------------------
CREATE TABLE quarantined_extractions (
    id                 BIGSERIAL PRIMARY KEY,
    tenant_id          TEXT NOT NULL DEFAULT 'default',
    document_id        BIGINT REFERENCES documents(id),
    source_chunk_id    BIGINT REFERENCES chunks(id),
    reason             TEXT NOT NULL,      -- unbound_entity_type | unbound_predicate | validation_failure
    detail             TEXT,               -- e.g. the offending predicate/type
    raw_output         JSONB,              -- the model's actual output for this item
    extractor          TEXT NOT NULL,
    extractor_version  TEXT NOT NULL,
    ontology_version   TEXT NOT NULL REFERENCES ontology_versions(version),
    status             TEXT NOT NULL DEFAULT 'open',   -- open | resolved | dismissed
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_quarantine_reason CHECK (reason IN
        ('unbound_entity_type', 'unbound_predicate', 'validation_failure')),
    CONSTRAINT chk_quarantine_status CHECK (status IN
        ('open', 'resolved', 'dismissed'))
);
CREATE INDEX ix_quarantine_open ON quarantined_extractions(tenant_id)
    WHERE status = 'open';
CREATE INDEX ix_quarantine_reason ON quarantined_extractions(tenant_id, reason);

-- ----------------------------------------------------------------------------
-- 3. EXTRACTION RUNS — per-unit observability + the idempotency ledger.
--    unit_hash = the parent chunk's content_hash (prose) or the raw
--    document's content_hash (structured): re-extracting the same content
--    with the same extractor+model+ontology is a replay, not a re-stage.
-- ----------------------------------------------------------------------------
CREATE TABLE extraction_runs (
    id                 BIGSERIAL PRIMARY KEY,
    tenant_id          TEXT NOT NULL DEFAULT 'default',
    document_id        BIGINT NOT NULL REFERENCES documents(id),
    source_chunk_id    BIGINT REFERENCES chunks(id),   -- NULL for structured docs
    unit_hash          TEXT NOT NULL,
    strategy           TEXT NOT NULL,                  -- llm_joint | structured_map
    extractor          TEXT NOT NULL,
    extractor_version  TEXT NOT NULL,                  -- model digest for LLM runs
    ontology_version   TEXT NOT NULL REFERENCES ontology_versions(version),
    prompt_tokens      INT,
    output_tokens      INT,
    wall_ms            INT,
    facts_staged       INT NOT NULL DEFAULT 0,
    mentions_staged    INT NOT NULL DEFAULT 0,
    quarantined        INT NOT NULL DEFAULT 0,
    grounding_flags    INT NOT NULL DEFAULT 0,
    repairs            INT NOT NULL DEFAULT 0,         -- capped at 1 by policy
    status             TEXT NOT NULL DEFAULT 'ok',     -- ok | error
    error              TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_run_status CHECK (status IN ('ok', 'error'))
);
-- Idempotency: one COMPLETED run per (tenant, content, extractor, model,
-- ontology). Errored runs don't count — a retry after a failure is welcome.
CREATE UNIQUE INDEX ux_extraction_unit ON extraction_runs
    (tenant_id, unit_hash, extractor, extractor_version, ontology_version)
    WHERE status = 'ok';
CREATE INDEX ix_runs_document ON extraction_runs(tenant_id, document_id);

-- ----------------------------------------------------------------------------
-- 4. EXTRACTION QUEUE — processing -> extraction outbox (same contract as
--    dispatch_queue: reference-only payload, idempotent enqueue, at-least-once
--    delivery via lease expiry)
-- ----------------------------------------------------------------------------
CREATE TABLE extraction_queue (
    id               BIGSERIAL PRIMARY KEY,
    tenant_id        TEXT NOT NULL DEFAULT 'default',
    raw_document_id  BIGINT NOT NULL REFERENCES raw_documents(id),
    status           TEXT NOT NULL DEFAULT 'queued',  -- queued|inflight|done|error
    attempts         INT NOT NULL DEFAULT 0,
    available_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    claimed_at       TIMESTAMPTZ,
    acked_at         TIMESTAMPTZ,
    last_error       TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ux_extraction_queue_doc UNIQUE (tenant_id, raw_document_id),
    CONSTRAINT chk_extraction_queue_status CHECK (status IN
        ('queued', 'inflight', 'done', 'error'))
);
CREATE INDEX ix_extraction_queue_claim
    ON extraction_queue(tenant_id, available_at)
    WHERE status IN ('queued', 'inflight');

-- ----------------------------------------------------------------------------
-- 5. REVIEW QUEUE — rebuild with the two new feeders (a view's SELECT list is
--    frozen at CREATE time — same reason 001 and 003 rebuilt it)
-- ----------------------------------------------------------------------------
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
  FROM documents WHERE review_status = 'review'
UNION ALL
SELECT 'quarantine'::text, id, reason,         created_at, tenant_id
  FROM quarantined_extractions WHERE status = 'open'
UNION ALL
SELECT 'pending_fact'::text, id, grounding,    created_at, tenant_id
  FROM pending_facts WHERE needs_review;

-- ----------------------------------------------------------------------------
-- 6. ONTOLOGY DATA — per-type/per-predicate examples (included in extraction
--    prompts) and unambiguous surface-variant aliases (normalized in
--    deterministic code; swap=true flips subject/object). DATA on the
--    ontology row: a new ontology version replaces all of this, no code
--    change. Predicates NOT in this alias map stay unknown -> quarantine.
-- ----------------------------------------------------------------------------
UPDATE ontology_versions
SET definition = definition || '{
  "examples": {
    "entity_types": {
      "Person":        ["Dana Reyes", "the shift supervisor"],
      "Organization":  ["QA Team", "Diversified Botanics"],
      "Document":      ["SOP-014", "the cleaning log"],
      "Process":       ["batch release review", "equipment cleaning"],
      "System":        ["the ERP system", "LIMS"],
      "Project":       ["the Building A retrofit"],
      "Event":         ["the quarterly audit"],
      "Asset":         ["mixer M-3", "the labeler"],
      "Communication": ["the recall notice email"],
      "Location":      ["Building A", "the wash station"]
    },
    "predicates": {
      "authored_by":      ["SOP-014 authored_by Dana Reyes (Document authored_by Person)"],
      "mentions":         ["the audit report mentions mixer M-3"],
      "part_of":          ["the labeler part_of Building A; a step part_of a process"],
      "supersedes":       ["SOP-015 supersedes SOP-014 (newer Document supersedes older)"],
      "reports_to":       ["a line operator reports_to the shift supervisor"],
      "owns":             ["the QA Team owns SOP-014 (owner is the SUBJECT, owned thing is the OBJECT)"],
      "governs":          ["SOP-014 governs the batch release process"],
      "participated_in":  ["Dana Reyes participated_in the quarterly audit"],
      "references":       ["SOP-014 references the cleaning log"],
      "derived_from":     ["the summary sheet derived_from the batch record"]
    }
  },
  "predicate_aliases": {
    "owned_by":       {"predicate": "owns", "swap": true},
    "owner_of":       "owns",
    "authored":       {"predicate": "authored_by", "swap": true},
    "wrote":          {"predicate": "authored_by", "swap": true},
    "written_by":     "authored_by",
    "belongs_to":     "part_of",
    "member_of":      "part_of",
    "refers_to":      "references",
    "mentioned_in":   {"predicate": "mentions", "swap": true},
    "governed_by":    {"predicate": "governs", "swap": true},
    "replaces":       "supersedes",
    "superseded_by":  {"predicate": "supersedes", "swap": true},
    "replaced_by":    {"predicate": "supersedes", "swap": true},
    "derives_from":   "derived_from",
    "participates_in":"participated_in"
  }
}'::jsonb
WHERE version = 'baseline-0.1';

COMMIT;
