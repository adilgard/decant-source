"""PostgresFactStore — psycopg (v3) implementation of FactStore.

Backend: Postgres 16 + pgvector (ANN), pg_trgm (fuzzy alias), Apache AGE
(graph projection). Schema: knowledge_hub_baseline_schema.sql v0.2 +
migrations/001_persistence_addenda.sql.

Tenancy: row-level today. All SQL goes through connections handed out by
`_conn(tenant_id)`, and every statement filters/stamps tenant_id. To move to
schema- or DB-per-tenant, reroute `_conn` (return a per-tenant DSN/connection)
— no call site changes.

Vectors travel as pgvector text literals ('[1,2,...]') cast with ::vector, so
no extra client dependency is needed. AGE cypher() cannot take bind parameters
for its query body, so graph statements are built as escaped literals inside a
dollar-quoted block and executed without params.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any, Iterator, Mapping, Optional, Sequence

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from knowledge_hub.config import settings
from knowledge_hub.interfaces import AnnCandidate, FactStore, StagedPending
from knowledge_hub.models import (
    Chunk,
    Document,
    Entity,
    EntityAlias,
    EntityMention,
    EntityMerge,
    ExtractionRun,
    Fact,
    Label,
    MatchCandidate,
    PendingFact,
    QuarantinedExtraction,
    RawDocument,
    ResolutionDecision,
    ResolutionPolicy,
)

GRAPH_NAME = "knowledge_hub"
# Soft size alarm (schema v0.2): facts serializing past this many lines are
# written intact but flagged oversized -> review_queue. Not a rejection.
OVERSIZED_SOFT_LINES = 70


def vector_literal(v: Sequence[float]) -> str:
    """pgvector text form; pair with a ::vector cast in SQL."""
    return "[" + ",".join(format(x, "g") for x in v) + "]"


def parse_vector(v: Any) -> Optional[list[float]]:
    """pgvector comes back as its text form when no client codec is registered."""
    if v is None or isinstance(v, list):
        return v
    return json.loads(v)


def _cy_str(s: str) -> str:
    """Escape a Python string as a cypher single-quoted literal."""
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _jsonb(d: Optional[dict]) -> Optional[Jsonb]:
    return Jsonb(d) if d is not None else None


class PostgresFactStore(FactStore):
    def __init__(self, dsn: Optional[str] = None, graph_name: str = GRAPH_NAME):
        self._dsn = dsn or settings.postgres_dsn
        self._graph = graph_name
        self._conns: dict[str, psycopg.Connection] = {}

    # ------------------------------------------------------------------ conns
    def _dsn_for(self, tenant_id: str) -> str:
        # Row-level tenancy: one DSN for all tenants. The DB-per-tenant swap
        # happens HERE (map tenant_id -> its DSN); nothing above this changes.
        return self._dsn

    def _conn(self, tenant_id: str) -> psycopg.Connection:
        # Keyed by DSN, not tenant: with row-level tenancy every tenant maps
        # to ONE DSN and shares one connection (a per-tenant key grows a
        # connection per tenant and exhausts max_connections); when the
        # DB-per-tenant swap lands in _dsn_for, distinct DSNs naturally get
        # distinct connections — nothing here changes.
        dsn = self._dsn_for(tenant_id)
        conn = self._conns.get(dsn)
        if conn is None or conn.closed:
            # autocommit=True + explicit conn.transaction() blocks: bare reads
            # don't linger idle-in-transaction, and every write below runs in
            # an explicit BEGIN/COMMIT.
            conn = psycopg.connect(dsn, row_factory=dict_row, autocommit=True)
            # ag_catalog LAST — otherwise objects land in the wrong schema
            # (gotcha from the pilot practice run, see NOTES.md).
            conn.execute('SET search_path = public, ag_catalog, "$user";')
            self._conns[dsn] = conn
        return conn

    @contextmanager
    def transaction(self, tenant_id: str) -> Iterator[psycopg.Connection]:
        """Explicit transaction on the tenant's connection."""
        conn = self._conn(tenant_id)
        with conn.transaction():
            yield conn

    def close(self) -> None:
        for conn in self._conns.values():
            if not conn.closed:
                conn.close()
        self._conns.clear()

    # ---------------------------------------------------------------- entities
    def upsert_entity(self, entity: Entity) -> int:
        with self.transaction(entity.tenant_id) as conn:
            emb = vector_literal(entity.embedding) if entity.embedding is not None else None
            if entity.id is None:
                row = conn.execute(
                    """
                    INSERT INTO entities
                        (tenant_id, canonical_name, entity_type, attributes,
                         ontology_version, security_label_id, embedding,
                         embedding_model, valid_from, valid_to)
                    VALUES (%s, %s, %s, %s, %s, %s, %s::vector, %s, %s, %s)
                    RETURNING id
                    """,
                    (entity.tenant_id, entity.canonical_name, entity.entity_type,
                     Jsonb(entity.attributes), entity.ontology_version,
                     entity.security_label_id, emb, entity.embedding_model,
                     entity.valid_from, entity.valid_to),
                ).fetchone()
                entity.id = row["id"]
            else:
                updated = conn.execute(
                    """
                    UPDATE entities SET
                        canonical_name = %s, entity_type = %s, attributes = %s,
                        ontology_version = %s, security_label_id = %s,
                        embedding = %s::vector, embedding_model = %s,
                        valid_from = %s, valid_to = %s
                    WHERE id = %s AND tenant_id = %s
                    """,
                    (entity.canonical_name, entity.entity_type, Jsonb(entity.attributes),
                     entity.ontology_version, entity.security_label_id,
                     emb, entity.embedding_model, entity.valid_from, entity.valid_to,
                     entity.id, entity.tenant_id),
                ).rowcount
                if updated == 0:
                    raise LookupError(
                        f"entity id={entity.id} not found for tenant {entity.tenant_id!r}")
            for alias in entity.aliases:
                conn.execute(
                    """
                    INSERT INTO entity_aliases (tenant_id, entity_id, alias, source, confidence)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (entity_id, alias) DO NOTHING
                    """,
                    (entity.tenant_id, entity.id, alias.alias, alias.source, alias.confidence),
                )
        return entity.id

    def ann_candidates(
        self,
        tenant_id: str,
        embedding: Sequence[float],
        entity_type: str,
        k: int = 20,
    ) -> list[AnnCandidate]:
        emb = vector_literal(embedding)
        rows = self._conn(tenant_id).execute(
            """
            SELECT id, canonical_name, entity_type, attributes,
                   1 - (embedding <=> %s::vector) AS similarity
            FROM entities
            WHERE tenant_id = %s AND entity_type = %s
              AND embedding IS NOT NULL AND valid_to IS NULL
            ORDER BY embedding <=> %s::vector
            LIMIT %s
            """,
            (emb, tenant_id, entity_type, emb, k),
        ).fetchall()
        return [
            AnnCandidate(
                entity_id=r["id"], canonical_name=r["canonical_name"],
                entity_type=r["entity_type"], attributes=r["attributes"] or {},
                similarity=r["similarity"],
            )
            for r in rows
        ]

    # ------------------------------------------------------------------- facts
    def write_facts(self, facts: Sequence[Fact]) -> list[int]:
        ids: list[int] = []
        for fact in facts:
            with self.transaction(fact.tenant_id) as conn:
                serialized_lines = fact.model_dump_json(indent=2).count("\n") + 1
                oversized = serialized_lines > OVERSIZED_SOFT_LINES
                row = conn.execute(
                    """
                    INSERT INTO facts
                        (tenant_id, subject_entity_id, predicate, object_entity_id,
                         object_literal, attributes, ontology_version,
                         valid_from, valid_to,
                         source_document_id, source_chunk_id, char_start, char_end,
                         locator, extractor, extractor_version, confidence,
                         security_label_id, serialized_lines, oversized)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (fact.tenant_id, fact.subject_entity_id, fact.predicate,
                     fact.object_entity_id, fact.object_literal, Jsonb(fact.attributes),
                     fact.ontology_version, fact.valid_from, fact.valid_to,
                     fact.source_document_id, fact.source_chunk_id,
                     fact.char_start, fact.char_end, _jsonb(fact.locator),
                     fact.extractor, fact.extractor_version, fact.confidence,
                     fact.security_label_id, serialized_lines, oversized),
                ).fetchone()
                fact.id = row["id"]
                fact.serialized_lines = serialized_lines
                fact.oversized = oversized
                if fact.object_entity_id is not None:
                    self._project_fact(conn, fact)
            ids.append(fact.id)
        return ids

    def _project_fact(self, conn: psycopg.Connection, fact: Fact) -> None:
        """MERGE the fact's endpoints + edge into the AGE graph — RETIRED
        (Build Prompt 9): a no-op unless settings.project_to_age, which is
        off by default and stays off. Nothing reads the graph (serve path
        and resolver are SQL over the authoritative facts tables), and the
        temporal spine (BP7/BP8) made existing edges KNOWN-STALE — edge
        valid_to is written here at fact-write time and never updated by
        retraction/supersession. Kept as frozen reference; re-enabling is a
        deliberate project (AGE_DORMANT.md: rebuild-from-facts + wire
        temporal updates + solve cypher parameterization), never a toggle."""
        if not settings.project_to_age:
            return
        t = _cy_str(fact.tenant_id)
        # All edge properties go in the MERGE map: AGE 1.5.0 silently drops a
        # SET applied to a MERGE-created edge (verified on this stack). NOTE
        # (BP7): fact rows are no longer insert-only — retraction mutates
        # valid_to relationally and does NOT touch this edge map; that
        # staleness is part of why the projection is retired.
        valid_to = (
            f", valid_to: {_cy_str(fact.valid_to.isoformat())}"
            if fact.valid_to is not None else ""
        )
        body = (
            f"MERGE (s:Entity {{id: {fact.subject_entity_id}, tenant_id: {t}}}) "
            f"MERGE (o:Entity {{id: {fact.object_entity_id}, tenant_id: {t}}}) "
            f"MERGE (s)-[r:REL {{fact_id: {fact.id}, "
            f"predicate: {_cy_str(fact.predicate)}, tenant_id: {t}{valid_to}}}]->(o)"
        )
        conn.execute(self._cypher_sql(body, ncols=1))

    def _cypher_sql(self, body: str, ncols: int) -> str:
        if "$kh$" in body:
            raise ValueError("cypher body may not contain the $kh$ quote tag")
        cols = ", ".join(f"c{i} ag_catalog.agtype" for i in range(ncols))
        return (
            f"SELECT * FROM ag_catalog.cypher('{self._graph}', $kh$ {body} $kh$) "
            f"AS ({cols});"
        )

    def run_cypher(self, tenant_id: str, body: str, ncols: int = 1) -> list[tuple]:
        """Escape hatch for graph reads — tests/diagnostics ONLY. No live
        path reads AGE (the serve path and resolver are SQL over facts —
        S2 finding; the projection itself is RETIRED, Build Prompt 9, and
        its edges are known-stale). The caller is responsible for
        tenant-scoping the query (filter on tenant_id properties)."""
        conn = self._conn(tenant_id)
        with conn.transaction():
            rows = conn.execute(self._cypher_sql(body, ncols)).fetchall()
        return [tuple(r.values()) for r in rows]

    # ------------------------------------------------------- extraction handoff
    def stage_pending(
        self,
        mentions: Mapping[str, EntityMention],
        facts: Sequence[PendingFact],
    ) -> StagedPending:
        if not mentions and not facts:
            return StagedPending(mention_ids={}, pending_fact_ids=[])
        tenants = {m.tenant_id for m in mentions.values()} | {f.tenant_id for f in facts}
        if len(tenants) != 1:
            raise ValueError(f"stage_pending is single-tenant per call, got {tenants}")
        tenant_id = tenants.pop()

        mention_ids: dict[str, int] = {}
        fact_ids: list[int] = []
        with self.transaction(tenant_id) as conn:
            for key, m in mentions.items():
                emb = vector_literal(m.context_embedding) if m.context_embedding is not None else None
                row = conn.execute(
                    """
                    INSERT INTO entity_mentions
                        (tenant_id, surface_text, entity_type, source_system,
                         source_document_id, source_chunk_id, char_start, char_end,
                         locator, extracted_keys, context_embedding,
                         resolved_entity_id, resolution_status, resolver_version)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector, %s, %s, %s)
                    RETURNING id
                    """,
                    (m.tenant_id, m.surface_text, m.entity_type, m.source_system,
                     m.source_document_id, m.source_chunk_id, m.char_start, m.char_end,
                     _jsonb(m.locator), Jsonb(m.extracted_keys), emb,
                     m.resolved_entity_id, m.resolution_status, m.resolver_version),
                ).fetchone()
                m.id = row["id"]
                mention_ids[key] = m.id

            for f in facts:
                subject_ref = self._rewrite_key(f.subject_ref, mention_ids)
                object_ref = (
                    self._rewrite_key(f.object_ref, mention_ids)
                    if f.object_ref is not None else None
                )
                # Size soft-alarm at staging (same rule as write_facts): the
                # envelope carries it from extraction onward, not just after
                # promotion.
                f.serialized_lines = f.model_dump_json(indent=2).count("\n") + 1
                f.oversized = f.serialized_lines > OVERSIZED_SOFT_LINES
                row = conn.execute(
                    """
                    INSERT INTO pending_facts
                        (tenant_id, subject_ref, predicate, object_ref, object_literal,
                         attributes, ontology_version, valid_from, valid_to,
                         source_document_id, source_chunk_id, char_start, char_end,
                         locator, extractor, extractor_version, confidence,
                         security_label_id, resolution_status,
                         grounding, needs_review, serialized_lines, oversized)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s, 'pending',
                            %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (f.tenant_id, subject_ref, f.predicate, object_ref, f.object_literal,
                     Jsonb(f.attributes), f.ontology_version, f.valid_from, f.valid_to,
                     f.source_document_id, f.source_chunk_id, f.char_start, f.char_end,
                     _jsonb(f.locator), f.extractor, f.extractor_version, f.confidence,
                     f.security_label_id,
                     f.grounding, f.needs_review, f.serialized_lines, f.oversized),
                ).fetchone()
                f.id = row["id"]
                f.subject_ref, f.object_ref = subject_ref, object_ref
                fact_ids.append(f.id)
        return StagedPending(mention_ids=mention_ids, pending_fact_ids=fact_ids)

    # ------------------------------------------------- extraction bookkeeping
    def insert_quarantine(self, item: QuarantinedExtraction) -> int:
        """Persist one off-ontology/won't-validate extraction. Born 'open',
        which is what the review_queue 'quarantine' feeder filters on."""
        with self.transaction(item.tenant_id) as conn:
            row = conn.execute(
                """
                INSERT INTO quarantined_extractions
                    (tenant_id, document_id, source_chunk_id, reason, detail,
                     raw_output, extractor, extractor_version, ontology_version,
                     status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (item.tenant_id, item.document_id, item.source_chunk_id,
                 item.reason, item.detail, _jsonb(item.raw_output),
                 item.extractor, item.extractor_version, item.ontology_version,
                 item.status),
            ).fetchone()
            item.id = row["id"]
        return item.id

    def insert_extraction_run(self, run: ExtractionRun) -> int:
        """Record one extraction run (observability + idempotency ledger)."""
        with self.transaction(run.tenant_id) as conn:
            row = conn.execute(
                """
                INSERT INTO extraction_runs
                    (tenant_id, document_id, source_chunk_id, unit_hash,
                     strategy, extractor, extractor_version, ontology_version,
                     prompt_tokens, output_tokens, wall_ms,
                     facts_staged, mentions_staged, quarantined,
                     grounding_flags, repairs, status, error)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (run.tenant_id, run.document_id, run.source_chunk_id,
                 run.unit_hash, run.strategy, run.extractor,
                 run.extractor_version, run.ontology_version,
                 run.prompt_tokens, run.output_tokens, run.wall_ms,
                 run.facts_staged, run.mentions_staged, run.quarantined,
                 run.grounding_flags, run.repairs, run.status, run.error),
            ).fetchone()
            run.id = row["id"]
        return run.id

    def find_extraction_run(self, tenant_id: str, unit_hash: str,
                            extractor: str, extractor_version: str,
                            ontology_version: str) -> Optional[ExtractionRun]:
        """The idempotency probe: a completed (status='ok') run for this exact
        (content, extractor, model, ontology) means the unit is already
        extracted — replay, don't re-stage."""
        row = self._conn(tenant_id).execute(
            """
            SELECT * FROM extraction_runs
            WHERE tenant_id = %s AND unit_hash = %s AND extractor = %s
              AND extractor_version = %s AND ontology_version = %s
              AND status = 'ok'
            """,
            (tenant_id, unit_hash, extractor, extractor_version,
             ontology_version),
        ).fetchone()
        return ExtractionRun(**row) if row else None

    def get_ontology_definition(self, tenant_id: str,
                                version: Optional[str] = None
                                ) -> tuple[str, dict[str, Any]]:
        """The current (or a specific) ontology row: (version, definition).

        'Current' = the operator's explicit selection (ontology_active,
        migration 011) — THE single source of truth every unpinned binding,
        label stamp, and entity stamp resolves. The old newest-effective_from
        rule survives only as a fallback for a pointer row that does not
        exist yet (a registry emptied before 011 seeded it), so insertion is
        INERT and activation is a separate, audited act."""
        conn = self._conn(tenant_id)
        if version is None:
            row = conn.execute(
                "SELECT v.version, v.definition FROM ontology_active a"
                " JOIN ontology_versions v ON v.version = a.version"
            ).fetchone()
            if row is None:  # unseeded pointer: legacy newest-wins fallback
                row = conn.execute(
                    "SELECT version, definition FROM ontology_versions"
                    " ORDER BY effective_from DESC LIMIT 1").fetchone()
        else:
            row = conn.execute(
                "SELECT version, definition FROM ontology_versions"
                " WHERE version = %s", (version,)).fetchone()
        if row is None:
            raise LookupError(f"ontology version {version!r} not found")
        return row["version"], row["definition"]

    def insert_ontology_version(self, tenant_id: str, version: str,
                                definition: dict[str, Any],
                                notes: Optional[str] = None) -> str:
        """Load one VALIDATED ontology set (the ontology_registry module is
        the gate; this method only persists). Inert: importing never touches
        the active selection. Idempotent on identical content ('already_
        imported'); the same version with DIFFERENT content is a hard error
        — versions are immutable, publish a new version string instead.
        Returns 'created' or 'already_imported'."""
        with self.transaction(tenant_id) as conn:
            row = conn.execute(
                "SELECT definition FROM ontology_versions WHERE version = %s",
                (version,)).fetchone()
            if row is not None:
                if row["definition"] == definition:
                    return "already_imported"
                raise ValueError(
                    f"ontology version {version!r} already exists with "
                    f"different content — versions are immutable; import "
                    f"under a new version string")
            conn.execute(
                "INSERT INTO ontology_versions (version, definition, notes)"
                " VALUES (%s, %s, %s)",
                (version, Jsonb(definition), notes))
        return "created"

    def list_ontology_versions(self, tenant_id: str) -> list[dict[str, Any]]:
        """Every loaded ontology set, newest first, with the active one
        flagged — the operator console's listing."""
        conn = self._conn(tenant_id)
        active_row = conn.execute(
            "SELECT version FROM ontology_active").fetchone()
        active = active_row["version"] if active_row else None
        rows = conn.execute(
            "SELECT version, effective_from, definition, notes"
            " FROM ontology_versions"
            " ORDER BY effective_from DESC, version").fetchall()
        return [{
            "version": r["version"],
            "effective_from": r["effective_from"],
            "entity_types": len(r["definition"].get("entity_types", [])),
            "predicates": len(r["definition"].get("predicates", [])),
            "notes": r["notes"],
            "active": r["version"] == active,
        } for r in rows]

    def set_active_ontology(self, tenant_id: str, version: str,
                            activated_by: Optional[str] = None) -> None:
        """Point the single-row selection at `version` (which must already
        be imported). Applies to FUTURE extraction only — nothing here
        touches facts, and nothing ever rewrites a fact's ontology_version
        (that column is true provenance). History is the operator audit
        trail; this row answers only the present tense."""
        with self.transaction(tenant_id) as conn:
            row = conn.execute(
                "SELECT 1 FROM ontology_versions WHERE version = %s",
                (version,)).fetchone()
            if row is None:
                raise LookupError(
                    f"ontology version {version!r} not found — import it "
                    f"before selecting it")
            conn.execute(
                """
                INSERT INTO ontology_active (one, version, activated_by)
                VALUES (TRUE, %s, %s)
                ON CONFLICT (one) DO UPDATE
                    SET version = EXCLUDED.version,
                        activated_at = now(),
                        activated_by = EXCLUDED.activated_by
                """,
                (version, activated_by))

    @staticmethod
    def _rewrite_key(ref: str, mention_ids: Mapping[str, int]) -> str:
        if ref.startswith(("mention:", "entity:")):
            return ref
        if ref not in mention_ids:
            raise KeyError(
                f"fact ref {ref!r} is neither 'mention:<id>'/'entity:<id>' nor a "
                f"mention key in this batch ({sorted(mention_ids)})")
        return f"mention:{mention_ids[ref]}"

    # ------------------------------------------------- resolution bookkeeping
    # Implementation-level helpers for Stage D (like insert_quarantine /
    # insert_extraction_run for Stage C): the ResolutionService composes them
    # inside one outer transaction() — nested transaction() calls on the same
    # tenant connection become savepoints, so a whole apply/merge/reversal
    # commits or rolls back as a unit.
    def insert_match_candidate(self, mc: MatchCandidate) -> int:
        with self.transaction(mc.tenant_id) as conn:
            row = conn.execute(
                """
                INSERT INTO match_candidates
                    (tenant_id, left_type, left_id, right_type, right_id,
                     match_score, match_method, features, band, decision,
                     decision_reason, reviewed_by, reviewed_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (mc.tenant_id, mc.left_type, mc.left_id, mc.right_type,
                 mc.right_id, mc.match_score, mc.match_method,
                 _jsonb(mc.features), mc.band, mc.decision,
                 mc.decision_reason, mc.reviewed_by, mc.reviewed_at),
            ).fetchone()
            mc.id = row["id"]
        return mc.id

    def insert_entity_merge(self, merge: EntityMerge) -> int:
        with self.transaction(merge.tenant_id) as conn:
            row = conn.execute(
                """
                INSERT INTO entity_merges
                    (tenant_id, surviving_entity_id, merged_entity_id,
                     merged_snapshot, triggered_by, method, score, merged_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (merge.tenant_id, merge.surviving_entity_id,
                 merge.merged_entity_id, Jsonb(merge.merged_snapshot),
                 merge.triggered_by, merge.method, merge.score,
                 merge.merged_by),
            ).fetchone()
            merge.id = row["id"]
        return merge.id

    def insert_label(self, label: Label) -> int:
        with self.transaction(label.tenant_id) as conn:
            row = conn.execute(
                """
                INSERT INTO labels
                    (tenant_id, label_type, payload, source, authority,
                     confidence, ontology_version)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (label.tenant_id, label.label_type, Jsonb(label.payload),
                 label.source, label.authority, label.confidence,
                 label.ontology_version),
            ).fetchone()
            label.id = row["id"]
        return label.id

    def insert_resolution_decision(self, d: ResolutionDecision) -> int:
        with self.transaction(d.tenant_id) as conn:
            row = conn.execute(
                """
                INSERT INTO resolution_decisions
                    (tenant_id, mention_id, tier, method, score, band,
                     decision, entity_id, match_candidate_id, features,
                     resolver_version, wall_ms)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (d.tenant_id, d.mention_id, d.tier, d.method, d.score,
                 d.band, d.decision, d.entity_id, d.match_candidate_id,
                 _jsonb(d.features), d.resolver_version, d.wall_ms),
            ).fetchone()
            d.id = row["id"]
        return d.id

    def get_resolution_policy(self, tenant_id: str,
                              entity_type: str) -> Optional[ResolutionPolicy]:
        """The threshold row for one entity type, read as DATA each time it
        is needed (retuning is an UPDATE, not a deploy). resolution_policy is
        per-database, not per-row-tenant; tenant_id only routes the
        connection (and becomes per-tenant policy for free once tenancy moves
        to DB-per-tenant)."""
        row = self._conn(tenant_id).execute(
            "SELECT * FROM resolution_policy WHERE entity_type = %s",
            (entity_type,),
        ).fetchone()
        return ResolutionPolicy(**row) if row else None

    def project_fact(self, fact: Fact) -> None:
        """Re-project one persisted entity-entity fact into the AGE graph
        (public wrapper over the write_facts projection step; used by merge/
        reversal to rebuild edges after endpoints are repointed)."""
        if fact.id is None or fact.object_entity_id is None:
            raise ValueError("project_fact needs a persisted entity-entity fact")
        with self.transaction(fact.tenant_id) as conn:
            self._project_fact(conn, fact)

    def delete_fact_edge(self, tenant_id: str, fact_id: int) -> None:
        """Remove one fact's REL edge from the graph projection (merge/
        reversal repoint facts, then re-project). RETIRED with the
        projection (Build Prompt 9): no-op unless settings.project_to_age."""
        if not settings.project_to_age:
            return
        t = _cy_str(tenant_id)
        self.run_cypher(
            tenant_id,
            f"MATCH ()-[r:REL {{fact_id: {fact_id}, tenant_id: {t}}}]->() DELETE r")

    def delete_entity_vertex(self, tenant_id: str, entity_id: int) -> None:
        """Remove one Entity vertex (and any remaining edges) from the graph
        projection — the absorbed side of a merge. RETIRED with the
        projection (Build Prompt 9): no-op unless settings.project_to_age."""
        if not settings.project_to_age:
            return
        t = _cy_str(tenant_id)
        self.run_cypher(
            tenant_id,
            f"MATCH (v:Entity {{id: {entity_id}, tenant_id: {t}}}) DETACH DELETE v")

    # ---------------------------------------------------------- document tiers
    def insert_document(self, document: Document) -> int:
        with self.transaction(document.tenant_id) as conn:
            row = conn.execute(
                """
                INSERT INTO documents
                    (tenant_id, raw_document_id, doc_type, title, author,
                     source_timestamp, thread_id, security_label_id, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (document.tenant_id, document.raw_document_id, document.doc_type.value,
                 document.title, document.author, document.source_timestamp,
                 document.thread_id, document.security_label_id, _jsonb(document.metadata)),
            ).fetchone()
            document.id = row["id"]
        return document.id

    def insert_chunks(self, chunks: Sequence[Chunk]) -> list[int]:
        ids: list[int] = []
        for chunk in chunks:
            with self.transaction(chunk.tenant_id) as conn:
                emb = vector_literal(chunk.embedding) if chunk.embedding is not None else None
                row = conn.execute(
                    """
                    INSERT INTO chunks
                        (tenant_id, document_id, parent_chunk_id, level, seq, content,
                         contextual_prefix, content_hash, token_count,
                         char_start, char_end, locator, speaker, event_time,
                         embedding, embedding_model, embedding_version)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s::vector, %s, %s)
                    ON CONFLICT (tenant_id, content_hash) DO NOTHING
                    RETURNING id
                    """,
                    (chunk.tenant_id, chunk.document_id, chunk.parent_chunk_id,
                     chunk.level.value, chunk.seq, chunk.content, chunk.contextual_prefix,
                     chunk.content_hash, chunk.token_count, chunk.char_start,
                     chunk.char_end, _jsonb(chunk.locator), chunk.speaker,
                     chunk.event_time, emb, chunk.embedding_model, chunk.embedding_version),
                ).fetchone()
                if row is None:  # idempotent replay: fetch the existing row's id
                    row = conn.execute(
                        "SELECT id FROM chunks WHERE tenant_id = %s AND content_hash = %s",
                        (chunk.tenant_id, chunk.content_hash),
                    ).fetchone()
                chunk.id = row["id"]
            ids.append(chunk.id)
        return ids

    # ------------------------------------------------------- fetch (round-trip)
    # Implementation-level getters: handy for tests and debugging; not part of
    # the FactStore interface contract.
    def get_raw_document(self, tenant_id: str, raw_id: int) -> Optional[RawDocument]:
        r = self._fetch(tenant_id, "raw_documents", raw_id)
        return RawDocument(**r) if r else None

    def get_document(self, tenant_id: str, doc_id: int) -> Optional[Document]:
        r = self._fetch(tenant_id, "documents", doc_id)
        return Document(**r) if r else None

    def get_chunk(self, tenant_id: str, chunk_id: int) -> Optional[Chunk]:
        r = self._fetch(tenant_id, "chunks", chunk_id)
        if not r:
            return None
        r.pop("content_tsv", None)  # generated column; not part of the model
        r["embedding"] = parse_vector(r["embedding"])
        r.pop("ingested_at", None)
        return Chunk(**r)

    def get_entity(self, tenant_id: str, entity_id: int) -> Optional[Entity]:
        r = self._fetch(tenant_id, "entities", entity_id)
        if not r:
            return None
        r["embedding"] = parse_vector(r["embedding"])
        aliases = self._conn(tenant_id).execute(
            "SELECT id, tenant_id, entity_id, alias, source, confidence"
            " FROM entity_aliases WHERE tenant_id = %s AND entity_id = %s ORDER BY id",
            (tenant_id, entity_id),
        ).fetchall()
        return Entity(**r, aliases=[EntityAlias(**a) for a in aliases])

    def get_fact(self, tenant_id: str, fact_id: int) -> Optional[Fact]:
        r = self._fetch(tenant_id, "facts", fact_id)
        if not r:
            return None
        r.pop("ingested_at", None)
        return Fact(**r)

    def get_mention(self, tenant_id: str, mention_id: int) -> Optional[EntityMention]:
        r = self._fetch(tenant_id, "entity_mentions", mention_id)
        if not r:
            return None
        r.pop("created_at", None)
        r["context_embedding"] = parse_vector(r["context_embedding"])
        return EntityMention(**r)

    def get_pending_fact(self, tenant_id: str, pending_id: int) -> Optional[PendingFact]:
        r = self._fetch(tenant_id, "pending_facts", pending_id)
        if not r:
            return None
        r.pop("created_at", None)
        return PendingFact(**r)

    def get_quarantine(self, tenant_id: str,
                       item_id: int) -> Optional[QuarantinedExtraction]:
        r = self._fetch(tenant_id, "quarantined_extractions", item_id)
        return QuarantinedExtraction(**r) if r else None

    def get_match_candidate(self, tenant_id: str,
                            candidate_id: int) -> Optional[MatchCandidate]:
        r = self._fetch(tenant_id, "match_candidates", candidate_id)
        return MatchCandidate(**r) if r else None

    def get_entity_merge(self, tenant_id: str,
                         merge_id: int) -> Optional[EntityMerge]:
        r = self._fetch(tenant_id, "entity_merges", merge_id)
        return EntityMerge(**r) if r else None

    def get_label(self, tenant_id: str, label_id: int) -> Optional[Label]:
        r = self._fetch(tenant_id, "labels", label_id)
        return Label(**r) if r else None

    def get_resolution_decision(self, tenant_id: str,
                                decision_id: int) -> Optional[ResolutionDecision]:
        r = self._fetch(tenant_id, "resolution_decisions", decision_id)
        return ResolutionDecision(**r) if r else None

    _TABLES = {"raw_documents", "documents", "chunks", "entities", "facts",
               "entity_mentions", "pending_facts", "quarantined_extractions",
               "extraction_runs", "match_candidates", "entity_merges",
               "labels", "resolution_decisions"}

    def _fetch(self, tenant_id: str, table: str, row_id: int) -> Optional[dict]:
        assert table in self._TABLES
        return self._conn(tenant_id).execute(
            f"SELECT * FROM {table} WHERE tenant_id = %s AND id = %s",
            (tenant_id, row_id),
        ).fetchone()
