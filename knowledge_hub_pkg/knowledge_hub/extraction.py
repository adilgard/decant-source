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
from knowledge_hub.extraction_parser_supplied import ParserSuppliedStrategy
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
    Chunk,
    Document,
    EntityMention,
    ExtractionRun,
    PendingFact,
    RawDocument,
)
from knowledge_hub.pipeline import Pipeline
from knowledge_hub.plugins import (
    LLM_STRATEGY,
    MODEL_KEY,
    PARSER_SUPPLIED_STRATEGY,
    STRUCTURED_STRATEGY,
    build_fact_parser,
    fact_parser_ref_for,
    source_config,
    strategy_name_for,
)

logger = logging.getLogger(__name__)

GROUNDING_PENALTY = 0.5  # confidence multiplier on a failed grounding check


def document_text_from_chunks(parents: list[Chunk]) -> str:
    """Rebuild the document's extracted text from its persisted parent
    chunks, at their original offsets.

    Extraction needs the extracted text to verify a producer's declared
    spans, but the Parser lives one stage upstream in ProcessingService and
    this service has never held one. Re-parsing here would mean two parses
    that can disagree, and the one that matters is the one chunks were cut
    from — so reconstruct from exactly that.

    Parents tile the text by construction (SectionChunker emits contiguous
    section spans), except that whitespace-only sections are dropped. Those
    gaps are refilled with spaces, which keeps every surviving character at
    its original index. That is the only property this function owes: an
    offset that was valid against the parser's output is valid against
    this string.
    """
    pieces: list[str] = []
    cursor = 0
    for chunk in sorted(parents, key=lambda c: (c.char_start or 0, c.seq)):
        start = chunk.char_start or 0
        if start > cursor:
            pieces.append(" " * (start - cursor))  # a dropped blank section
            cursor = start
        content = chunk.content
        if start < cursor:
            # Overlapping parents should be impossible; if the chunker ever
            # emits them, keep offsets honest by dropping the overlap rather
            # than shifting everything after it.
            content = content[cursor - start:]
        pieces.append(content)
        cursor += len(content)
    return "".join(pieces)


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
                                  ExtractionStrategy]]] = None,
                 llm_strategy_factory: Optional[Callable[
                     [OntologyBinding, str], ExtractionStrategy]] = None):
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
        # parser_supplied strategies, cached per (ontology version, plugin
        # reference). Cached rather than rebuilt per document because the
        # instance carries the idempotency ledger's key material
        # (extractor name + plugin version) and constructing a plugin may
        # be expensive; keyed on the plugin ref because two sources may use
        # two different plugins under the same ontology.
        self._parser_strategies: dict[tuple[str, str],
                                      ParserSuppliedStrategy] = {}
        # d.s Stage 5: per-source model pinning, same shape as the version
        # pin above — a source whose config carries extraction_model runs
        # its prose under THAT served model. The factory builds an llm
        # strategy for (binding, model); instances are cached per
        # (ontology version, model) because the strategy caches its own
        # extractor_version stamp (model@digest). Injected, like
        # strategy_factory, so the service stays typed against the seam.
        self._llm_strategy_factory = llm_strategy_factory
        self._llm_by_model: dict[tuple[str, str], ExtractionStrategy] = {}

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

        # THE ROUTING SEAM. This used to be `if data_track == PROSE_TRACK`,
        # which conflated two questions: what SHAPE is this content (which
        # drives parsing and chunking) and WHO produces its facts. A source
        # can now answer them independently, by config. With no
        # `extraction_strategy` declared, strategy_name_for reproduces the
        # old branch exactly, so nothing that predates this changes.
        config = source_config(self.store, raw)
        strategy_name = strategy_name_for(config, document.data_track)
        if strategy_name == LLM_STRATEGY:
            summary = self._extract_prose(
                raw, document, binding,
                self._llm_for(raw, binding, llm_strategy,
                              config.get(MODEL_KEY)))
        elif strategy_name == STRUCTURED_STRATEGY:
            summary = self._extract_structured(raw, document, binding,
                                               structured_strategy, config)
        else:  # PARSER_SUPPLIED_STRATEGY
            summary = self._extract_parser_supplied(
                raw, document, binding,
                self._parser_supplied_strategy(raw, binding, config))

        self._mark_extracted(tenant_id, raw_document_id)
        return summary

    def _parser_supplied_strategy(self, raw: RawDocument,
                                  binding: OntologyBinding,
                                  config: dict) -> ParserSuppliedStrategy:
        """Build (or reuse) the conformance gate around this source's
        plugin. The plugin arrives as a STRING from config and is resolved
        through the registry, which is what keeps core's import graph free
        of every domain package that will ever exist."""
        ref = fact_parser_ref_for(config)
        if ref is None:
            raise ExtractionError(
                raw.tenant_id, None, None,
                f"raw_document id={raw.id} selects the "
                f"{PARSER_SUPPLIED_STRATEGY!r} strategy but its source "
                f"config names no {PARSER_SUPPLIED_STRATEGY} plugin — set "
                f"'fact_parser' on the source (console: edit scope)")
        key = (binding.version, ref)
        if key not in self._parser_strategies:
            self._parser_strategies[key] = ParserSuppliedStrategy(
                binding, build_fact_parser(ref))
        return self._parser_strategies[key]

    def _llm_for(self, raw: RawDocument, binding: OntologyBinding,
                 default: ExtractionStrategy,
                 model: Optional[str]) -> ExtractionStrategy:
        """The llm strategy this document's prose runs under: its source's
        pinned model if one is configured (d.s Stage 5), else the injected
        default. Same hard-error rule as the version pin: silently
        extracting under the wrong model would stamp a provenance lie, so
        a pin with no factory nacks and stays visible on the queue."""
        if not model or model == getattr(default, "model", None):
            return default
        key = (binding.version, model)
        if key not in self._llm_by_model:
            if self._llm_strategy_factory is None:
                raise ExtractionError(
                    raw.tenant_id, None, None,
                    f"raw_document id={raw.id}'s source pins "
                    f"extraction_model {model!r} but this ExtractionService "
                    f"has no llm_strategy_factory — wire one "
                    f"(operator_jobs/deploy_launch do) or clear the pin")
            self._llm_by_model[key] = self._llm_strategy_factory(binding,
                                                                 model)
        return self._llm_by_model[key]

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
                            strategy: ExtractionStrategy,
                            config: dict) -> ExtractSummary:
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
            # `config` is the merged source config the router already
            # resolved (per-item native_metadata over the registry row) —
            # the structured_map lookup that used to live here was a third
            # copy of that same precedence rule, so it is gone.
            config={"structured_map": config.get("structured_map")})
        result = strategy.extract(unit)
        self._finalize(unit, result, _DocDigest(), raw.content_hash,
                       strategy, binding, summary)
        return summary

    # -------------------------------------------------- parser_supplied  --
    def _extract_parser_supplied(self, raw: RawDocument, document: Document,
                                 binding: OntologyBinding,
                                 strategy: ParserSuppliedStrategy
                                 ) -> ExtractSummary:
        """One unit per document, no LLM, no chunk iteration.

        A plugin reads the whole document because structure is the thing it
        understands: a cross-reference from one section to another is not
        visible from inside either section alone. Prose chunking and
        embedding already happened upstream and are untouched — this
        replaces only the fact producer, so the same document is fully
        retrievable AND carries deterministic facts.

        Facts land anchored to the parent chunk their span falls inside, so
        retrieval's grounded-facts enrichment (which joins on
        source_chunk_id) surfaces them exactly like LLM facts. Nothing
        downstream has to know a plugin was involved.
        """
        tenant_id = document.tenant_id
        summary = ExtractSummary(
            tenant_id=tenant_id, raw_document_id=raw.id,
            document_id=document.id, status="extracted", units=1)
        # Ledger key is the document's content hash, like the structured
        # track: one unit, one run. The strategy's extractor name carries
        # the plugin version, so a plugin upgrade is fresh work, never a
        # replay of the old plugin's verdict.
        if self._already_ran(tenant_id, raw.content_hash, strategy, binding):
            summary.units_replayed, summary.status = 1, "replayed"
            return summary

        parents = self._parents(tenant_id, document.id)
        if not parents:
            raise ExtractionError(
                tenant_id, document.id, None,
                f"document has no parent chunks, so a declared span has "
                f"nothing to be verified against. A {PARSER_SUPPLIED_STRATEGY} "
                f"source is expected to chunk normally for retrieval — check "
                f"that its data_track is 'prose' and that processing finished")

        unit = ExtractionUnit(
            document=document, source_system=raw.source_system,
            chunk=None,
            text=document_text_from_chunks(parents),
            payload=self.raw_store.get(raw.raw_uri))
        result = strategy.extract(unit)
        self._finalize(unit, result, _DocDigest(), raw.content_hash,
                       strategy, binding, summary,
                       span_chunks=[(p.char_start, p.char_end, p.id)
                                    for p in parents
                                    if p.char_start is not None
                                    and p.char_end is not None])
        return summary

    # ------------------------------------------------------ finalize/stage --
    def _finalize(self, unit: ExtractionUnit, result: ExtractionResult,
                  digest: _DocDigest, unit_hash: str,
                  strategy: ExtractionStrategy, binding: OntologyBinding,
                  summary: ExtractSummary,
                  span_chunks: Optional[list[tuple[int, int, int]]] = None
                  ) -> None:
        """Ground -> envelope -> intra-unit mention dedup -> stage_pending ->
        record the run, atomically. Quarantined items persist regardless —
        they are observations, not part of the staged unit.

        `span_chunks` is (char_start, char_end, chunk_id) for the document's
        parent chunks, supplied by whole-document units so a fact can be
        anchored to the chunk its span falls inside. Chunk-scoped units pass
        nothing: they already know their chunk."""
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
            if ent.char_start is not None and ent.char_end is not None:
                # DECLARED (parser_supplied): the producer computed where
                # this surface form is. Searching for it instead would take
                # the first occurrence, which in a document that names the
                # same thing repeatedly is usually not this one.
                m_start, m_end = ent.char_start, ent.char_end
            else:
                span = (find_span(ent.surface_text, unit.text)
                        if unit.text else None)
                base = (unit.chunk.char_start or 0) if unit.chunk else 0
                m_start = base + span[0] if span else None
                m_end = base + span[1] if span else None
            mentions[ent.key] = EntityMention(
                tenant_id=tenant_id,
                surface_text=ent.surface_text,
                entity_type=ent.entity_type,
                source_system=unit.source_system,
                source_document_id=unit.document.id,
                source_chunk_id=(unit.chunk.id if unit.chunk else
                                 self._chunk_for_span(span_chunks, m_start,
                                                      m_end)),
                char_start=m_start,
                char_end=m_end,
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
                # A whole-document unit anchors each fact to the parent
                # chunk containing its verified span, so retrieval's
                # facts_citing enrichment (which joins on source_chunk_id)
                # surfaces plugin facts exactly like model facts. A span
                # straddling two parents stays document-anchored rather
                # than being assigned to an arbitrary one of them.
                source_chunk_id=(unit.chunk.id if unit.chunk else
                                 self._chunk_for_span(span_chunks,
                                                      grounding.char_start,
                                                      grounding.char_end)),
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

    @staticmethod
    def _chunk_for_span(span_chunks: Optional[list[tuple[int, int, int]]],
                        start: Optional[int],
                        end: Optional[int]) -> Optional[int]:
        """The parent chunk wholly containing [start, end), or None. None is
        a real answer, not a failure: a span with no home chunk (unverified,
        or straddling a section boundary) stays anchored to the document,
        which is a weaker citation but a true one."""
        if not span_chunks or start is None:
            return None
        finish = end if end is not None else start
        for chunk_start, chunk_end, chunk_id in span_chunks:
            if chunk_start <= start and finish <= chunk_end:
                return chunk_id
        return None

    def _ground(self, unit: ExtractionUnit, fact: CandidateFact,
                surfaces: dict[str, str], subject_ref: str,
                object_ref: Optional[str]):
        """Three producers, three honest verdicts.

        DECLARED (parser_supplied) — the producer computed offsets and named
        the text there, so slice and compare. Checked FIRST because it is
        the strongest available check: it can prove the producer wrong,
        which searching for a quote cannot.
        QUOTED (LLM) — re-find the model's quote in the parent text.
        CONSTRUCTED (SoR) — no span and none claimed; the cell locator IS
        the provenance and there is no assertion of position to verify.
        """
        if fact.char_start is not None and fact.char_end is not None:
            return self.grounder.verify_span(
                fact.evidence, fact.char_start, fact.char_end, unit.text)
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
