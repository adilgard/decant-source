-- ============================================================================
-- MIGRATION 006 — BENCHMARK RECORDING HARNESS (Build Prompt 6)
-- Applies ON TOP of 005. Additive only. Keep models.py in lock-step (same
-- commit): GoldSet, GoldSetItem, PinProfile, BenchmarkRun, BenchmarkRunItem.
-- ----------------------------------------------------------------------------
-- Implements §7 of the APPROVED benchmark methodology
-- (KnowledgeHub_Benchmark_Methodology_v0.1_2026-07-22.md): if a run is not
-- recorded structurally with full provenance, it did not happen.
--   1. GOLD SETS — versioned, immutable ground truth. Editing = a new
--      version row; items are content-hashed and the set's content_hash is
--      the sha256 over ordered item hashes. status draft -> active gates the
--      runner (spot-check/review happens on draft).
--   2. PIN PROFILES — "pinned" as a recorded fact, not a claim: a named
--      JSONB snapshot of every axis's setting. Axis runs vary their own axis
--      and must name the profile that freezes the rest. Seeded with the
--      methodology §0.3 incumbents.
--   3. BENCHMARK RUNS — one row per (config x gold set), written
--      status='running' BEFORE execution (a crash leaves a visible error
--      row, never a phantom run) with full provenance: model digests,
--      package version + source-tree hash, hardware fingerprint, gold-set
--      content hash denormalized for integrity.
--   4. RUN ITEMS — per-gold-item outcomes. Aggregates in benchmark_runs.
--      metrics must be RECOMPUTABLE from these rows (the dry-run proves it);
--      they are also what bootstrap CIs resample.
--   5. LEADERBOARD VIEW — runs are comparable ONLY within the same
--      (gold_set, pin_profile); the view carries both as grouping keys so
--      no consumer can accidentally rank across conditions.
-- ============================================================================

BEGIN;

-- ----------------------------------------------------------------------------
-- 1. GOLD SETS (versioned, immutable; §6.3/§6.4 of the methodology)
-- ----------------------------------------------------------------------------
CREATE TABLE gold_sets (
    id                 BIGSERIAL PRIMARY KEY,
    tenant_id          TEXT NOT NULL DEFAULT 'default',
    kind               TEXT NOT NULL,      -- retrieval | er | extraction
    version            TEXT NOT NULL,      -- e.g. 'retrieval-prose-0.1'
    status             TEXT NOT NULL DEFAULT 'draft',  -- draft|active|retired
    generator          TEXT NOT NULL,
    generator_version  TEXT NOT NULL,
    item_count         INT NOT NULL DEFAULT 0,
    content_hash       TEXT,               -- sha256 over ordered item hashes
    floors_met         BOOLEAN NOT NULL DEFAULT false,  -- §6.2 statistical floors
    spec               JSONB NOT NULL DEFAULT '{}'::jsonb,  -- track, corpus ids/hash, review notes
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    activated_at       TIMESTAMPTZ,
    activated_by       TEXT,
    CONSTRAINT ux_gold_set_version UNIQUE (tenant_id, kind, version),
    CONSTRAINT chk_gold_kind   CHECK (kind IN ('retrieval', 'er', 'extraction')),
    CONSTRAINT chk_gold_status CHECK (status IN ('draft', 'active', 'retired'))
);
CREATE INDEX ix_gold_sets_tenant ON gold_sets(tenant_id, kind, status);

CREATE TABLE gold_set_items (
    id            BIGSERIAL PRIMARY KEY,
    gold_set_id   BIGINT NOT NULL REFERENCES gold_sets(id),
    seq           INT NOT NULL,
    item          JSONB NOT NULL,     -- shape per kind (methodology §6.3)
    item_hash     TEXT NOT NULL,
    CONSTRAINT ux_gold_item_seq UNIQUE (gold_set_id, seq)
);
CREATE INDEX ix_gold_items_set ON gold_set_items(gold_set_id);

-- ----------------------------------------------------------------------------
-- 2. PIN PROFILES — the frozen non-varying axes, as data (§7.3)
-- ----------------------------------------------------------------------------
CREATE TABLE pin_profiles (
    name        TEXT PRIMARY KEY,           -- e.g. 'pins-2026-07-v1'
    profile     JSONB NOT NULL,             -- every axis's pinned setting
    notes       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Seed: the methodology §0.3 incumbents. Digests are pinned by NAME here and
-- resolved+recorded per run; pin an explicit "digest" key to hard-require one.
INSERT INTO pin_profiles (name, profile, notes) VALUES (
  'pins-2026-07-v1',
  '{
     "a_index":     {"index": "pgvector-hnsw", "params": {"m": 16, "ef_construction": 64}},
     "b_er":        {"resolver": "tiered-0.1/splink-4.0.16/adj-qwen3.6"},
     "c_embedder":  {"embedder": "bge-m3", "dim": 1024, "chunk_tokens": 300,
                     "overlap_pct": 15, "contextual_prefix": true, "mode": "dense"},
     "d_extraction":{"model": "qwen3.6", "contract": "p2", "think": false,
                     "temperature": 0, "repair_cap": 1}
   }'::jsonb,
  'Methodology v1.0 incumbents (doc §0.3). New incumbent after a verdict = new profile version.'
);

-- ----------------------------------------------------------------------------
-- 3. BENCHMARK RUNS — the provenance envelope (§7.1)
-- ----------------------------------------------------------------------------
CREATE TABLE benchmark_runs (
    id                BIGSERIAL PRIMARY KEY,
    tenant_id         TEXT NOT NULL DEFAULT 'default',
    axis              TEXT NOT NULL,        -- a_index | b_er | c_embedder | d_extraction
    config            JSONB NOT NULL,       -- EVERY knob of the thing under test
    pin_profile_name  TEXT NOT NULL REFERENCES pin_profiles(name),
    pin_profile       JSONB NOT NULL,       -- denormalized snapshot (profiles may gain versions)
    gold_set_id       BIGINT NOT NULL REFERENCES gold_sets(id),
    gold_set_hash     TEXT,                 -- denormalized for integrity checks
    metrics           JSONB,                -- aggregates + CIs; headline_name/headline_value
    advisory          BOOLEAN NOT NULL DEFAULT false,  -- below floors / backfilled / out-of-scale
    -- Provenance: if it isn't here, the run didn't happen.
    model_digests     JSONB,                -- {model_name: digest} for every model touched
    ontology_version  TEXT REFERENCES ontology_versions(version),  -- axes B/D
    package_version   TEXT NOT NULL,
    code_hash         TEXT NOT NULL,        -- sha256 over sorted package source hashes (no git yet)
    runner_version    TEXT NOT NULL,
    hardware          JSONB,                -- gpu/cpu/ram, ollama/postgres/extension versions
    wall_ms           INT,
    status            TEXT NOT NULL DEFAULT 'running',  -- running | ok | error
    error             TEXT,
    notes             TEXT,
    started_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at       TIMESTAMPTZ,
    CONSTRAINT chk_bench_axis CHECK (axis IN
        ('a_index', 'b_er', 'c_embedder', 'd_extraction')),
    CONSTRAINT chk_bench_status CHECK (status IN ('running', 'ok', 'error'))
);
CREATE INDEX ix_bench_runs_axis ON benchmark_runs(tenant_id, axis, status);
CREATE INDEX ix_bench_runs_gold ON benchmark_runs(gold_set_id);

CREATE TABLE benchmark_run_items (
    id                BIGSERIAL PRIMARY KEY,
    run_id            BIGINT NOT NULL REFERENCES benchmark_runs(id),
    gold_set_item_id  BIGINT NOT NULL REFERENCES gold_set_items(id),
    outcome           JSONB NOT NULL,       -- per-item result; aggregates recompute from these
    CONSTRAINT ux_bench_item UNIQUE (run_id, gold_set_item_id)
);
CREATE INDEX ix_bench_items_run ON benchmark_run_items(run_id);

-- ----------------------------------------------------------------------------
-- 4. LEADERBOARD — comparability keys (gold_set_id, pin_profile_name) ride
--    every row; consumers group by them, never across them.
-- ----------------------------------------------------------------------------
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
       r.wall_ms,
       r.finished_at
FROM benchmark_runs r
JOIN gold_sets g ON g.id = r.gold_set_id
WHERE r.status = 'ok'
ORDER BY r.axis, r.gold_set_id, r.pin_profile_name,
         headline_value DESC NULLS LAST;

COMMIT;
