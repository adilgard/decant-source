-- 019_serving_usage.sql — the durable usage sink, and the second half of
-- the §8.8 verification story.
--
-- WHY THIS EXISTS. check_side_doors proves the NEGATIVE: nothing is
-- connected to Postgres that shouldn't be. It cannot prove the POSITIVE —
-- that the consumers which are supposed to read through the serving ops
-- actually do. That answer lives in the usage log, and until now the log
-- was an append-only JSONL file whose records carried no identity: you
-- could see that SOMETHING was served, never who read it. SERVICE_NOTES
-- called this half "unbuilt"; more precisely, the record type could not
-- express the answer (see serving.py EnvelopeUsage).
--
-- So: principal_id + served_at on the record, and a real table under it.
-- "The agents' reads flow through ops" becomes a query.
--
-- LOCK-STEP: models.py gains EnvelopeUsageRow in the same commit
-- (SERVING_NOTES rule about new tables).
--
-- NOT PARTITIONED, and that is a decision rather than an oversight. This
-- table grows with every served envelope, so it is the first table in the
-- schema with an unbounded append rate. Partitioning it now would be
-- guessing at a retention policy nobody has set; the index set below keeps
-- the attribution query fast, and the retention conversation is bookmarked
-- rather than pre-answered wrong.

CREATE TABLE IF NOT EXISTS serving_usage (
    id              BIGSERIAL PRIMARY KEY,
    request_id      TEXT        NOT NULL,
    tenant_id       TEXT        NOT NULL,
    -- WHO. A tenant is not an identity: several principals share one, so
    -- tenant_id alone could never answer "which consumer read this".
    principal_id    TEXT        NOT NULL,
    envelope_kind   TEXT        NOT NULL,
    envelope_key    TEXT        NOT NULL,
    -- The strip-later evidence (Decision 4a/4b): which fields the caller
    -- actually serialized, and which uncertainty states it branched on.
    fields_read     JSONB       NOT NULL DEFAULT '[]'::jsonb,
    states_branched JSONB       NOT NULL DEFAULT '[]'::jsonb,
    served_at       TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT serving_usage_kind_chk
        CHECK (envelope_kind IN ('fact', 'evidence'))
);

-- The attribution query: "did principal X read through the ops surface, and
-- when". Leading tenant_id because every read in this system is
-- tenant-scoped first.
CREATE INDEX IF NOT EXISTS ix_serving_usage_principal
    ON serving_usage (tenant_id, principal_id, served_at DESC);

-- Grouping one request's envelopes back together.
CREATE INDEX IF NOT EXISTS ix_serving_usage_request
    ON serving_usage (request_id);

-- The strip-later sweep reads by time across all principals.
CREATE INDEX IF NOT EXISTS ix_serving_usage_served_at
    ON serving_usage (served_at DESC);
