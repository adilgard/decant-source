-- ============================================================================
-- KNOWLEDGE HUB — BASELINE SCHEMA (v0.2)
-- Postgres 16+ with extensions: pgvector, Apache AGE
-- ----------------------------------------------------------------------------
-- Design decisions this encodes (see progress doc, 2026-07-21):
--   * Facts are first-class typed rows, promoted OUT of chunks. Chunks are
--     evidence only. Determinism lives here, not in the vector index.
--   * Ontology is SWAPPABLE: entity_type and predicate are DATA (strings),
--     not table names. Every fact carries ontology_version. Enforcement lives
--     in the extraction/validation layer while the ontology is in flux.
--   * Three chunk tiers: documents (superparent) -> chunks(level='parent')
--     -> chunks(level='child'). Children are the embed/cite unit.
--   * Two tracks (system-of-record, form header fields) produce facts with NO
--     chunk. Provenance therefore points at a chunk OR a raw-doc location.
--   * Temporal columns reserved now (valid_from/valid_to); no validity logic yet.
--   * Permissions by REFERENCE: security_label on rows + separate grant table.
--   * Embedding dim = 1024 (bge-m3 default). Change vector(1024) if you switch.
-- ----------------------------------------------------------------------------
-- v0.2 additions (entity resolution):
--   * entities gains an embedding (so mentions can be blocked against entities).
--   * entity_mentions: raw pre-resolution observations; the ER input layer.
--   * match_candidates: scored pairs + banded decision + deterministic evidence.
--   * entity_merges: reversible undo log (snapshots the absorbed entity).
--   * resolution_policy: the threshold matrix, as editable data per entity_type.
--   * facts gains serialized_lines + oversized: soft alarm for "fact is secretly
--     a document." No hard size cap; one absurdly-high CHECK as a bug tripwire.
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS vector;   -- pgvector
CREATE EXTENSION IF NOT EXISTS age;      -- Apache AGE (graph)
CREATE EXTENSION IF NOT EXISTS pg_trgm;  -- fuzzy alias matching for entity resolution

-- ============================================================================
-- 0. ONTOLOGY VERSIONING  (swap target)
-- ============================================================================
CREATE TABLE ontology_versions (
    version         TEXT PRIMARY KEY,          -- e.g. 'baseline-0.1', 'real-1.0'
    effective_from  TIMESTAMPTZ NOT NULL DEFAULT now(),
    definition      JSONB NOT NULL,            -- allowed entity_types + predicates + attribute schemas
    notes           TEXT
);

-- Seed the swappable baseline vocabulary.
INSERT INTO ontology_versions (version, definition, notes) VALUES (
  'baseline-0.1',
  '{
     "entity_types": ["Person","Organization","Document","Process","System",
                      "Project","Event","Asset","Communication","Location"],
     "predicates":   ["authored_by","mentions","part_of","supersedes","reports_to",
                      "owns","governs","participated_in","references","derived_from"]
   }'::jsonb,
  'Placeholder vocabulary. Shaped like the real ontology; type names are throwaway.'
);

-- ============================================================================
-- 1. PERMISSIONS  (reference, not copy)
-- ============================================================================
CREATE TABLE security_labels (
    id           BIGSERIAL PRIMARY KEY,
    label        TEXT UNIQUE NOT NULL,         -- e.g. 'public','finance_restricted'
    description  TEXT
);

CREATE TABLE label_role_grants (
    label_id  BIGINT NOT NULL REFERENCES security_labels(id),
    role      TEXT   NOT NULL,                 -- role name from your IdP/RBAC
    PRIMARY KEY (label_id, role)
);
-- Change access for everything under a label by editing THIS table. No row rewrites.

INSERT INTO security_labels (label, description)
VALUES ('public','Default, visible to all authenticated roles');

-- ============================================================================
-- 2. RAW LANDING  (immutable; idempotency by content hash)
-- ============================================================================
CREATE TABLE raw_documents (
    id                BIGSERIAL PRIMARY KEY,
    source_system     TEXT NOT NULL,           -- 'gmail','slack','sharepoint','crm', ...
    source_native_id  TEXT,                    -- id in the source system, if any
    mime_type         TEXT,
    content_hash      TEXT NOT NULL,           -- sha256 of raw bytes -> idempotency
    raw_uri           TEXT NOT NULL,           -- object-store path to original bytes
    source_acl        JSONB,                   -- captured source permissions
    security_label_id BIGINT REFERENCES security_labels(id),
    captured_at       TIMESTAMPTZ,             -- when it existed/occurred at source
    ingested_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    status            TEXT NOT NULL DEFAULT 'landed'  -- landed|parsed|extracted|error
);
CREATE UNIQUE INDEX ux_raw_content_hash ON raw_documents(content_hash);
CREATE INDEX ix_raw_source ON raw_documents(source_system, source_native_id);

-- ============================================================================
-- 3. DOCUMENTS  (SUPERPARENT tier)
-- ============================================================================
CREATE TYPE doc_type AS ENUM ('prose','sop','communication','tabular','sor','form');

CREATE TABLE documents (
    id                BIGSERIAL PRIMARY KEY,
    raw_document_id   BIGINT NOT NULL REFERENCES raw_documents(id),
    doc_type          doc_type NOT NULL,
    title             TEXT,
    author            TEXT,
    source_timestamp  TIMESTAMPTZ,
    thread_id         TEXT,                    -- comms: groups messages into a thread
    security_label_id BIGINT REFERENCES security_labels(id),
    metadata          JSONB,                   -- type-specific (sheet name, form type, ...)
    ingested_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_documents_type ON documents(doc_type);
CREATE INDEX ix_documents_thread ON documents(thread_id);

-- ============================================================================
-- 4. CHUNKS  (PARENT + CHILD tiers; self-referencing)
--    - level='parent' : extraction unit + LLM context; parent_chunk_id IS NULL
--    - level='child'  : embed/cite/retrieve unit; parent_chunk_id -> parent row
--    - tabular/sor/form-header facts may have NO chunk at all.
-- ============================================================================
CREATE TYPE chunk_level AS ENUM ('parent','child');

CREATE TABLE chunks (
    id                BIGSERIAL PRIMARY KEY,
    document_id       BIGINT NOT NULL REFERENCES documents(id),   -- superparent link
    parent_chunk_id   BIGINT REFERENCES chunks(id),               -- child->parent; NULL for parents
    level             chunk_level NOT NULL,
    seq               INT NOT NULL,            -- ordering within its parent/document
    content           TEXT NOT NULL,
    contextual_prefix TEXT,                    -- Anthropic contextual-retrieval blurb (children)
    content_hash      TEXT NOT NULL,
    token_count       INT,

    -- Provenance anchors (char offsets for prose; page/cell for pdf/tabular)
    char_start        INT,
    char_end          INT,
    locator           JSONB,                   -- {"page":3} or {"sheet":"Q2","row":42}

    -- Communication-specific
    speaker           TEXT,
    event_time        TIMESTAMPTZ,

    -- Vector + keyword (bge-m3: dense 1024 + sparse handles BM25 in one model,
    -- but we also keep a native tsvector so keyword search works without the model)
    embedding         vector(1024),            -- NULL when not embedded (e.g. rows)
    embedding_model   TEXT,
    embedding_version TEXT,
    content_tsv       tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,

    ingested_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ux_chunk_hash ON chunks(content_hash);
CREATE INDEX ix_chunks_document ON chunks(document_id);
CREATE INDEX ix_chunks_parent ON chunks(parent_chunk_id);
CREATE INDEX ix_chunks_tsv ON chunks USING GIN(content_tsv);          -- keyword/BM25-ish
CREATE INDEX ix_chunks_embedding ON chunks
    USING hnsw (embedding vector_cosine_ops);                         -- ANN vector search

-- ============================================================================
-- 5. ENTITIES  (canonical registry) + ALIASES
-- ============================================================================
CREATE TABLE entities (
    id                BIGSERIAL PRIMARY KEY,
    canonical_name    TEXT NOT NULL,
    entity_type       TEXT NOT NULL,           -- validated against current ontology
    attributes        JSONB NOT NULL DEFAULT '{}'::jsonb,
    ontology_version  TEXT NOT NULL REFERENCES ontology_versions(version),
    security_label_id BIGINT REFERENCES security_labels(id),

    -- Canonical embedding: the vector new mentions are blocked against (v0.2).
    -- On new-entity creation, seed from the triggering mention's context_embedding.
    embedding         vector(1024),
    embedding_model   TEXT,

    valid_from        TIMESTAMPTZ,             -- reserved; no logic yet
    valid_to          TIMESTAMPTZ,             -- NULL = currently true
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_entities_type ON entities(entity_type);
CREATE INDEX ix_entities_name_trgm ON entities USING GIN(canonical_name gin_trgm_ops);
CREATE INDEX ix_entities_embedding ON entities
    USING hnsw (embedding vector_cosine_ops);   -- ANN blocking target for the resolver

-- Every observed surface form maps here -> resolves wikilinks/mentions to one ID.
CREATE TABLE entity_aliases (
    id           BIGSERIAL PRIMARY KEY,
    entity_id    BIGINT NOT NULL REFERENCES entities(id),
    alias        TEXT NOT NULL,
    source       TEXT,                         -- where this surface form was seen
    confidence   REAL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_alias_entity ON entity_aliases(entity_id);
CREATE INDEX ix_alias_trgm ON entity_aliases USING GIN(alias gin_trgm_ops);

-- ============================================================================
-- 6. FACTS  (unified triple store: entity-entity relationships AND attribute facts)
--    subject --predicate--> object_entity   (relationship)
--    subject --predicate--> object_literal  (attribute, e.g. payment_terms='Net-30')
--    THE FACT ENVELOPE = this row's provenance + temporal + security columns.
-- ============================================================================
CREATE TABLE facts (
    id                BIGSERIAL PRIMARY KEY,

    -- The triple
    subject_entity_id BIGINT NOT NULL REFERENCES entities(id),
    predicate         TEXT NOT NULL,           -- validated against current ontology
    object_entity_id  BIGINT REFERENCES entities(id),  -- for entity-entity facts
    object_literal    TEXT,                    -- for attribute facts (one of the two is set)
    attributes        JSONB NOT NULL DEFAULT '{}'::jsonb,

    ontology_version  TEXT NOT NULL REFERENCES ontology_versions(version),

    -- Temporal (reserved; NULL valid_to = currently true)
    valid_from        TIMESTAMPTZ,
    valid_to          TIMESTAMPTZ,

    -- Provenance envelope (chunk OR raw-doc location; chunk is NULL for row/form facts)
    source_document_id BIGINT REFERENCES documents(id),
    source_chunk_id    BIGINT REFERENCES chunks(id),
    char_start         INT,
    char_end           INT,
    locator            JSONB,                  -- {"sheet":"Q2","row":42,"col":"terms"}
    extractor          TEXT NOT NULL,          -- which parser/model produced this
    extractor_version  TEXT NOT NULL,
    confidence         REAL,

    security_label_id  BIGINT REFERENCES security_labels(id),
    ingested_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Fact-size soft alarm (v0.2): app computes serialized_lines at insert and
    -- sets oversized=true above the soft threshold (~60-80 lines). This does NOT
    -- reject -- oversized facts are written intact and queued for review, because
    -- size is a modeling smell ("is this really one assertion?"), not an error.
    serialized_lines   INT,
    oversized          BOOLEAN NOT NULL DEFAULT false,

    CONSTRAINT chk_object_present
      CHECK (object_entity_id IS NOT NULL OR object_literal IS NOT NULL),
    CONSTRAINT chk_provenance_present
      CHECK (source_chunk_id IS NOT NULL OR source_document_id IS NOT NULL),
    -- Bug tripwire ONLY (not size policy): a fact this large means an extractor
    -- dumped a chunk/looped. Set absurdly high so it never touches valid data.
    CONSTRAINT chk_not_pathological
      CHECK (serialized_lines IS NULL OR serialized_lines <= 5000)
);
CREATE INDEX ix_facts_subject ON facts(subject_entity_id);
CREATE INDEX ix_facts_object ON facts(object_entity_id);
CREATE INDEX ix_facts_predicate ON facts(predicate);
CREATE INDEX ix_facts_current ON facts(subject_entity_id) WHERE valid_to IS NULL;
CREATE INDEX ix_facts_source_doc ON facts(source_document_id);
CREATE INDEX ix_facts_oversized ON facts(oversized) WHERE oversized;   -- review queue

-- ============================================================================
-- 7. ENTITY RESOLUTION  (mentions -> candidates -> banded decision -> merges)
-- ----------------------------------------------------------------------------
-- Flow:  extraction emits entity_mentions
--        -> resolver blocks each against entities (exact keys, then ANN)
--        -> writes match_candidates (score + evidence + band)
--        -> resolution_policy bands decide: auto_merge | auto_separate | review
--        -> entity-to-entity merges are logged (reversibly) in entity_merges.
-- Keyed sources (CRM customer_id, etc.) skip mentions and resolve deterministically.
-- ============================================================================

-- 7a. Raw pre-resolution observations. Persisted (not resolved-and-discarded)
--     so re-resolution and merge-reversal are possible.
CREATE TABLE entity_mentions (
    id                 BIGSERIAL PRIMARY KEY,
    surface_text       TEXT NOT NULL,          -- as extracted, e.g. "Acme Corp"
    entity_type        TEXT NOT NULL,          -- as extracted (pre-resolution)
    source_system      TEXT NOT NULL,          -- resolver reads this for identifier strength
    source_document_id BIGINT REFERENCES documents(id),
    source_chunk_id    BIGINT REFERENCES chunks(id),
    char_start         INT,
    char_end           INT,
    locator            JSONB,
    extracted_keys     JSONB NOT NULL DEFAULT '{}'::jsonb, -- {"email":..,"domain":..,"tax_id":..}
    context_embedding  vector(1024),           -- for ANN blocking (reuse bge-m3)
    resolved_entity_id BIGINT REFERENCES entities(id),     -- NULL until resolved
    resolution_status  TEXT NOT NULL DEFAULT 'pending',    -- pending|resolved|review|rejected
    resolver_version   TEXT,
    resolved_at        TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_mentions_status ON entity_mentions(resolution_status);   -- review queue
CREATE INDEX ix_mentions_resolved ON entity_mentions(resolved_entity_id);
CREATE INDEX ix_mentions_type ON entity_mentions(entity_type);
CREATE INDEX ix_mentions_text_trgm ON entity_mentions USING GIN(surface_text gin_trgm_ops);
CREATE INDEX ix_mentions_embedding ON entity_mentions
    USING hnsw (context_embedding vector_cosine_ops);

-- 7b. Scored candidate pairs + the banded decision + the deterministic evidence.
--     left/right are polymorphic (mention or entity), so NOT FK-enforced here;
--     enforce referential integrity in the resolver/app layer.
CREATE TABLE match_candidates (
    id              BIGSERIAL PRIMARY KEY,
    left_type       TEXT NOT NULL,             -- 'mention' | 'entity'
    left_id         BIGINT NOT NULL,
    right_type      TEXT NOT NULL,             -- usually 'entity' (existing canonical)
    right_id        BIGINT NOT NULL,
    match_score     REAL NOT NULL,
    match_method    TEXT NOT NULL,             -- deterministic_key|deterministic_name|probabilistic|embedding|llm
    features        JSONB,                     -- key_overlap, name_sim, cosine, corroboration, FS weights
    band            TEXT,                      -- high | gray | low
    decision        TEXT NOT NULL DEFAULT 'pending', -- auto_merge|auto_separate|review|applied
    decision_reason TEXT,
    reviewed_by     TEXT,
    reviewed_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_candidates_decision ON match_candidates(decision);       -- review queue
CREATE INDEX ix_candidates_left  ON match_candidates(left_type, left_id);
CREATE INDEX ix_candidates_right ON match_candidates(right_type, right_id);

-- 7c. Reversible merge log. Snapshots the absorbed entity so a bad over-merge
--     can be split back apart (recreate from snapshot, re-resolve its mentions).
CREATE TABLE entity_merges (
    id                  BIGSERIAL PRIMARY KEY,
    surviving_entity_id BIGINT NOT NULL REFERENCES entities(id),
    merged_entity_id    BIGINT NOT NULL,       -- absorbed id (row may be gone; not FK-enforced)
    merged_snapshot     JSONB NOT NULL,        -- full copy: name, type, attributes, aliases -> restorable
    triggered_by        BIGINT REFERENCES match_candidates(id),
    method              TEXT,
    score               REAL,
    merged_by           TEXT NOT NULL,         -- 'auto' | reviewer id
    merged_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    reversed_at         TIMESTAMPTZ,
    reversed_by         TEXT
);
CREATE INDEX ix_merges_surviving ON entity_merges(surviving_entity_id);
CREATE INDEX ix_merges_active ON entity_merges(surviving_entity_id) WHERE reversed_at IS NULL;

-- 7d. The threshold policy as editable data, per entity_type. Retune without code.
--     You choose precision_target (acceptable error); calibration sets t_high/t_low.
CREATE TABLE resolution_policy (
    entity_type            TEXT PRIMARY KEY,
    t_high                 REAL NOT NULL,      -- >= this -> auto-merge
    t_low                  REAL NOT NULL,      -- <= this -> auto-separate (confident non-match)
    precision_target       REAL,              -- what t_high was calibrated to hit
    requires_corroboration BOOLEAN NOT NULL DEFAULT false, -- name match needs a graph edge too
    auto_merge_allowed     BOOLEAN NOT NULL DEFAULT true,
    notes                  TEXT,
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Seed with the starter matrix mapped onto the BASELINE ontology types.
-- NUMBERS ARE PLACEHOLDERS until calibrated on labeled pairs. When the real
-- ontology lands (Vendor/Customer/Contract/Employee...), add/replace rows to match.
INSERT INTO resolution_policy
    (entity_type, t_high, t_low, precision_target, requires_corroboration, auto_merge_allowed, notes) VALUES
  ('Organization', 0.95, 0.50, 0.995, true,  true,  'High stakes (vendors/customers live here). Wide review band; name match needs corroboration.'),
  ('Person',       0.93, 0.50, 0.980, true,  true,  'Name collisions common; require a shared-org/project edge to auto-merge on name.'),
  ('Contract',     0.96, 0.55, 0.995, true,  true,  'High stakes. Prefer key/ID match; corroborate name matches.'),
  ('Process',      0.92, 0.45, 0.950, false, true,  'Medium stakes.'),
  ('System',       0.92, 0.45, 0.950, false, true,  'Medium stakes.'),
  ('Project',      0.92, 0.45, 0.950, false, true,  'Medium stakes.'),
  ('Event',        0.92, 0.45, 0.950, false, true,  'Medium stakes.'),
  ('Asset',        0.92, 0.45, 0.950, false, true,  'Medium stakes.'),
  ('Document',     0.98, 0.60, 0.980, false, true,  'Mostly deduped by content_hash upstream; ER is a fallback.'),
  ('Communication',0.98, 0.60, 0.980, false, true,  'Deduped by hash/thread upstream; ER is a fallback.'),
  ('Location',     0.90, 0.35, 0.900, false, true,  'Low stakes, high volume; loose threshold, mostly auto-merge.');

-- ============================================================================
-- 8. GRAPH LAYER (Apache AGE)  -- PROJECTION of entities+facts, not source of truth
-- ----------------------------------------------------------------------------
-- Relational tables above are authoritative. The graph is rebuilt/synced from
-- them for traversal queries. AGE requires labels declared before use.
-- ============================================================================
SELECT create_graph('knowledge_hub');
-- Declare vertex/edge labels up front (AGE requirement; also enforces your ontology):
SELECT create_vlabel('knowledge_hub','Entity');
SELECT create_elabel('knowledge_hub','REL');
-- Sync job (not shown) walks facts where object_entity_id IS NOT NULL and
-- MERGEs (:Entity {id})-[:REL {predicate, fact_id, valid_to}]->(:Entity {id}).

-- ============================================================================
-- 9. HELPER VIEWS
-- ============================================================================
-- 9a. Current facts (permission filtering applied in the query/app layer:
--     join facts.security_label_id -> label_role_grants filtered by caller roles).
CREATE VIEW facts_current AS
SELECT f.*
FROM facts f
WHERE f.valid_to IS NULL;

-- 9b. Unified review queue: everything the pipeline flagged for a human.
CREATE VIEW review_queue AS
SELECT 'mention'::text AS kind, id AS ref_id, entity_type AS context, created_at
  FROM entity_mentions WHERE resolution_status = 'review'
UNION ALL
SELECT 'match'::text,   id, match_method,      created_at
  FROM match_candidates WHERE decision = 'review'
UNION ALL
SELECT 'oversized_fact'::text, id, predicate,  ingested_at
  FROM facts WHERE oversized;
