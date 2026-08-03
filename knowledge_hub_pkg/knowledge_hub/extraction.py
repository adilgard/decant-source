"""Extraction flow (Build Prompt 4): parent chunks -> staged candidate facts.

ExtractionService is processing's downstream twin: where ProcessingService
turns landed bytes into the persisted chunk hierarchy, this turns persisted
parents into validated, grounded, ontology-conformant candidate facts +
mentions, STAGED for the resolver (pending_facts via stage_pending — never
`facts` directly; pre-resolution refs are 'mention:<id>', promotion is the
resolver's job). Per document:

    route by document.data_track       prose -> LLM joint pass per parent;
                                       structured -> deterministic column map
      -> strategy.extract(unit)        candidates + quarantined + stats
      -> Grounder.ground(...)          deterministic span verify (LLM facts)
      -> envelope                      ontology_version, extractor@digest,
                                       confidence, inherited security label
      -> stage_pending                 mentions + pending facts, atomically,
      -> extraction_runs               with the run record in the SAME
                                       transaction (observability + the
                                       idempotency ledger)
      -> raw_documents.status = 'extracted'

Sequencing: parents are processed SEQUENTIALLY within a document — the
entity digest accumulates so later parents resolve coreference against
earlier ones. Parallelism belongs ACROSS documents: consume() claims with
FOR UPDATE SKIP LOCKED, so extra workers scale out without coordination.

Idempotency is content-hash keyed: one status='ok' extraction_runs row per
(tenant, unit content_hash, extractor, extractor_version, ontology_version).
A redelivered or re-run document skips already-extracted units (replay); a
concurrent duplicate insert trips the unique index and rolls the whole
stage+record transaction back, so at-least-once delivery never double-stages.

consume() is the extraction_queue consumer (claim -> extract -> ack; poison
-> nack + error recorded + lease redelivery), exactly like ProcessingService
on dispatch_queue — the outbox pattern repeated one stage downstream.

Quality note: this flow makes extraction CORRECT AS MACHINERY and
observable. Whether the model extracted the right facts is the benchmark's
question; the per-fact grounding results, per-item quarantine reasons, and
per-unit token/wall numbers persisted here are that benchmark's inputs.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from pydantic import BaseModel

from knowledge_hub.dispatch_pg import PostgresDispatcher
from knowledge_hub.grounding import find_span
from knowledge_hub.interfaces import (
    CandidateFact,
    DigestEntry,
    ExtractionError,
    ExtractionResult,
    ExtractionStrategy,
    ExtractionUnit,
    Grounder,
    GroundingResult,
    OntologyBinding,
    RawStore,
)
from knowledge_hub.models import (
    PROSE_TRACK,
    Chunk,
    Document,
    EntityMention,
    ExtractionRun,
    PendingFact,
    RawDocument,
)
from knowledge_hub.pipeline import Pipeline

logger = logging.getLogger(__name__)

GROUNDING_PENALTY = 0.5  # confidence multiplier on a failed grounding check


class ExtractSummary(BaseModel):
    """Summary of extracting one raw document (returned, safe to log)."""
    tenant_id: str
    raw_document_id: int
    document_id: Optional[int] = None
    status: str          # extracted|replayed|review
    reason: Optional[str] = None
    units: int = 0
    units_replayed: int = 0
    facts: int = 0
    mentions: int = 0
    quarantined: int = 0
    grounding_flags: int = 0


class _DocDigest:
    """The document's carried-forward entity digest: every entity staged for
    this document so far, keyed 'e<mention_id>'. Rebuilt from the database on
    entry (so a resumed/partially-replayed document keeps its coreference
    context) and accumulated in memory as units stage. The most-referenced
    entities sort first, so prompt truncation keeps them sticky."""

    def __init__(self) -> None:
        self.entries: dict[str, DigestEntry] = {}
        self._by_name: dict[tuple[str, str], str] = {}

    def add(self, mention_id: int, surface_text: str, entity_type: str,
            refs: int = 0) -> str:
        key = f"e{mention_id}"
        self.entries[key] = DigestEntry(
            key=key, mention_id=mention_id, surface_text=surface_text,
            entity_type=entity_type, refs=refs)
        self._by_name.setdefault((surface_text.casefold(), entity_type), key)
        return key

    def match(self, surface_text: str, entity_type: str) -> Optional[DigestEntry]:
        key = self._by_name.get((surface_text.casefold(), entity_type))
        return self.entries[key] if key else None

    def get(self, key: str) -> Optional[DigestEntry]:
        return self.entries.get(key)

    def bump(self, key: str) -> None:
        if key in self.entries:
            self.entries[key].refs += 1

    def for_prompt(self) -> list[DigestEntry]:
        return sorted(self.entries.values(),
                      key=lambda d: (-d.refs, d.mention_id))


class ExtractionService:
    def __init__(self, pipeline: Pipeline, raw_store: RawStore,
                 binding: OntologyBinding,
                 llm_strategy: ExtractionStrategy,
                 structured_strategy: ExtractionStrategy,
                 grounder: Grounder,
                 dispatcher: Optional[PostgresDispatcher] = None,
                 strategy_factory: Optional[Callable[
                     [str], tuple[OntologyBinding, ExtractionStrategy,
                                  ExtractionStrategy]]] = None):
        self.pipeline = pipeline
        self.store = pipeline.store
        self.raw_store = raw_store
        self.binding = binding
        self.llm_strategy = llm_strategy
        self.structured_strategy = structured_strategy
        self.grounder = grounder
        self.dispatcher = dispatcher  # the extraction_queue dispatcher
        # d.s Stage 2: per-document ontology pinning. A raw document whose
        # native_metadata carries ontology_version_override (stamped at
        # capture time by a console folder job — the version FIXED at job
        # creation) extracts under THAT version, not the process default.
        # The factory builds the (binding, llm, structured) trio for a
        # version; trios are cached per version. Injected, so this service
        # stays typed against the seams, never the concrete strategies.
        self._strategy_factory = strategy_factory
        self._trios: dict[str, tuple[OntologyBinding, ExtractionStrategy,
                                     ExtractionStrategy]] = {
            binding.version: (binding, llm_strategy, structured_strategy)}

    def _trio_for(self, raw: RawDocument,
                  ontology_version: Optional[str] = None) -> tuple[
            OntologyBinding, ExtractionStrategy, ExtractionStrategy]:
        """The binding + strategies this document extracts under: an
        explicit call-time pin first (Stage 3 re-extraction passes the
        job's frozen version per call), else its capture-time stamp, else
        the service default. An override with no factory is a HARD error —
        extracting under the wrong version silently would be a provenance
        lie; the nack keeps the document visible on the queue instead."""
        override = ontology_version if ontology_version is not None else \
            (raw.native_metadata or {}).get("ontology_version_override")
        if override is None or override == self.binding.version:
            return self.binding, self.llm_strategy, self.structured_strategy
        if override not in self._trios:
            if self._strategy_factory is None:
                raise ExtractionError(
                    raw.tenant_id, None, None,
                    f"raw_document id={raw.id} is pinned to ontology "
                    f"{override!r} but this ExtractionService has no "
                    f"strategy_factory — wire one (operator_jobs/run_ingest "
                    f"do) or re-run without the pin")
            self._trios[override] = self._strategy_factory(override)
        return self._trios[override]

    # ------------------------------------------------------------ consume --
    def consume(self, tenant_id: str, limit: int = 10) -> list[ExtractSummary]:
        """Drain up to `limit` queued documents: claim -> extract -> ack.
        A failure nacks that ONE message (error recorded, lease redelivers)
        and the drain continues — one poison document must not wedge the
        queue."""
        if self.dispatcher is None:
            raise RuntimeError("ExtractionService was built without a dispatcher")
        results: list[ExtractSummary] = []
        for message in self.dispatcher.claim(tenant_id, limit=limit):
            try:
                result = self.extract(tenant_id, message.raw_document_id)
            except Exception as e:
                logger.warning("extraction for raw_document id=%s failed: %s",
                               message.raw_document_id, e)
                self.dispatcher.nack(tenant_id, message.id,
                                     error=f"{type(e).__name__}: {e}")
                continue
            self.dispatcher.ack(tenant_id, message.id)
            results.append(result)
        return results

    # ------------------------------------------------------------ extract --
    def extract(self, tenant_id: str, raw_document_id: int,
                ontology_version: Optional[str] = None) -> ExtractSummary:
        """Extract one processed document into staged mentions + pending
        facts. Idempotent per unit content hash: already-extracted units
        replay as no-ops. `ontology_version` pins THIS call to a specific
        vocabulary (Stage 3 re-extraction: the job's frozen target), taking
        precedence over any capture-time stamp; the idempotency ledger keys
        on the version, so the same content under a new version is fresh
        work, never a replay."""
        raw = self.store.get_raw_document(tenant_id, raw_document_id)
        if raw is None:
            raise LookupError(f"raw_document id={raw_document_id} not found "
                              f"for tenant {tenant_id!r}")
        document = self._document_for(tenant_id, raw_document_id)
        if document is None:
            # Not processed yet (or processing crashed before the document
            # row): a nack-worthy state — the lease redelivers after
            # processing catches up.
            raise ExtractionError(
                tenant_id, None, None,
                f"raw_document id={raw_document_id} has no processed "
                "document row yet")
        if document.review_status == "review":
            # §8.1a hold: a human owns this document right now. Ack (the
            # adjudication path re-runs extraction directly).
            return ExtractSummary(
                tenant_id=tenant_id, raw_document_id=raw_document_id,
                document_id=document.id, status="review",
                reason=document.review_reason)

        binding, llm_strategy, structured_strategy = self._trio_for(
            raw, ontology_version)
        if document.data_track == PROSE_TRACK:
            summary = self._extract_prose(raw, document, binding,
                                          llm_strategy)
        else:
            summary = self._extract_structured(raw, document, binding,
                                               structured_strategy)

        self._mark_extracted(tenant_id, raw_document_id)
        return summary

    # ------------------------------------------------------- prose track --
    def _extract_prose(self, raw: RawDocument, document: Document,
                       binding: OntologyBinding,
                       strategy: ExtractionStrategy) -> ExtractSummary:
        tenant_id = document.tenant_id
        parents = self._parents(tenant_id, document.id)
        if not parents:
            raise ExtractionError(
                tenant_id, document.id, None,
                "prose document has no parent chunks — processing is "
                "incomplete, redeliver after Stage B finishes")
        digest = self._load_digest(tenant_id, document.id)
        summary = ExtractSummary(
            tenant_id=tenant_id, raw_document_id=raw.id,
            document_id=document.id, status="extracted", units=len(parents))

        for parent in parents:
            if self._already_ran(tenant_id, parent.content_hash,
                                 strategy, binding):
                summary.units_replayed += 1
                continue
            unit = ExtractionUnit(
                document=document, source_system=raw.source_system,
                chunk=parent, text=parent.content,
                digest=digest.for_prompt())
            result = strategy.extract(unit)
            self._finalize(unit, result, digest, parent.content_hash,
                           strategy, binding, summary)

        if summary.units_replayed == summary.units:
            summary.status = "replayed"
        return summary

    # -------------------------------------------------- structured track --
    def _extract_structured(self, raw: RawDocument, document: Document,
                            binding: OntologyBinding,
                            strategy: ExtractionStrategy) -> ExtractSummary:
        tenant_id = document.tenant_id
        summary = ExtractSummary(
            tenant_id=tenant_id, raw_document_id=raw.id,
            document_id=document.id, status="extracted", units=1)
        if self._already_ran(tenant_id, raw.content_hash,
                             strategy, binding):
            summary.units_replayed, summary.status = 1, "replayed"
            return summary
        unit = ExtractionUnit(
            document=document, source_system=raw.source_system,
            payload=self.raw_store.get(raw.raw_uri),
            config={"structured_map": self._structured_map(raw)})
        result = strategy.extract(unit)
        self._finalize(unit, result, _DocDigest(), raw.content_hash,
                       strategy, binding, summary)
        return summary

    def _structured_map(self, raw: RawDocument) -> Optional[dict]:
        """The manifest's structured_map: a per-item native_metadata
        declaration wins; otherwise the source's registry config (same
        precedence as the data_track declaration in Stage B)."""
        native = raw.native_metadata or {}
        if native.get("structured_map"):
            return native["structured_map"]
        source_ref = native.get("source_ref")
        if not source_ref:
            return None
        with self.store.transaction(raw.tenant_id) as conn:
            row = conn.execute(
                "SELECT config FROM source_registry"
                " WHERE tenant_id = %s AND source_ref = %s",
                (raw.tenant_id, source_ref)).fetchone()
        return ((row or {}).get("config") or {}).get("structured_map")

    # ------------------------------------------------------ finalize/stage --
    def _finalize(self, unit: ExtractionUnit, result: ExtractionResult,
                  digest: _DocDigest, unit_hash: str,
                  strategy: ExtractionStrategy, binding: OntologyBinding,
                  summary: ExtractSummary) -> None:
        """Ground -> envelope -> intra-unit mention dedup -> stage_pending ->
        record the run, atomically. Quarantined items persist regardless —
        they are observations, not part of the staged unit."""
        tenant_id = unit.document.tenant_id
        for q in result.quarantined:
            self.store.insert_quarantine(q)
        summary.quarantined += len(result.quarantined)

        # New entities: fold into the digest when an earlier unit already
        # staged this (surface, type); otherwise build a mention to stage.
        keymap: dict[str, str] = {}   # candidate key -> batch key or mention ref
        surfaces: dict[str, str] = {d.key: d.surface_text
                                    for d in digest.entries.values()}
        mentions: dict[str, EntityMention] = {}
        pending_entities = {}
        batch_by_name: dict[tuple[str, str], str] = {}
        for ent in result.entities:
            known = digest.match(ent.surface_text, ent.entity_type)
            if known is not None:
                keymap[ent.key] = f"mention:{known.mention_id}"
                surfaces[f"mention:{known.mention_id}"] = known.surface_text
                continue
            name_key = (ent.surface_text.casefold(), ent.entity_type)
            if name_key in batch_by_name:  # same surface twice in one unit
                keymap[ent.key] = batch_by_name[name_key]
                continue
            batch_by_name[name_key] = ent.key
            keymap[ent.key] = ent.key
            surfaces[ent.key] = ent.surface_text
            pending_entities[ent.key] = ent
            span = find_span(ent.surface_text, unit.text) if unit.text else None
            base = (unit.chunk.char_start or 0) if unit.chunk else 0
            mentions[ent.key] = EntityMention(
                tenant_id=tenant_id,
                surface_text=ent.surface_text,
                entity_type=ent.entity_type,
                source_system=unit.source_system,
                source_document_id=unit.document.id,
                source_chunk_id=unit.chunk.id if unit.chunk else None,
                char_start=base + span[0] if span else None,
                char_end=base + span[1] if span else None,
                locator=unit.chunk.locator if unit.chunk else None,
                extracted_keys=ent.extracted_keys)

        # Digest keys used directly by facts (coreference hits) -> refs.
        def resolve(key: Optional[str]) -> Optional[str]:
            if key is None:
                return None
            if key in keymap:
                return keymap[key]
            entry = digest.get(key)
            if entry is not None:
                ref = f"mention:{entry.mention_id}"
                surfaces[ref] = entry.surface_text
                return ref
            raise ExtractionError(
                tenant_id, unit.document.id,
                unit.chunk.id if unit.chunk else None,
                f"fact references unknown key {key!r} (strategy conformance "
                "bug)")

        staged_facts: list[PendingFact] = []
        unit_flags = 0
        for fact in result.facts:
            subject_ref = resolve(fact.subject_key)
            object_ref = resolve(fact.object_key)
            grounding = self._ground(unit, fact, surfaces, subject_ref,
                                     object_ref)
            confidence = fact.confidence
            needs_review = False
            if not grounding.passed:
                confidence *= GROUNDING_PENALTY
                needs_review = True
                unit_flags += 1
            staged_facts.append(PendingFact(
                tenant_id=tenant_id,
                subject_ref=subject_ref,
                predicate=fact.predicate,
                object_ref=object_ref,
                object_literal=fact.object_literal,
                attributes=({"evidence": fact.evidence} if fact.evidence
                            else {}),
                ontology_version=binding.version,
                source_document_id=unit.document.id,
                source_chunk_id=unit.chunk.id if unit.chunk else None,
                char_start=grounding.char_start,
                char_end=grounding.char_end,
                locator=fact.locator or (unit.chunk.locator if unit.chunk
                                         else None),
                extractor=strategy.extractor,
                extractor_version=strategy.version,
                confidence=confidence,
                security_label_id=unit.document.security_label_id,
                grounding=grounding.status,
                needs_review=needs_review))

        # Drop mentions nothing references only if the strategy produced
        # facts at all? No — an entity observation is a valid output even
        # factless (SoR without a column map, a name-drop paragraph).

        run = ExtractionRun(
            tenant_id=tenant_id, document_id=unit.document.id,
            source_chunk_id=unit.chunk.id if unit.chunk else None,
            unit_hash=unit_hash, strategy=strategy.extractor,
            extractor=strategy.extractor, extractor_version=strategy.version,
            ontology_version=binding.version,
            prompt_tokens=result.stats.prompt_tokens,
            output_tokens=result.stats.output_tokens,
            wall_ms=result.stats.wall_ms,
            facts_staged=len(staged_facts), mentions_staged=len(mentions),
            quarantined=len(result.quarantined),
            grounding_flags=unit_flags, repairs=result.stats.repairs)

        # Stage + record atomically: if a concurrent worker already recorded
        # an ok-run for this unit, the unique index rolls BOTH back — no
        # double staging under at-least-once delivery.
        with self.store.transaction(tenant_id):
            staged = self.store.stage_pending(mentions, staged_facts)
            self.store.insert_extraction_run(run)

        summary.facts += len(staged_facts)
        summary.mentions += len(mentions)
        summary.grounding_flags += unit_flags

        # Accumulate the digest for the document's later units.
        for key, mention_id in staged.mention_ids.items():
            ent = pending_entities[key]
            digest.add(mention_id, ent.surface_text, ent.entity_type)
        for fact in staged_facts:
            for ref in (fact.subject_ref, fact.object_ref):
                if ref and ref.startswith("mention:"):
                    digest.bump(f"e{ref.split(':', 1)[1]}")

    def _ground(self, unit: ExtractionUnit, fact: CandidateFact,
                surfaces: dict[str, str], subject_ref: str,
                object_ref: Optional[str]):
        """LLM facts get deterministic span verification against the parent
        text; SoR facts are grounded by construction (cell locator IS the
        provenance — there is no model quote to verify)."""
        if unit.chunk is None:
            return GroundingResult(status="construction")
        components = [surfaces.get(subject_ref, "")]
        if object_ref is not None:
            components.append(surfaces.get(object_ref, ""))
        elif fact.object_literal:
            components.append(fact.object_literal)
        return self.grounder.ground(
            fact.evidence, components, unit.text,
            base_offset=unit.chunk.char_start or 0)

    # ----------------------------------------------------------- internals --
    def _document_for(self, tenant_id: str,
                      raw_document_id: int) -> Optional[Document]:
        with self.store.transaction(tenant_id) as conn:
            row = conn.execute(
                "SELECT id FROM documents"
                " WHERE tenant_id = %s AND raw_document_id = %s"
                " ORDER BY id LIMIT 1",
                (tenant_id, raw_document_id)).fetchone()
        return self.store.get_document(tenant_id, row["id"]) if row else None

    def _parents(self, tenant_id: str, document_id: int) -> list[Chunk]:
        with self.store.transaction(tenant_id) as conn:
            rows = conn.execute(
                "SELECT id FROM chunks"
                " WHERE tenant_id = %s AND document_id = %s"
                "   AND level = 'parent' ORDER BY seq, id",
                (tenant_id, document_id)).fetchall()
        return [self.store.get_chunk(tenant_id, r["id"]) for r in rows]

    def _already_ran(self, tenant_id: str, unit_hash: str,
                     strategy: ExtractionStrategy,
                     binding: OntologyBinding) -> bool:
        # The idempotency ledger keys on the ontology version too, so the
        # same content under a NEW version is fresh work, never a replay —
        # the property Stage 3's re-extraction rides on.
        return self.store.find_extraction_run(
            tenant_id, unit_hash, strategy.extractor, strategy.version,
            binding.version) is not None

    def _load_digest(self, tenant_id: str, document_id: int) -> _DocDigest:
        """Rebuild the document's digest from staged mentions + their fact
        reference counts — a resumed document keeps its coreference context
        across process restarts and partial replays."""
        digest = _DocDigest()
        with self.store.transaction(tenant_id) as conn:
            rows = conn.execute(
                """
                SELECT m.id, m.surface_text, m.entity_type,
                       (SELECT count(*) FROM pending_facts pf
                         WHERE pf.tenant_id = m.tenant_id
                           AND (pf.subject_ref = 'mention:' || m.id
                                OR pf.object_ref = 'mention:' || m.id)) AS refs
                FROM entity_mentions m
                WHERE m.tenant_id = %s AND m.source_document_id = %s
                ORDER BY m.id
                """,
                (tenant_id, document_id)).fetchall()
        for r in rows:
            digest.add(r["id"], r["surface_text"], r["entity_type"],
                       refs=r["refs"])
        return digest

    def _mark_extracted(self, tenant_id: str, raw_document_id: int) -> None:
        with self.store.transaction(tenant_id) as conn:
            conn.execute(
                "UPDATE raw_documents SET status = 'extracted'"
                " WHERE tenant_id = %s AND id = %s",
                (tenant_id, raw_document_id))
