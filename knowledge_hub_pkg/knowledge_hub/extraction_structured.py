"""StructuredMapStrategy — the SoR/spreadsheet extractor. No LLM, ever.

P3 routes structured-track documents around chunking (`no_chunks`); their
facts come straight off fields/rows, deterministically. The map from columns
to ontology predicates is MANIFEST data (source_registry.config or a
per-item native_metadata declaration, key 'structured_map'), because a
system-of-record's meaning is something its owner declares, not something a
model guesses:

    {"entity_type": "Asset",              # what each row IS
     "subject_column": "asset_name",      # the row's surface form
     "key_columns": {"asset_id": "asset_id"},   # column -> extracted_keys name
     "columns": {
        "site":   {"predicate": "part_of", "object_entity_type": "Location"},
        "status": {"predicate": "has_status"}    # literal-valued
     }}

Provenance locator = the cell ({"row": <1-based data row>, "col": <name>});
facts are grounded BY CONSTRUCTION (the flow stamps grounding='construction'
— there is no model output to verify against a span).

The same conformance rules as the LLM path apply, deterministically: mapped
predicates are normalized through the ontology's alias data; a column mapped
to an unbound predicate or unbound object entity type quarantines ONCE per
document (not once per row — the mapping is wrong, not the rows), with the
offending mapping as the raw output. A document with no structured_map at
all stages row mentions only (entity observations are still valuable; facts
would be guesses).
"""
from __future__ import annotations

import csv
import io
import logging
from typing import Any, ClassVar, Optional

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

_DELIMITERS = ",;\t|"


class StructuredMapStrategy(ExtractionStrategy):
    extractor: ClassVar[str] = "structured_map"
    version = "0.1.0"  # code version: the 'model' here is this module

    def __init__(self, binding: OntologyBinding):
        self.binding = binding

    # -------------------------------------------------- ExtractionStrategy --
    def extract(self, unit: ExtractionUnit) -> ExtractionResult:
        if unit.payload is None:
            raise ExtractionError(
                unit.document.tenant_id, unit.document.id, None,
                "structured_map needs the landed payload bytes")
        header, rows = self._parse_table(unit)
        result = ExtractionResult(stats=ExtractionStats())

        mapping = unit.config.get("structured_map") or {}
        if not mapping:
            logger.warning(
                "document id=%s has no structured_map declaration; staging "
                "row mentions only (no facts without a declared meaning)",
                unit.document.id)
        entity_type = mapping.get("entity_type")
        subject_column = mapping.get("subject_column") or (header[0] if header
                                                           else None)
        if subject_column not in header:
            raise ExtractionError(
                unit.document.tenant_id, unit.document.id, None,
                f"subject column {subject_column!r} not in header {header}")
        if entity_type is None:
            entity_type = "Asset"  # only used when a mapping exists w/o type
        if mapping and not self.binding.is_entity_type(entity_type):
            result.quarantined.append(self._quarantine(
                unit, "unbound_entity_type", entity_type,
                {"structured_map": mapping}))
            return result

        columns = self._conform_columns(unit, mapping, header, result)
        key_columns: dict[str, str] = mapping.get("key_columns") or {}

        col_ix = {name: i for i, name in enumerate(header)}
        for row_no, row in enumerate(rows, start=1):
            surface = self._cell(row, col_ix, subject_column)
            if not surface:
                continue  # a row without a subject surface asserts nothing
            if not mapping:
                # No declared meaning: entity observation only.
                result.entities.append(CandidateEntity(
                    key=f"r{row_no}", surface_text=surface,
                    entity_type=entity_type))
                continue
            subject_key = f"r{row_no}"
            keys = {name: self._cell(row, col_ix, col)
                    for col, name in key_columns.items()
                    if self._cell(row, col_ix, col)}
            result.entities.append(CandidateEntity(
                key=subject_key, surface_text=surface,
                entity_type=entity_type, extracted_keys=keys))
            for col, spec in columns.items():
                value = self._cell(row, col_ix, col)
                if not value:
                    continue
                locator = {"row": row_no, "col": col}
                if spec["object_entity_type"] is not None:
                    object_key = f"r{row_no}.{col}"
                    result.entities.append(CandidateEntity(
                        key=object_key, surface_text=value,
                        entity_type=spec["object_entity_type"]))
                    subj, obj = subject_key, object_key
                    if spec["swap"]:
                        subj, obj = obj, subj
                    result.facts.append(CandidateFact(
                        subject_key=subj, predicate=spec["predicate"],
                        object_key=obj, confidence=1.0, locator=locator))
                else:
                    result.facts.append(CandidateFact(
                        subject_key=subject_key, predicate=spec["predicate"],
                        object_literal=value, confidence=1.0,
                        locator=locator))
        return result

    # ----------------------------------------------------------- internals --
    def _conform_columns(self, unit: ExtractionUnit, mapping: dict,
                         header: list[str],
                         result: ExtractionResult) -> dict[str, dict]:
        """Validate the declared column map once per document: predicate
        normalized through ontology alias data, object entity type bound.
        A bad column quarantines the MAPPING (once), not every row."""
        conformed: dict[str, dict] = {}
        for col, spec in (mapping.get("columns") or {}).items():
            raw = {"column": col, "spec": spec}
            if col not in header:
                result.quarantined.append(self._quarantine(
                    unit, "validation_failure",
                    f"mapped column {col!r} not in header", raw))
                continue
            normalized = self.binding.normalize_predicate(spec.get("predicate", ""))
            if normalized is None:
                result.quarantined.append(self._quarantine(
                    unit, "unbound_predicate", spec.get("predicate", ""), raw))
                continue
            object_entity_type = spec.get("object_entity_type")
            if (object_entity_type is not None
                    and not self.binding.is_entity_type(object_entity_type)):
                result.quarantined.append(self._quarantine(
                    unit, "unbound_entity_type", object_entity_type, raw))
                continue
            predicate, swap = normalized
            conformed[col] = {"predicate": predicate, "swap": swap,
                              "object_entity_type": object_entity_type}
        return conformed

    def _parse_table(self, unit: ExtractionUnit) -> tuple[list[str],
                                                          list[list[str]]]:
        try:
            text = unit.payload.decode("utf-8-sig")
        except UnicodeDecodeError as e:
            raise ExtractionError(
                unit.document.tenant_id, unit.document.id, None,
                f"payload is not UTF-8 text: {e}") from e
        sample = text[:4096]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=_DELIMITERS)
        except csv.Error:
            dialect = csv.excel  # comma
        reader = csv.reader(io.StringIO(text), dialect)
        table = [row for row in reader if any(cell.strip() for cell in row)]
        if not table:
            raise ExtractionError(unit.document.tenant_id, unit.document.id,
                                  None, "empty table")
        header = [h.strip() for h in table[0]]
        return header, table[1:]

    @staticmethod
    def _cell(row: list[str], col_ix: dict[str, int],
              col: Optional[str]) -> str:
        ix = col_ix.get(col or "")
        if ix is None or ix >= len(row):
            return ""
        return row[ix].strip()

    def _quarantine(self, unit: ExtractionUnit, reason: str, detail: str,
                    raw_output: dict[str, Any]) -> QuarantinedExtraction:
        return QuarantinedExtraction(
            tenant_id=unit.document.tenant_id,
            document_id=unit.document.id,
            reason=reason, detail=detail, raw_output=raw_output,
            extractor=self.extractor, extractor_version=self.version,
            ontology_version=self.binding.version)
