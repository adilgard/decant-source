"""ParserSuppliedStrategy — the conformance gate around a fact plugin.

A registered plugin states what it found; THIS validates it and turns it
into candidates. No LLM is anywhere in the path, by construction: the only
thing that ran was the plugin's own deterministic code.

WHY THE GATE IS HERE AND NOT IN THE PLUGIN. Ontology validation in this
codebase has always lived inside the strategy — the LLM strategy checks
its own model output, the structured strategy checks its own column map,
and `ExtractionService._finalize` trusts whatever a strategy returns and
does no vocabulary checking of its own. So a plugin allowed to return an
`ExtractionResult` directly would sit DOWNSTREAM of the only allowlist
check that exists, and could write any predicate it liked into
pending_facts. The database would not catch it either: `facts.predicate`
is a bare TEXT column and only `ontology_version` carries a foreign key.

The fix is the shape of this module. A plugin returns `ParsedFact`, a
statement of what it found; this class is the only thing that can turn one
into a candidate, and it applies exactly the checks the LLM path applies:

    predicate  -> binding.normalize_predicate  (alias data may map surface
                  variants and may request a subject/object swap; genuine
                  unknowns quarantine as 'unbound_predicate')
    entity type-> binding.is_entity_type       ('unbound_entity_type')
    shape      -> exactly one object flavor    ('validation_failure')

Rejects are QUARANTINED, never dropped: the review queue shows what the
plugin tried to say, which is the same signal that grows an ontology when
a model tries it. A parser gets no more trust than a model here, and that
is deliberate. It is a different kind of producer, not a privileged one.

WHAT THIS CLASS DOES NOT KNOW. It does not know what corpus it is serving,
what format the plugin read, or what the predicates mean. It sees strings
and offsets. Every domain-specific decision was made before `parse_facts`
returned.

Entity identity within a document is by (surface text, entity type),
resolved HERE rather than in plugins, so two facts about the same thing
share one mention and every plugin gets the rule for free. Cross-document
identity remains the resolver's job, untouched.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

from knowledge_hub.interfaces import (
    CandidateEntity,
    CandidateFact,
    ExtractionError,
    ExtractionResult,
    ExtractionStats,
    ExtractionStrategy,
    ExtractionUnit,
    FactParser,
    OntologyBinding,
    ParsedFact,
)
from knowledge_hub.models import QuarantinedExtraction

logger = logging.getLogger(__name__)

STRATEGY_PREFIX = "parser_supplied"


class ParserSuppliedStrategy(ExtractionStrategy):
    """One plugin, bound to one ontology version. Built per (version, plugin)
    by ExtractionService and cached there, so `extractor`/`version` are
    stable for the idempotency ledger: the ledger key names the plugin and
    its code version, which means shipping a new plugin version makes the
    same document fresh work instead of a replay. That is the correct
    behavior and it falls out of naming the producer honestly."""

    def __init__(self, binding: OntologyBinding, fact_parser: FactParser):
        self.binding = binding
        self.plugin = fact_parser
        # Per-instance, unlike the shipped strategies (see the ABC note):
        # provenance names the plugin, not just the seam it came through.
        self.extractor = f"{STRATEGY_PREFIX}:{fact_parser.name}"
        self.version = fact_parser.version

    # -------------------------------------------------- ExtractionStrategy --
    def extract(self, unit: ExtractionUnit) -> ExtractionResult:
        if unit.payload is None:
            raise ExtractionError(
                unit.document.tenant_id, unit.document.id, None,
                f"{self.extractor} needs the landed payload bytes")

        started = time.monotonic()
        parsed = self.plugin.parse_facts(unit.document, unit.text,
                                         unit.payload)
        result = ExtractionResult(stats=ExtractionStats(
            wall_ms=int((time.monotonic() - started) * 1000)))

        # (surface casefolded, entity type) -> candidate key. One mention per
        # distinct thing in this document, however many facts reference it.
        keys: dict[tuple[str, str], str] = {}

        for index, item in enumerate(parsed):
            self._conform(unit, index, item, keys, result)

        logger.info(
            "%s produced %d fact(s) and %d entity/entities for document "
            "id=%s (%d quarantined)", self.extractor, len(result.facts),
            len(result.entities), unit.document.id, len(result.quarantined))
        return result

    # ----------------------------------------------------------- internals --
    def _conform(self, unit: ExtractionUnit, index: int, item: ParsedFact,
                 keys: dict[tuple[str, str], str],
                 result: ExtractionResult) -> None:
        """Validate ONE stated assertion and append it, or quarantine it.

        Order matters: shape is checked before vocabulary, so a malformed
        item is reported as malformed rather than as an ontology miss. An
        operator reading the review queue needs to know which of the two
        it is, because the fixes are completely different (patch the
        plugin, versus extend the ontology)."""
        raw: dict[str, Any] = {"index": index, "parsed_fact": item.model_dump()}

        # --- shape -----------------------------------------------------
        has_object_entity = bool(item.object_text)
        has_literal = item.object_literal is not None and item.object_literal != ""
        if has_object_entity == has_literal:
            result.quarantined.append(self._quarantine(
                unit, "validation_failure",
                "a fact needs exactly one of object_text or object_literal, "
                f"got {'both' if has_object_entity else 'neither'}", raw))
            return
        if not item.subject_text.strip():
            result.quarantined.append(self._quarantine(
                unit, "validation_failure", "empty subject_text", raw))
            return
        if has_object_entity and not item.object_type:
            result.quarantined.append(self._quarantine(
                unit, "validation_failure",
                "an entity-valued fact needs object_type", raw))
            return

        # --- vocabulary ------------------------------------------------
        normalized = self.binding.normalize_predicate(item.predicate)
        if normalized is None:
            result.quarantined.append(self._quarantine(
                unit, "unbound_predicate", item.predicate, raw))
            return
        predicate, swap = normalized

        if not self.binding.is_entity_type(item.subject_type):
            result.quarantined.append(self._quarantine(
                unit, "unbound_entity_type", item.subject_type, raw))
            return
        if has_object_entity and not self.binding.is_entity_type(
                item.object_type):
            result.quarantined.append(self._quarantine(
                unit, "unbound_entity_type", item.object_type, raw))
            return
        if swap and not has_object_entity:
            # The ontology's alias data says this surface form reverses the
            # triple, but there is no object entity to reverse it with.
            # Guessing (drop the swap? invent a subject?) would silently
            # store a fact that means something else.
            result.quarantined.append(self._quarantine(
                unit, "validation_failure",
                f"predicate {item.predicate!r} maps to {predicate!r} with a "
                f"subject/object swap, which a literal-valued fact cannot "
                f"satisfy", raw))
            return

        # --- candidates -------------------------------------------------
        subject_key = self._entity_key(
            keys, result, item.subject_text, item.subject_type,
            item.subject_keys, item.subject_char_start, item.subject_char_end)
        object_key: Optional[str] = None
        if has_object_entity:
            object_key = self._entity_key(
                keys, result, item.object_text, item.object_type,
                item.object_keys, item.object_char_start,
                item.object_char_end)

        subj, obj = subject_key, object_key
        if swap:
            subj, obj = obj, subj

        start, end = item.char_start, item.char_end
        if start is None or end is None or end <= start:
            # A half-declared or inverted span is no span: fall through to
            # the flow's construction handling rather than staging offsets
            # that would slice the wrong text.
            start = end = None

        result.facts.append(CandidateFact(
            subject_key=subj,
            predicate=predicate,
            object_key=obj,
            object_literal=item.object_literal if not has_object_entity else None,
            evidence=item.span_text,
            confidence=item.confidence,
            locator=item.locator,
            char_start=start,
            char_end=end,
        ))

    @staticmethod
    def _entity_key(keys: dict[tuple[str, str], str], result: ExtractionResult,
                    surface: str, entity_type: str, extracted_keys: dict,
                    char_start: Optional[int],
                    char_end: Optional[int]) -> str:
        """The candidate key for (surface, type), creating the entity the
        first time it is seen in this document. Later mentions of the same
        pair reuse the key, so one thing becomes one mention no matter how
        many facts touch it."""
        identity = (surface.casefold(), entity_type)
        existing = keys.get(identity)
        if existing is not None:
            return existing
        key = f"p{len(keys)}"
        keys[identity] = key
        span_ok = (char_start is not None and char_end is not None
                   and char_end > char_start)
        result.entities.append(CandidateEntity(
            key=key, surface_text=surface, entity_type=entity_type,
            extracted_keys=extracted_keys or {},
            char_start=char_start if span_ok else None,
            char_end=char_end if span_ok else None))
        return key

    def _quarantine(self, unit: ExtractionUnit, reason: str, detail: str,
                    raw_output: dict[str, Any]) -> QuarantinedExtraction:
        """Same envelope the LLM and structured paths build, so a
        plugin's rejects land in the same review queue, readable the same
        way, with no special case for where they came from."""
        return QuarantinedExtraction(
            tenant_id=unit.document.tenant_id,
            document_id=unit.document.id,
            source_chunk_id=unit.chunk.id if unit.chunk else None,
            reason=reason, detail=detail, raw_output=raw_output,
            extractor=self.extractor, extractor_version=self.version,
            ontology_version=self.binding.version)
