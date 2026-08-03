-- Runs ONCE, automatically, the first time the Postgres data volume is created.
-- Turns on the three extensions inside the knowledge_hub database so we can verify
-- them before applying the full baseline schema (which is a separate, deliberate step).
CREATE EXTENSION IF NOT EXISTS vector;   -- pgvector  (embeddings + ANN search)
CREATE EXTENSION IF NOT EXISTS age;      -- Apache AGE (graph)
CREATE EXTENSION IF NOT EXISTS pg_trgm;  -- fuzzy text matching (entity resolution)

-- AGE stores its types/functions in the ag_catalog schema. Putting it on the
-- search_path means you don't have to prefix everything with ag_catalog.
-- IMPORTANT: ag_catalog goes LAST. Unqualified CREATE TABLE writes to the FIRST
-- schema on the path — with ag_catalog first, the whole baseline schema lands in
-- ag_catalog instead of public (learned the hard way on the practice run).
ALTER DATABASE knowledge_hub SET search_path = "$user", public, ag_catalog;
