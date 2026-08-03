-- ============================================================================
-- MIGRATION 005 — RESOLUTION + THE FLYWHEEL LABEL STORE (Build Prompt 5)
-- Applies ON TOP of 004. Additive only. Keep models.py in lock-step (same
-- commit): Label, ResolutionDecision (+ MatchCandidate / EntityMerge /
-- ResolutionPolicy models mirroring the v0.2 baseline tables Stage D now
-- exercises).
-- ----------------------------------------------------------------------------
-- Two things arrive with the resolution stage:
--   1. LABELS — the flywheel store (§3.4). Every resolution decision that is
--      GROUND TRUTH (not model opinion) lands here as it happens: Tier-0
--      deterministic key matches (free positives), human review decisions,
--      and merge reversals (the hard negatives — the richest source).
--      "Build a gold set" later becomes a query over this table; retrofitting
--      would lose the early decisions, so it exists from the first sweep.
--   2. RESOLUTION DECISIONS — per-mention observability, the extraction_runs
--      principle repeated one stage downstream: which tier fired, the score,
--      the band, the decision, and the deterministic evidence (key_overlap /
--      name_sim / cosine / corroboration in `features`). This IS the
--      ER-benchmark signal (Axis B). match_candidates (v0.2 baseline) stays
--      the pair-level log; this table records the per-mention outcome —
--      including "no candidates at all", which has no pair row.
-- No new queue: resolution runs as a re-runnable batch sweep over
-- entity_mentions.resolution_status='pending' — the status column IS the
-- queue, and ix_mentions_tenant (001) already serves the claim query.
-- ============================================================================

BEGIN;

-- ----------------------------------------------------------------------------
-- 1. LABELS — the flywheel (§3.4). payload is label_type-specific:
--    er_match / er_nonmatch: {"left": {"type": "mention"|"entity", "id": n},
--                             "right": {"type": "entity", "id": n}, ...evidence}
--    retrieval_relevance / correction: reserved for the retrieval stage.
--    `authority` is the trust weight of the SOURCE (deterministic 1.0,
--    human_review ~0.9, reversal ~0.95, agent_feedback lower); `confidence`
--    is the labeler's own confidence in THIS item, when it has one.
-- ----------------------------------------------------------------------------
CREATE TABLE labels (
    id                BIGSERIAL PRIMARY KEY,
    tenant_id         TEXT NOT NULL DEFAULT 'default',
    label_type        TEXT NOT NULL,
    payload           JSONB NOT NULL,
    source            TEXT NOT NULL,
    authority         REAL NOT NULL DEFAULT 1.0,
    confidence        REAL,
    ontology_version  TEXT NOT NULL REFERENCES ontology_versions(version),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_label_type CHECK (label_type IN
        ('er_match', 'er_nonmatch', 'retrieval_relevance', 'correction')),
    CONSTRAINT chk_label_source CHECK (source IN
        ('human_review', 'reversal', 'agent_feedback', 'deterministic',
         'explicit'))
);
CREATE INDEX ix_labels_type   ON labels(tenant_id, label_type);
CREATE INDEX ix_labels_source ON labels(tenant_id, source);

-- ----------------------------------------------------------------------------
-- 2. RESOLUTION DECISIONS — one row per resolver pass over one mention.
--    entity_id is deliberately NOT FK-enforced: the entity a mention resolved
--    to can later be absorbed by a merge and its row deleted; this history
--    row must survive that (same stance as match_candidates' polymorphic
--    left/right ids).
-- ----------------------------------------------------------------------------
CREATE TABLE resolution_decisions (
    id                  BIGSERIAL PRIMARY KEY,
    tenant_id           TEXT NOT NULL DEFAULT 'default',
    mention_id          BIGINT NOT NULL REFERENCES entity_mentions(id),
    tier                TEXT NOT NULL,   -- t0 | t1 | t1b | none (no candidates)
    method              TEXT NOT NULL,   -- deterministic_key|probabilistic|embedding|llm|none
    score               REAL,
    band                TEXT,            -- high | gray | low (NULL for t0/none)
    decision            TEXT NOT NULL,   -- resolved | new_entity | review
    entity_id           BIGINT,          -- resolved/new target (see note above)
    match_candidate_id  BIGINT REFERENCES match_candidates(id),
    features            JSONB,           -- key_overlap/name_sim/cosine/corroboration/FS weights
    resolver_version    TEXT NOT NULL,
    wall_ms             INT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_decision_tier CHECK (tier IN ('t0', 't1', 't1b', 'none')),
    CONSTRAINT chk_decision_band CHECK (band IS NULL OR band IN
        ('high', 'gray', 'low')),
    CONSTRAINT chk_decision_decision CHECK (decision IN
        ('resolved', 'new_entity', 'review'))
);
CREATE INDEX ix_decisions_mention ON resolution_decisions(tenant_id, mention_id);
CREATE INDEX ix_decisions_tier    ON resolution_decisions(tenant_id, tier);

COMMIT;
