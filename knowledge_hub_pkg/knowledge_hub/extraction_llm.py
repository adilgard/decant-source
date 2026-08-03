"""LLMJointExtractionStrategy — the prose/SOP/comms extractor (qwen3.6 pilot).

One joint pass per parent chunk emitting (subject, predicate, object,
evidence_span, confidence) tuples at temperature 0, structure-constrained by
the ontology binding's JSON schema (Ollama `format=`, `think:false`). Joint
means entities and facts come out of the SAME call, with the document's
carried-forward entity digest in the prompt so coreference ("she", "the
supervisor") resolves in-pass to already-staged mentions — no separate coref
step.

Determinism posture (§8.2c/f/g), division of labor:
  * the SCHEMA makes malformed output impossible at decode time;
  * this module's deterministic code validates MEANING post-hoc (Pydantic
    shape, key references, ontology vocabulary via the binding);
  * repair is capped at ONE retry; won't-validate-after-one -> the whole
    unit's raw output is quarantined (validation_failure), never dropped;
  * predicates are normalized toward the ontology only where the ontology's
    own alias data says the mapping is unambiguous ('owned by' -> owns, with
    a subject/object swap); genuine unknowns -> quarantined
    (unbound_predicate) WITH the raw fact — the signal that grows the
    ontology. Same for unknown entity types.

Extraction captures, it does not canonicalize: mentions carry raw
surface_text plus only confidently-extracted keys (email/domain/tax_id by
deterministic regex); normalization for matching is the resolver's job.

NOTE on quality: this module makes the machinery correct and observable.
Whether qwen3.6 extracts the RIGHT facts is a benchmark question (Axes
A/B/C), answered later with the observability rows this stage persists —
do not read green tests as good extraction.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, ClassVar, Optional

import ollama
from pydantic import BaseModel, Field, ValidationError

from knowledge_hub.config import settings
from knowledge_hub.interfaces import (
    CandidateEntity,
    CandidateFact,
    ExtractionError,
    ExtractionResult,
    ExtractionStats,
    ExtractionStrategy,
    ExtractionUnit,
    OntologyBinding,
)
from knowledge_hub.models import QuarantinedExtraction

logger = logging.getLogger(__name__)

# Generous context so a 2048-token parent + vocabulary + digest never
# truncates silently (Ollama's default num_ctx is small).
NUM_CTX = 8192
MAX_DIGEST_ENTRIES = 15  # most-referenced entities stay sticky

# Bumped whenever the prompt/schema contract changes; part of
# extractor_version so the idempotency ledger re-extracts under the new
# contract instead of replaying stale runs.
PROMPT_VERSION = 2

_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
_DOMAIN = re.compile(r"\b(?:[A-Za-z0-9-]+\.)+(?:com|org|net|io|gov|edu)\b",
                     re.IGNORECASE)
_TAX_ID = re.compile(r"\b\d{2}-\d{7}\b")  # US EIN shape


def extracted_keys_for(surface: str) -> dict[str, Any]:
    """Deterministic identifier harvest from a surface form. Only patterns
    confident enough to hand the resolver as keys; everything else stays raw
    surface text."""
    keys: dict[str, Any] = {}
    email = _EMAIL.search(surface)
    if email:
        keys["email"] = email.group(0).lower()
        keys["domain"] = email.group(1).lower()
    else:
        domain = _DOMAIN.search(surface)
        if domain:
            keys["domain"] = domain.group(0).lower()
    tax_id = _TAX_ID.search(surface)
    if tax_id:
        keys["tax_id"] = tax_id.group(0)
    return keys


# --------------------------------------------------------------------------
# The model's output contract (shape only; vocabulary is validated separately
# so off-ontology attempts survive to the quarantine).
# --------------------------------------------------------------------------
class _OutEntity(BaseModel):
    key: str
    name: str
    type: str


class _OutFact(BaseModel):
    subject: str
    predicate: str
    object: Optional[str] = None
    object_literal: Optional[str] = None
    evidence: str
    confidence: float = Field(ge=0.0, le=1.0)


class _Output(BaseModel):
    entities: list[_OutEntity]
    facts: list[_OutFact]


class LLMJointExtractionStrategy(ExtractionStrategy):
    extractor: ClassVar[str] = "llm_joint"

    def __init__(self, binding: OntologyBinding,
                 host: Optional[str] = None, model: Optional[str] = None,
                 client: Optional[ollama.Client] = None):
        self.binding = binding
        self.model = model or settings.extraction_model
        self._client = client or ollama.Client(host=host or settings.ollama_host)
        self._version: Optional[str] = None

    @property
    def version(self) -> str:
        """model@digest of the SERVED weights plus the prompt revision
        (lazily resolved, cached) — the honest extractor_version stamp the
        envelope requires: same weights under a changed prompt contract are
        a DIFFERENT extractor for idempotency and benchmarking purposes."""
        if self._version is None:
            self._version = (f"{self.model}@{self._resolve_digest()}"
                             f"/p{PROMPT_VERSION}")
        return self._version

    # -------------------------------------------------- ExtractionStrategy --
    def extract(self, unit: ExtractionUnit) -> ExtractionResult:
        if unit.chunk is None:
            raise ExtractionError(
                unit.document.tenant_id, unit.document.id, None,
                "llm_joint extracts parent chunks; structured documents "
                "route to structured_map")

        started = time.monotonic()
        output, stats, raw_text = self._call_with_repair(unit)
        stats.wall_ms = int((time.monotonic() - started) * 1000)
        if output is None:  # would not validate after the one capped repair
            return ExtractionResult(
                quarantined=[self._quarantine(
                    unit, "validation_failure",
                    "output failed schema/shape validation after 1 repair",
                    {"raw": raw_text})],
                stats=stats)
        return self._conform(unit, output, stats)

    # ------------------------------------------------------------ LLM call --
    def _call_with_repair(self, unit: ExtractionUnit) -> tuple[
            Optional[_Output], ExtractionStats, str]:
        """One extraction call plus AT MOST one repair round-trip. Transport
        failures raise ExtractionError (the queue nacks and redelivers);
        validation failures after repair return None (quarantine)."""
        stats = ExtractionStats()
        messages = [
            {"role": "system", "content": self._system_prompt()},
            {"role": "user", "content": self._user_prompt(unit)},
        ]
        raw_text = ""
        for attempt in range(2):
            try:
                response = self._client.chat(
                    model=self.model, messages=messages, think=False,
                    format=self.binding.output_schema(unit.document.data_track),
                    options={"temperature": 0, "num_ctx": NUM_CTX})
            except Exception as e:
                raise ExtractionError(
                    unit.document.tenant_id, unit.document.id, unit.chunk.id,
                    f"ollama chat failed (model {self.model!r}): "
                    f"{type(e).__name__}: {e}") from e
            stats.prompt_tokens = (stats.prompt_tokens or 0) + \
                (response.get("prompt_eval_count") or 0)
            stats.output_tokens = (stats.output_tokens or 0) + \
                (response.get("eval_count") or 0)
            raw_text = response.message.content or ""
            try:
                return _Output.model_validate(json.loads(raw_text)), stats, raw_text
            except (json.JSONDecodeError, ValidationError) as e:
                if attempt == 1:
                    break
                stats.repairs += 1
                logger.warning("extraction output failed validation for "
                               "chunk id=%s; one repair attempt: %s",
                               unit.chunk.id, e)
                messages += [
                    {"role": "assistant", "content": raw_text},
                    {"role": "user", "content":
                        f"Your output failed validation: {e}. Return the "
                        "corrected JSON object only, same schema."},
                ]
        return None, stats, raw_text

    def _system_prompt(self) -> str:
        return (
            "You are a precise information-extraction engine for a knowledge "
            "base. You emit only JSON conforming to the given schema. You "
            "extract only what the text explicitly asserts — never invent, "
            "never embellish.")

    def _user_prompt(self, unit: ExtractionUnit) -> str:
        digest = unit.digest[:MAX_DIGEST_ENTRIES]
        if digest:
            known = "\n".join(
                f'  {d.key}: "{d.surface_text}" ({d.entity_type})'
                for d in digest)
        else:
            known = "  (none yet)"
        return f"""{self.binding.prompt_vocabulary()}

Known entities from earlier in this document — reuse these EXACT keys when \
the passage refers to them, including via pronouns (she/he/it/they) or \
descriptions ("the supervisor", "this document"):
{known}

Extract entities and facts from the passage below.

Rules:
- entities: list ONLY entities not already known above. Give each a short \
new key (n1, n2, ...) and its surface text exactly as written. Never create \
an entity for a pronoun — pronouns refer to known entities.
- facts: only assertions the passage explicitly states. Direction matters: \
subject --predicate--> object.
- subject and object must be KEYS — from the known-entities list or from \
your entities array. Never put a name or free text in subject/object; \
declare the entity first and use its key.
- Every fact needs an object: either object (an entity key, with \
object_literal=null) or object_literal (the value as text — a duration, \
count, date, status — with object=null). If you cannot fill either, do not \
emit the fact.
- Prefer the listed predicates. If the passage clearly asserts a \
relationship none of them expresses, emit the best snake_case name for it \
instead of forcing a wrong listed predicate.
- evidence: copy the exact sentence or clause asserting the fact, verbatim.
- confidence: 1.0 only when the passage states it outright.

Passage:
\"\"\"
{unit.text}
\"\"\""""

    # -------------------------------------------------------- conformance --
    def _conform(self, unit: ExtractionUnit, output: _Output,
                 stats: ExtractionStats) -> ExtractionResult:
        """Deterministic meaning-validation: ontology vocabulary, key
        references, alias normalization, intra-unit dedup. Anything unbound
        or unresolvable goes to quarantined — never silently into facts."""
        result = ExtractionResult(stats=stats)
        digest_keys = {d.key for d in unit.digest}
        digest_by_name = {(d.surface_text.casefold(), d.entity_type): d.key
                          for d in unit.digest}

        # --- entities: split known (digest) from new, quarantine unbound ----
        keymap: dict[str, str] = {}   # model key -> digest key or local key
        bad_keys: set[str] = set()    # keys whose entity was quarantined
        for ent in output.entities:
            if ent.key in keymap:
                continue  # duplicate declaration; first wins
            if ent.key in digest_keys:
                keymap[ent.key] = ent.key  # re-declared a known entity
                continue
            known = digest_by_name.get((ent.name.casefold(), ent.type))
            if known:  # same surface+type as a digest entity, new key
                keymap[ent.key] = known
                continue
            if not self.binding.is_entity_type(ent.type):
                result.quarantined.append(self._quarantine(
                    unit, "unbound_entity_type", ent.type,
                    ent.model_dump()))
                bad_keys.add(ent.key)
                continue
            keymap[ent.key] = ent.key
            result.entities.append(CandidateEntity(
                key=ent.key, surface_text=ent.name, entity_type=ent.type,
                extracted_keys=extracted_keys_for(ent.name)))

        # --- facts: resolve refs, normalize predicate, dedup ---------------
        valid_keys = digest_keys | {e.key for e in result.entities}
        seen: dict[tuple, CandidateFact] = {}
        for fact in output.facts:
            raw = fact.model_dump()
            if fact.subject in bad_keys or (fact.object or "") in bad_keys:
                result.quarantined.append(self._quarantine(
                    unit, "unbound_entity_type",
                    "fact references a quarantined entity", raw))
                continue
            subject = keymap.get(fact.subject, fact.subject)
            obj = keymap.get(fact.object, fact.object) if fact.object else None
            if subject not in valid_keys or (obj is not None
                                             and obj not in valid_keys):
                result.quarantined.append(self._quarantine(
                    unit, "validation_failure",
                    "fact references an undeclared entity key", raw))
                continue
            if obj is None and fact.object_literal is None:
                result.quarantined.append(self._quarantine(
                    unit, "validation_failure",
                    "fact has neither object entity nor literal", raw))
                continue
            normalized = self.binding.normalize_predicate(fact.predicate)
            if normalized is None:
                result.quarantined.append(self._quarantine(
                    unit, "unbound_predicate", fact.predicate, raw))
                continue
            predicate, swap = normalized
            if swap:
                if obj is None:  # can't swap subject with a literal
                    result.quarantined.append(self._quarantine(
                        unit, "validation_failure",
                        f"predicate {fact.predicate!r} normalizes to "
                        f"{predicate!r} with swapped arguments, but the "
                        "object is a literal", raw))
                    continue
                subject, obj = obj, subject
            if obj is not None and obj == subject:
                # Deterministically junk: X --rel--> X asserts nothing
                # (observed on SOP-014: 'certificate part_of certificate').
                result.quarantined.append(self._quarantine(
                    unit, "validation_failure",
                    "self-referential fact (subject == object)", raw))
                continue
            candidate = CandidateFact(
                subject_key=subject, predicate=predicate, object_key=obj,
                object_literal=fact.object_literal if obj is None else None,
                evidence=fact.evidence,
                confidence=min(max(fact.confidence, 0.0), 1.0))
            # Intra-unit dedup only (cross-unit sameness is the resolver's
            # job): identical assertions within one parent collapse, keeping
            # the highest confidence.
            key = (subject, predicate, obj,
                   (candidate.object_literal or "").casefold())
            if key in seen:
                seen[key].confidence = max(seen[key].confidence,
                                           candidate.confidence)
            else:
                seen[key] = candidate
                result.facts.append(candidate)
        return result

    # ----------------------------------------------------------- internals --
    def _quarantine(self, unit: ExtractionUnit, reason: str, detail: str,
                    raw_output: dict[str, Any]) -> QuarantinedExtraction:
        return QuarantinedExtraction(
            tenant_id=unit.document.tenant_id,
            document_id=unit.document.id,
            source_chunk_id=unit.chunk.id if unit.chunk else None,
            reason=reason, detail=detail, raw_output=raw_output,
            extractor=self.extractor, extractor_version=self.version,
            ontology_version=self.binding.version)

    def _resolve_digest(self) -> str:
        try:
            for entry in self._client.list().models:
                name = getattr(entry, "model", "") or ""
                if name == self.model or name.startswith(f"{self.model}:"):
                    digest = getattr(entry, "digest", "") or ""
                    if digest:
                        return digest[:12]
        except Exception as e:
            logger.warning("could not resolve %r model digest: %s",
                           self.model, e)
        return "unknown"
