-- ============================================================================
-- MIGRATION 011 — ONTOLOGY REGISTRY: EXPLICIT ACTIVE SELECTION (d.s Stage 1)
-- Applies ON TOP of 010. Additive only. Keep factstore_pg.py in lock-step
-- (same commit): get_ontology_definition resolves THIS table first;
-- insert_ontology_version / list_ontology_versions / set_active_ontology.
-- ----------------------------------------------------------------------------
-- Before this migration, "current ontology" meant "newest effective_from row"
-- (factstore_pg.get_ontology_definition) — which made INSERTING a version the
-- same act as ACTIVATING it, and left the ingest worker pinning a hard-coded
-- version string on the side (deploy_launch run_ingest). Two resolution rules
-- at once is the split-brain the operator features close.
--
-- After this migration there is ONE rule: the operator's explicit selection,
-- held in ontology_active. Importing a version is inert; selecting it is a
-- separate, audited operator action. The selection applies to FUTURE
-- extraction only — facts keep the ontology_version that actually produced
-- them (true provenance, never rewritten).
--
--   ontology_active — a single-row pointer ("which version is active NOW").
--     Single row is STRUCTURAL: the primary key is a boolean constrained to
--     TRUE, so a second row is uninsertable — no trigger, no app discipline
--     required. History lives in operator_audit (every select_ontology call
--     is one audit row); this table answers only the present tense.
-- ============================================================================

BEGIN;

CREATE TABLE ontology_active (
    one           BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (one),
    version       TEXT NOT NULL REFERENCES ontology_versions(version),
    activated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    activated_by  TEXT                    -- operator principal; NULL = seeded
);

-- Seed with what "current" resolved to under the old rule (newest
-- effective_from), so the migration changes the MECHANISM without changing
-- the ANSWER on any existing deployment.
INSERT INTO ontology_active (version)
SELECT version FROM ontology_versions
ORDER BY effective_from DESC LIMIT 1;

COMMIT;
