"""Ingestion pipeline — persistence slice (Build Prompt 1).

This module carries the pipeline's persistence responsibilities: raw landing
(versioned, idempotent), review-queue feeding, and the promotion of staged
pending facts into resolved facts. The extraction / chunking / embedding /
resolution stages (Build Prompts 2+) plug in around these methods; they should
only ever touch storage through `self.store` (a FactStore) or the helpers here.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from psycopg.types.json import Jsonb

from knowledge_hub.factstore_pg import PostgresFactStore
from knowledge_hub.models import Fact, PendingFact, RawDocument

REVIEW_KINDS = ("mention", "match", "oversized_fact", "document",
                "quarantine", "pending_fact")

# retraction_reason vocabulary (migration 009). Retraction reuses the
# reserved temporal axis (§2 #8): setting valid_to IS the supersession
# mechanism, and the reason records which trigger set it so each trigger can
# reverse ONLY its own work. 'source_tombstone' is written by tombstone
# propagation (the doc was DELETED at the source; reversible on revival);
# 'superseded' is written by re-version supersession at promotion time (the
# doc was EDITED — version N's facts retire as version N+1's become current,
# §8.1g); 'ontology_superseded' (d.s Stage 3) is written when the SAME
# document's facts re-promote under a DIFFERENT ontology version (operator
# re-extraction) — the old vocabulary's facts retire as the new one's become
# current, retained and queryable, never deleted. One mechanism, three
# triggers, never a parallel deletion path: all flow through
# _retract_facts_for_documents below, and the per-trigger reason is what
# makes a bad swap reversible (a future reversal op can clear exactly
# 'ontology_superseded' and nothing else, the way revival clears exactly
# 'source_tombstone').
RETRACTED_BY_TOMBSTONE = "source_tombstone"
RETRACTED_BY_REVERSION = "superseded"
RETRACTED_BY_ONTOLOGY = "ontology_superseded"


class Pipeline:
    # The persistence stubs here are SQL-level by design, so the pipeline is
    # typed against the Postgres store; stages above it should stick to the
    # FactStore interface.
    def __init__(self, store: Optional[PostgresFactStore] = None):
        self.store: PostgresFactStore = store or PostgresFactStore()

    # ------------------------------------------------------------ raw landing
    def _next_version(self, tenant_id: str, source_system: str,
                      native_id: Optional[str]) -> int:
        """Next version number for a source document: prior versions are rows
        sharing (tenant, source_system, source_native_id); starts at 1."""
        with self.store.transaction(tenant_id) as conn:
            row = conn.execute(
                """
                SELECT COALESCE(MAX(version), 0) + 1 AS next_version
                FROM raw_documents
                WHERE tenant_id = %s AND source_system = %s
                  AND source_native_id IS NOT DISTINCT FROM %s
                """,
                (tenant_id, source_system, native_id),
            ).fetchone()
        return row["next_version"]

    def _persist_raw(self, raw: RawDocument) -> int:
        """Insert the raw landing row; idempotent by (tenant_id, content_hash):
        re-inserting the same bytes is a no-op returning the existing id."""
        with self.store.transaction(raw.tenant_id) as conn:
            row = conn.execute(
                """
                INSERT INTO raw_documents
                    (tenant_id, source_system, source_native_id, mime_type,
                     content_hash, raw_uri, source_acl, security_label_id,
                     captured_at, status, version, native_metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, content_hash) DO NOTHING
                RETURNING id
                """,
                (raw.tenant_id, raw.source_system, raw.source_native_id,
                 raw.mime_type, raw.content_hash, raw.raw_uri,
                 Jsonb(raw.source_acl) if raw.source_acl is not None else None,
                 raw.security_label_id, raw.captured_at,
                 raw.status, raw.version,
                 Jsonb(raw.native_metadata) if raw.native_metadata is not None else None),
            ).fetchone()
            if row is None:  # same bytes already landed for this tenant
                row = conn.execute(
                    "SELECT id FROM raw_documents"
                    " WHERE tenant_id = %s AND content_hash = %s",
                    (raw.tenant_id, raw.content_hash),
                ).fetchone()
        raw.id = row["id"]
        return raw.id

    def ingest_raw(self, raw: RawDocument) -> int:
        """Convenience: stamp the next version, then land idempotently."""
        raw.version = self._next_version(
            raw.tenant_id, raw.source_system, raw.source_native_id)
        return self._persist_raw(raw)

    def tombstone_raw(self, tenant_id: str, source_system: str,
                      native_id: Optional[str]) -> int:
        """Soft-delete a logical document (§8.1g) AND propagate the
        retraction downstream, in one transaction:

          raw_documents.deleted_at -> documents.valid_to -> facts.valid_to

        all stamped with the SAME deletion timestamp, all tagged
        retraction_reason='source_tombstone' so revival can reverse exactly
        this and nothing else. Nothing is physically deleted anywhere —
        bytes stay in the WORM store (retention/erasure policy owns their
        physical fate) and rows stay for audit; the serve path hides
        non-current items via the choke point's {cur:} temporal predicate.

        Fact retraction is PER PROVENANCE LINK: a fact row's single anchor
        (source_document_id, else its chunk's document — the
        chk_provenance_present pair) ties it to exactly one document, and a
        multi-source assertion exists as sibling rows, one per source. Only
        rows anchored to THIS logical document are retracted; a sibling row
        from a surviving source keeps the assertion served. Retraction is
        EAGER (one UPDATE, no per-item model cost — §8.1g's lazy option
        exists for re-EXTRACTION, which retraction never does).

        Chunks and mentions carry no temporal columns by design: a chunk's
        currency IS its document's (evidence templates gate on the joined
        document), and mentions are audit/replay data — the promotion guard
        in promote_pending is what stops a deleted doc's staged facts from
        promoting. Returns raw rows stamped (0 = never landed).
        """
        deleted_at = datetime.now(tz=timezone.utc)
        with self.store.transaction(tenant_id) as conn:
            raw_rows = conn.execute(
                """
                UPDATE raw_documents SET deleted_at = %s
                WHERE tenant_id = %s AND source_system = %s
                  AND source_native_id IS NOT DISTINCT FROM %s
                  AND deleted_at IS NULL
                RETURNING id
                """,
                (deleted_at, tenant_id, source_system, native_id),
            ).fetchall()
            if not raw_rows:
                return 0
            raw_ids = [r["id"] for r in raw_rows]
            conn.execute(
                """
                UPDATE documents SET valid_to = %s, retraction_reason = %s
                WHERE tenant_id = %s AND raw_document_id = ANY(%s)
                  AND valid_to IS NULL
                """,
                (deleted_at, RETRACTED_BY_TOMBSTONE, tenant_id, raw_ids),
            )
            # Fact retraction anchors on ALL of the logical doc's documents,
            # not just the ones the UPDATE above touched: a supersession
            # diff's SURVIVING facts stay anchored to already-'superseded'
            # prior-version documents, and deleting the doc must take them
            # too (their own valid_to IS NULL guard is inside the primitive).
            doc_rows = conn.execute(
                "SELECT id FROM documents"
                " WHERE tenant_id = %s AND raw_document_id = ANY(%s)",
                (tenant_id, raw_ids),
            ).fetchall()
            doc_ids = [d["id"] for d in doc_rows]
            if doc_ids:
                self._retract_facts_for_documents(
                    conn, tenant_id, doc_ids, valid_to=deleted_at,
                    reason=RETRACTED_BY_TOMBSTONE)
        return len(raw_rows)

    @staticmethod
    def _retract_facts_for_documents(conn, tenant_id: str, doc_ids: list[int],
                                     *, valid_to: datetime, reason: str,
                                     keep_fact_ids: tuple = (),
                                     other_than_ontology_version:
                                     Optional[str] = None) -> int:
        """THE facts.valid_to writer — the BP7 retraction primitive's
        document-scoped core, shared by all three triggers (tombstone
        propagation, re-version supersession, ontology supersession; the
        reason discriminates). Retracts every CURRENT fact anchored to
        `doc_ids` per the provenance-link rule (source_document_id, else
        the chunk's document), except `keep_fact_ids` — the supersession
        diff's survivors, which stay temporally continuous.
        `other_than_ontology_version` narrows the retraction to facts NOT
        carrying that version — the ontology-supersession trigger's guard:
        the newly promoted rows (which ARE current) carry the new version
        and must survive their own cutover. Runs on the caller's connection
        so the caller's transaction owns atomicity. Returns rows retracted."""
        version_guard = ""
        params: list = [valid_to, reason, tenant_id, list(keep_fact_ids),
                        doc_ids, tenant_id, doc_ids]
        if other_than_ontology_version is not None:
            version_guard = " AND ontology_version <> %s"
            params.append(other_than_ontology_version)
        return conn.execute(
            """
            UPDATE facts SET valid_to = %s, retraction_reason = %s
            WHERE tenant_id = %s AND valid_to IS NULL
              AND NOT (id = ANY(%s))
              AND (source_document_id = ANY(%s)
                   OR (source_document_id IS NULL
                       AND source_chunk_id IN (
                           SELECT id FROM chunks
                           WHERE tenant_id = %s
                             AND document_id = ANY(%s))))
            """ + version_guard,
            tuple(params),
        ).rowcount

    def revive_raw(self, tenant_id: str, source_system: str,
                   native_id: Optional[str]) -> int:
        """Reverse tombstone propagation for a logical document that
        reappeared at the source (recycle-bin restore, access re-granted):
        an observed upsert outranks any earlier deletion signal.

        Reversal is scoped by retraction_reason='source_tombstone' — valid_to
        set by ANY other writer (future re-version supersession, manual
        temporal edits) is never cleared, so revival cannot resurrect a
        genuinely superseded fact. A delete->revive round trip leaves the
        served result identical to never-deleted. Returns raw rows revived."""
        with self.store.transaction(tenant_id) as conn:
            raw_rows = conn.execute(
                """
                UPDATE raw_documents SET deleted_at = NULL
                WHERE tenant_id = %s AND source_system = %s
                  AND source_native_id IS NOT DISTINCT FROM %s
                  AND deleted_at IS NOT NULL
                RETURNING id
                """,
                (tenant_id, source_system, native_id),
            ).fetchall()
            if not raw_rows:
                return 0
            raw_ids = [r["id"] for r in raw_rows]
            # Facts key on ALL of the logical doc's document rows, with the
            # reason guard doing the real scoping — never on which documents
            # happened to revive.
            doc_rows = conn.execute(
                "SELECT id FROM documents"
                " WHERE tenant_id = %s AND raw_document_id = ANY(%s)",
                (tenant_id, raw_ids),
            ).fetchall()
            doc_ids = [d["id"] for d in doc_rows]
            conn.execute(
                """
                UPDATE documents SET valid_to = NULL, retraction_reason = NULL
                WHERE tenant_id = %s AND raw_document_id = ANY(%s)
                  AND retraction_reason = %s
                """,
                (tenant_id, raw_ids, RETRACTED_BY_TOMBSTONE),
            )
            if doc_ids:
                conn.execute(
                    """
                    UPDATE facts SET valid_to = NULL, retraction_reason = NULL
                    WHERE tenant_id = %s AND retraction_reason = %s
                      AND (source_document_id = ANY(%s)
                           OR (source_document_id IS NULL
                               AND source_chunk_id IN (
                                   SELECT id FROM chunks
                                   WHERE tenant_id = %s
                                     AND document_id = ANY(%s))))
                    """,
                    (tenant_id, RETRACTED_BY_TOMBSTONE, doc_ids,
                     tenant_id, doc_ids),
                )
        return len(raw_rows)

    # ------------------------------------------------------------ review queue
    def _enqueue_review(self, tenant_id: str, kind: str, ref_id: int,
                        reason: Optional[str] = None) -> None:
        """Flag an item for a human. The review_queue view unions its feeders;
        enqueueing = setting the feeder column the view filters on."""
        if kind not in REVIEW_KINDS:
            raise ValueError(f"kind must be one of {REVIEW_KINDS}, got {kind!r}")
        with self.store.transaction(tenant_id) as conn:
            if kind == "mention":
                updated = conn.execute(
                    "UPDATE entity_mentions SET resolution_status = 'review'"
                    " WHERE tenant_id = %s AND id = %s",
                    (tenant_id, ref_id)).rowcount
            elif kind == "match":
                updated = conn.execute(
                    "UPDATE match_candidates SET decision = 'review',"
                    " decision_reason = COALESCE(%s, decision_reason)"
                    " WHERE tenant_id = %s AND id = %s",
                    (reason, tenant_id, ref_id)).rowcount
            elif kind == "document":  # migration 003: §8.1a track mismatch etc.
                updated = conn.execute(
                    "UPDATE documents SET review_status = 'review',"
                    " review_reason = COALESCE(%s, review_reason)"
                    " WHERE tenant_id = %s AND id = %s",
                    (reason, tenant_id, ref_id)).rowcount
            elif kind == "quarantine":  # migration 004: reopen a quarantined item
                updated = conn.execute(
                    "UPDATE quarantined_extractions SET status = 'open'"
                    " WHERE tenant_id = %s AND id = %s",
                    (tenant_id, ref_id)).rowcount
            elif kind == "pending_fact":  # migration 004: grounding-failed fact
                updated = conn.execute(
                    "UPDATE pending_facts SET needs_review = true"
                    " WHERE tenant_id = %s AND id = %s",
                    (tenant_id, ref_id)).rowcount
            else:  # oversized_fact
                updated = conn.execute(
                    "UPDATE facts SET oversized = true"
                    " WHERE tenant_id = %s AND id = %s",
                    (tenant_id, ref_id)).rowcount
        if updated == 0:
            raise LookupError(f"{kind} id={ref_id} not found for tenant {tenant_id!r}")

    # ------------------------------------------------- pending-fact promotion
    def _rewrite_refs(self, pending: PendingFact) -> Optional[Fact]:
        """Map a pending fact's subject_ref/object_ref to canonical entity ids.

        'entity:<id>' refs pass through; 'mention:<id>' refs read the mention's
        resolved_entity_id. Returns the promotable Fact, or None while any
        referenced mention is still unresolved (leave the row pending)."""
        subject_id = self._ref_to_entity_id(pending.tenant_id, pending.subject_ref)
        if subject_id is None:
            return None
        object_id = None
        if pending.object_ref is not None:
            object_id = self._ref_to_entity_id(pending.tenant_id, pending.object_ref)
            if object_id is None:
                return None
        return Fact(
            tenant_id=pending.tenant_id,
            subject_entity_id=subject_id,
            predicate=pending.predicate,
            object_entity_id=object_id,
            object_literal=pending.object_literal,
            attributes=pending.attributes,
            ontology_version=pending.ontology_version,
            valid_from=pending.valid_from,
            valid_to=pending.valid_to,
            source_document_id=pending.source_document_id,
            source_chunk_id=pending.source_chunk_id,
            char_start=pending.char_start,
            char_end=pending.char_end,
            locator=pending.locator,
            extractor=pending.extractor,
            extractor_version=pending.extractor_version,
            confidence=pending.confidence,
            security_label_id=pending.security_label_id,
        )

    def _ref_to_entity_id(self, tenant_id: str, ref: str) -> Optional[int]:
        kind, _, raw_id = ref.partition(":")
        if kind == "entity":
            return int(raw_id)
        if kind == "mention":
            with self.store.transaction(tenant_id) as conn:
                row = conn.execute(
                    "SELECT resolved_entity_id, resolution_status FROM entity_mentions"
                    " WHERE tenant_id = %s AND id = %s",
                    (tenant_id, int(raw_id))).fetchone()
            if row is None:
                raise LookupError(f"ref {ref!r}: mention not found for tenant {tenant_id!r}")
            if row["resolution_status"] != "resolved" or row["resolved_entity_id"] is None:
                return None
            return row["resolved_entity_id"]
        raise ValueError(f"unparseable ref {ref!r} (want 'mention:<id>' or 'entity:<id>')")

    def promote_pending(self, tenant_id: str) -> list[int]:
        """Promote every fully-resolved pending fact into `facts` (+ graph),
        marking the staging row promoted. Unresolved rows stay pending.

        Retraction guard (migration 009): a pending fact whose anchor
        document is retracted (valid_to set — tombstoned OR superseded) is
        SKIPPED, not mutated — a deleted source's facts aren't promotable
        until revival, and an old version's late-resolving facts are never
        promotable at all once a newer version has cut over.

        Re-version supersession (§8.1g, the BP7 primitive's second trigger)
        happens HERE, because promotion is where a new version's facts
        become current: promotion is grouped per anchor document, and each
        group is ONE transaction — the group's facts become current, the
        anchor's prior-version facts/documents retire ('superseded'), all
        under one shared cutover timestamp, so no query window ever sees
        both versions or neither. The cutover DIFFS rather than wholesale-
        retracts: an assertion the new version still makes keeps its
        EXISTING row (temporally continuous, no spurious valid_to/valid_from
        blink; the staging row's promoted_fact_id records the re-assertion
        for audit); only assertions the new version dropped are retracted,
        and only genuinely new ones insert (valid_from = cutover).

        Returns the promoted fact ids — a reused surviving row's id for
        unchanged assertions, a new row's id otherwise."""
        with self.store.transaction(tenant_id) as conn:
            rows = conn.execute(
                """
                SELECT p.id,
                       COALESCE(p.source_document_id,
                                (SELECT c.document_id FROM chunks c
                                 WHERE c.id = p.source_chunk_id))
                           AS anchor_document_id
                FROM pending_facts p
                WHERE p.tenant_id = %s AND p.resolution_status = 'pending'
                  AND NOT EXISTS (
                      SELECT 1 FROM documents dd
                      WHERE dd.tenant_id = p.tenant_id
                        AND dd.valid_to IS NOT NULL
                        AND dd.id = COALESCE(
                            p.source_document_id,
                            (SELECT c.document_id FROM chunks c
                             WHERE c.id = p.source_chunk_id)))
                ORDER BY anchor_document_id NULLS LAST, p.id
                """,
                (tenant_id,)).fetchall()

        groups: dict[Optional[int], list[int]] = {}
        for row in rows:
            groups.setdefault(row["anchor_document_id"], []).append(row["id"])

        promoted: list[int] = []
        for anchor_id, pending_ids in groups.items():
            promoted.extend(self._promote_document_group(
                tenant_id, anchor_id, pending_ids))
        return promoted

    def _promote_document_group(self, tenant_id: str,
                                anchor_document_id: Optional[int],
                                pending_ids: list[int]) -> list[int]:
        """One document's atomic cutover: promote its resolvable pending
        facts AND supersede its prior versions in a single transaction with
        a single timestamp. Nested store calls (write_facts,
        get_pending_fact) become savepoints on the same connection, so the
        outer commit is the one moment the served world changes — and an
        exception anywhere rolls the WHOLE group back (nothing partial,
        at-least-once redelivery re-runs it)."""
        promoted: list[int] = []
        with self.store.transaction(tenant_id) as conn:
            cutover = datetime.now(tz=timezone.utc)
            prior_docs = self._prior_version_documents(
                conn, tenant_id, anchor_document_id)
            prior_index = (self._current_triples(conn, tenant_id, prior_docs)
                           if prior_docs else {})
            # d.s Stage 3: ontology versions THIS document currently serves.
            # A promoting fact carrying a different version means this wave
            # is an ontology re-extraction cutover (the third supersession
            # trigger) — never anything else, because same-doc facts only
            # ever arrive under a new version by deliberate re-extraction.
            current_versions = self._current_ontology_versions(
                conn, tenant_id, anchor_document_id)
            kept: set[int] = set()
            promoted_versions: set[str] = set()
            for pending_id in pending_ids:
                pending = self.store.get_pending_fact(tenant_id, pending_id)
                fact = self._rewrite_refs(pending)
                if fact is None:
                    continue  # a referenced mention is unresolved: stay pending
                key = (fact.subject_entity_id, fact.predicate,
                       fact.object_entity_id, fact.object_literal)
                surviving = prior_index.get(key)
                if surviving is not None:
                    # Diff: the new version still asserts this — the old
                    # row stays current and continuous; no duplicate row.
                    fact_id = surviving
                    kept.add(surviving)
                else:
                    supersedes_ontology = bool(
                        current_versions and
                        fact.ontology_version not in current_versions)
                    if fact.valid_from is None and (prior_docs or
                                                    supersedes_ontology):
                        fact.valid_from = cutover  # validity begins at cutover
                    fact_id = self.store.write_facts([fact])[0]
                promoted_versions.add(fact.ontology_version)
                conn.execute(
                    "UPDATE pending_facts SET resolution_status = 'promoted',"
                    " promoted_fact_id = %s WHERE tenant_id = %s AND id = %s",
                    (fact_id, tenant_id, pending_id))
                promoted.append(fact_id)
            if prior_docs and promoted:
                self._retract_facts_for_documents(
                    conn, tenant_id, prior_docs, valid_to=cutover,
                    reason=RETRACTED_BY_REVERSION,
                    keep_fact_ids=tuple(kept))
                conn.execute(
                    "UPDATE documents SET valid_to = %s,"
                    " retraction_reason = %s"
                    " WHERE tenant_id = %s AND id = ANY(%s)"
                    "   AND valid_to IS NULL",
                    (cutover, RETRACTED_BY_REVERSION, tenant_id, prior_docs))
            # d.s Stage 3: ontology supersession. When this wave promoted
            # facts under exactly ONE version and the document was serving a
            # DIFFERENT one, the old vocabulary's facts retire — retained,
            # reason-tagged, same cutover timestamp, same transaction: no
            # query window ever sees both vocabularies or neither. NEVER
            # overwrite: the new facts are their own rows; A-vs-B stays
            # answerable via facts.ontology_version. A mixed-version wave
            # (only reachable through odd partial states) deliberately
            # skips — superseding on ambiguous evidence loses data, waiting
            # for the next wave loses nothing. Promotion-gated on purpose:
            # a re-extraction that stages NOTHING leaves the old facts
            # current — an empty new yield must never silently erase a
            # served corpus (the conservative reading of "reversible").
            if (anchor_document_id is not None and promoted
                    and len(promoted_versions) == 1):
                new_version = next(iter(promoted_versions))
                if current_versions - {new_version}:
                    self._retract_facts_for_documents(
                        conn, tenant_id, [anchor_document_id],
                        valid_to=cutover, reason=RETRACTED_BY_ONTOLOGY,
                        keep_fact_ids=tuple(promoted) + tuple(kept),
                        other_than_ontology_version=new_version)
        return promoted

    @staticmethod
    def _current_ontology_versions(conn, tenant_id: str,
                                   document_id: Optional[int]) -> set[str]:
        """Distinct ontology versions on the document's CURRENT facts
        (provenance-link rule) — the ontology-supersession trigger's
        evidence."""
        if document_id is None:
            return set()
        rows = conn.execute(
            """
            SELECT DISTINCT ontology_version FROM facts
            WHERE tenant_id = %s AND valid_to IS NULL
              AND (source_document_id = %s
                   OR (source_document_id IS NULL
                       AND source_chunk_id IN (
                           SELECT id FROM chunks
                           WHERE tenant_id = %s AND document_id = %s)))
            """,
            (tenant_id, document_id, tenant_id, document_id)).fetchall()
        return {r["ontology_version"] for r in rows}

    @staticmethod
    def _prior_version_documents(conn, tenant_id: str,
                                 document_id: Optional[int]) -> list[int]:
        """CURRENT documents rows of earlier raw versions of the same
        logical document (source_system + native_id — the identity
        _next_version stamps versions by). Already-retracted priors are
        excluded, which is what makes the cutover idempotent: the first
        promotion wave for a new version supersedes, later waves find
        nothing left to retire."""
        if document_id is None:
            return []
        rows = conn.execute(
            """
            WITH cur AS (
                SELECT r.source_system, r.source_native_id, r.version
                FROM documents d
                JOIN raw_documents r ON r.id = d.raw_document_id
                WHERE d.tenant_id = %s AND d.id = %s
            )
            SELECT d.id FROM documents d
            JOIN raw_documents r ON r.id = d.raw_document_id
            JOIN cur ON r.source_system = cur.source_system
                    AND r.source_native_id IS NOT DISTINCT FROM
                        cur.source_native_id
                    AND r.version < cur.version
            WHERE d.tenant_id = %s AND r.tenant_id = %s
              AND d.valid_to IS NULL
            ORDER BY d.id
            """,
            (tenant_id, document_id, tenant_id, tenant_id)).fetchall()
        return [r["id"] for r in rows]

    @staticmethod
    def _current_triples(conn, tenant_id: str,
                         doc_ids: list[int]) -> dict[tuple, int]:
        """Triple -> fact id for the CURRENT facts anchored to `doc_ids`
        (the supersession diff's left-hand side). Duplicate triples keep the
        lowest id; the siblings retire at cutover — the assertion stays
        continuous through the kept row."""
        rows = conn.execute(
            """
            SELECT id, subject_entity_id, predicate, object_entity_id,
                   object_literal
            FROM facts
            WHERE tenant_id = %s AND valid_to IS NULL
              AND (source_document_id = ANY(%s)
                   OR (source_document_id IS NULL
                       AND source_chunk_id IN (
                           SELECT id FROM chunks
                           WHERE tenant_id = %s AND document_id = ANY(%s))))
            ORDER BY id
            """,
            (tenant_id, doc_ids, tenant_id, doc_ids)).fetchall()
        index: dict[tuple, int] = {}
        for r in rows:
            key = (r["subject_entity_id"], r["predicate"],
                   r["object_entity_id"], r["object_literal"])
            index.setdefault(key, r["id"])
        return index
