"""PostgresOntologyBinding — the vocabulary, generated from ontology DATA.

Everything here derives from ONE ontology_versions row's definition JSONB:
the output schema handed to the extractor (Ollama `format=`), the
vocabulary checks, the prompt's vocabulary block (with the ontology's
per-type examples), and the surface-variant alias map. Swap the ontology by
inserting a new row and rebuilding the binding — no code change (the swap
target the baseline schema §0 promised).

The determinism guardrail, encoded (§8.2c/f/g):
  * The schema constrains STRUCTURE (malformed JSON impossible at decode
    time) but deliberately does NOT enum-constrain predicates/entity types —
    an off-ontology attempt must survive to the quarantine, because what the
    model keeps trying to say and can't is exactly the signal that grows the
    ontology. Hard-constraining the enum would blind us to it.
  * Meaning is validated by deterministic code: is_entity_type/is_predicate
    are set lookups; normalize_predicate maps only the UNAMBIGUOUS surface
    variants the ontology's own alias data declares (e.g. 'owned by' ->
    owns, swapping subject/object); genuine unknowns return None and
    quarantine.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from knowledge_hub.factstore_pg import PostgresFactStore
from knowledge_hub.interfaces import OntologyBinding
from knowledge_hub.models import DEFAULT_TENANT, PROSE_TRACK, STRUCTURED_TRACK

# Canonical predicate surface form: lowercase, single underscores.
_CANON = re.compile(r"[\s\-]+")


def _canon(raw: str) -> str:
    return _CANON.sub("_", raw.strip().lower())


class PostgresOntologyBinding(OntologyBinding):
    def __init__(self, store: PostgresFactStore,
                 version: Optional[str] = None,
                 tenant_id: str = DEFAULT_TENANT):
        # ontology_versions is global (no tenant column); the tenant here
        # only picks the connection.
        self.version, self._definition = store.get_ontology_definition(
            tenant_id, version)
        self._entity_types: set[str] = set(
            self._definition.get("entity_types", []))
        self._predicates: set[str] = set(
            self._definition.get("predicates", []))
        # Alias entries are either "canonical" or {"predicate":.., "swap":..}.
        self._aliases: dict[str, tuple[str, bool]] = {}
        for alias, target in (self._definition.get("predicate_aliases")
                              or {}).items():
            if isinstance(target, str):
                self._aliases[_canon(alias)] = (target, False)
            else:
                self._aliases[_canon(alias)] = (
                    target["predicate"], bool(target.get("swap", False)))

    # ------------------------------------------------------ OntologyBinding --
    def is_entity_type(self, entity_type: str) -> bool:
        return entity_type in self._entity_types

    def is_predicate(self, predicate: str) -> bool:
        return predicate in self._predicates

    def normalize_predicate(self, raw: str) -> Optional[tuple[str, bool]]:
        canon = _canon(raw)
        if canon in self._predicates:
            return canon, False
        if canon in self._aliases:
            target, swap = self._aliases[canon]
            if target in self._predicates:  # alias data can't invent vocabulary
                return target, swap
        return None

    def output_schema(self, data_track: str) -> dict[str, Any]:
        """Structure-only JSON schema for the extractor's output.

        Both tracks share the same joint shape (entities + facts); the
        structured track never reaches an LLM in the pilot, but its
        deterministic output is validated against the same contract."""
        if data_track not in (PROSE_TRACK, STRUCTURED_TRACK):
            raise ValueError(f"unknown data_track {data_track!r}")
        types = ", ".join(sorted(self._entity_types))
        preds = ", ".join(sorted(self._predicates))
        return {
            "type": "object",
            "properties": {
                "entities": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "key": {
                                "type": "string",
                                "description": "Short local key facts use to "
                                               "reference this entity.",
                            },
                            "name": {
                                "type": "string",
                                "description": "The entity's surface text "
                                               "exactly as written.",
                            },
                            "type": {
                                "type": "string",
                                "description": f"One of: {types}.",
                            },
                        },
                        "required": ["key", "name", "type"],
                    },
                },
                "facts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "subject": {
                                "type": "string",
                                "description": "Key of the subject entity.",
                            },
                            "predicate": {
                                "type": "string",
                                "description": f"Preferably one of: {preds}.",
                            },
                            "object": {
                                "type": ["string", "null"],
                                "description": "Key of the object entity, or "
                                               "null for a literal-valued fact.",
                            },
                            "object_literal": {
                                "type": ["string", "null"],
                                "description": "The literal value when there "
                                               "is no object entity.",
                            },
                            "evidence": {
                                "type": "string",
                                "description": "The EXACT sentence/clause "
                                               "from the text asserting this "
                                               "fact, quoted verbatim.",
                            },
                            "confidence": {
                                "type": "number",
                                "description": "0.0-1.0.",
                            },
                        },
                        # object/object_literal are REQUIRED (nullable):
                        # constrained decoding otherwise lets the model
                        # simply omit them, and an objectless fact is
                        # unstageable — observed on SOP-014, quarantined en
                        # masse. Requiring the keys forces an explicit
                        # choice; validation still enforces one is non-null.
                        "required": ["subject", "predicate", "object",
                                     "object_literal", "evidence",
                                     "confidence"],
                    },
                },
            },
            "required": ["entities", "facts"],
        }

    def prompt_vocabulary(self) -> str:
        """The vocabulary block for the extraction prompt, examples included
        (examples live on the ontology row, not in code)."""
        examples = self._definition.get("examples") or {}
        type_ex = examples.get("entity_types") or {}
        pred_ex = examples.get("predicates") or {}
        lines = [f"Ontology version: {self.version}", "", "Entity types:"]
        for t in sorted(self._entity_types):
            ex = type_ex.get(t)
            lines.append(f"  - {t}" + (f" (e.g. {', '.join(ex)})" if ex else ""))
        lines.append("")
        lines.append("Predicates (subject --predicate--> object):")
        for p in sorted(self._predicates):
            ex = pred_ex.get(p)
            lines.append(f"  - {p}" + (f" — {'; '.join(ex)}" if ex else ""))
        return "\n".join(lines)
