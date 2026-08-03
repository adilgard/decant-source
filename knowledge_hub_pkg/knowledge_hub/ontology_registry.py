"""Ontology import + selection — deterministic, operator-owned (d.s Stage 1).

An ontology SET is one JSON document: a version string plus the two flat
allowlists (entity_types, predicates), optionally with prompt examples and a
predicate alias map — exactly the definition JSONB a PostgresOntologyBinding
consumes. The ontology is an ALLOWLIST, NOT A SCHEMA: nothing structural
(attribute schemas, hierarchies) is part of this contract, on purpose.

Two forms, one truth:

  * PORTABLE form — `<version>.json` files in a git-tracked folder
    (settings.ontology_dir, `ontologies/` in the project tree). This is
    what travels between machines and lives in version control.
  * LOADED form — a row in ontology_versions. This is what the binding,
    the extractor, and the idempotency ledger read.

Import writes both (file first, then row); both writes are idempotent on
identical content, and a version string is IMMUTABLE — re-importing the
same version with different content is a hard error, never an overwrite.

Validation is plain deterministic code with one specific error per failure
(OntologyValidationError). No LLM is anywhere in this path — imports and
selection are control-plane operations (§ the four laws: the LLM sits at
the tip of per-document extraction only).

Selection is SEPARATE from import: importing a version is inert;
set_active_ontology (migration 011's single-row pointer, written via the
audited select_ontology operator action) is what changes which vocabulary
FUTURE ingests extract against. Existing facts keep the ontology_version
that produced them — true provenance, never rewritten.

Out of scope, named so it is not silently assumed (per the build prompt):
  * watched folders / auto-import on file change — importing is explicit;
  * structural ontology fields beyond the two allowlists;
  * purging superseded facts (retention is a later build).
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict

from knowledge_hub.config import settings
from knowledge_hub.ontology import _canon as canon_predicate

# Version strings double as filenames in the portable folder, so the charset
# is the safe-filename subset: starts alphanumeric, then letters / digits /
# dot / underscore / hyphen, max 64.
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# Every key an ontology set may carry. Anything else is a typo or a schema
# fantasy — rejected, named.
_ALLOWED_KEYS = {"version", "entity_types", "predicates", "examples",
                 "predicate_aliases", "notes"}
_ALLOWED_EXAMPLE_KEYS = {"entity_types", "predicates"}
_ALLOWED_ALIAS_KEYS = {"predicate", "swap"}


class OntologyValidationError(ValueError):
    """One specific reason this ontology set is not importable."""


class OntologySet(BaseModel):
    """A validated ontology set: the version plus the definition JSONB in
    exactly the shape ontology_versions.definition / the binding expect."""
    model_config = ConfigDict(extra="forbid")

    version: str
    definition: dict[str, Any]
    notes: Optional[str] = None


# ------------------------------------------------------------- validation --
def _require_name_list(value: Any, field: str) -> list[str]:
    """A non-empty, all-string, no-duplicate, no-blank list — the shape both
    allowlists share. Errors name the field and the offending values."""
    if not isinstance(value, list):
        raise OntologyValidationError(
            f"{field} must be a JSON array, got {type(value).__name__}")
    if not value:
        raise OntologyValidationError(f"{field} must not be empty")
    bad_type = [repr(v) for v in value if not isinstance(v, str)]
    if bad_type:
        raise OntologyValidationError(
            f"{field} must contain only strings; got {', '.join(bad_type)}")
    blank = [repr(v) for v in value if not v.strip()]
    if blank:
        raise OntologyValidationError(
            f"{field} must not contain blank entries; got {', '.join(blank)}")
    seen: set[str] = set()
    dupes = sorted({v for v in value if v in seen or seen.add(v)})
    if dupes:
        raise OntologyValidationError(
            f"{field} contains duplicates: {dupes} — de-duplicate the file "
            f"and re-import")
    return list(value)


def _validate_examples(examples: Any, entity_types: set[str],
                       predicates: set[str]) -> dict[str, Any]:
    if not isinstance(examples, dict):
        raise OntologyValidationError(
            f"examples must be a JSON object, got {type(examples).__name__}")
    unknown = sorted(set(examples) - _ALLOWED_EXAMPLE_KEYS)
    if unknown:
        raise OntologyValidationError(
            f"examples has unknown key(s) {unknown} — allowed: "
            f"{sorted(_ALLOWED_EXAMPLE_KEYS)}")
    for group, declared in (("entity_types", entity_types),
                            ("predicates", predicates)):
        block = examples.get(group)
        if block is None:
            continue
        if not isinstance(block, dict):
            raise OntologyValidationError(
                f"examples.{group} must be a JSON object mapping a declared "
                f"name to a list of example strings")
        undeclared = sorted(set(block) - declared)
        if undeclared:
            raise OntologyValidationError(
                f"examples.{group} names {undeclared}, which are not "
                f"declared in {group}")
        for name, ex in block.items():
            if (not isinstance(ex, list) or not ex
                    or not all(isinstance(e, str) and e.strip() for e in ex)):
                raise OntologyValidationError(
                    f"examples.{group}[{name!r}] must be a non-empty list "
                    f"of non-empty strings")
    return examples


def _validate_aliases(aliases: Any, predicates: set[str]) -> dict[str, Any]:
    if not isinstance(aliases, dict):
        raise OntologyValidationError(
            f"predicate_aliases must be a JSON object, got "
            f"{type(aliases).__name__}")
    for alias, target in aliases.items():
        if not isinstance(alias, str) or not alias.strip():
            raise OntologyValidationError(
                f"predicate_aliases key {alias!r} must be a non-empty string")
        # The binding canonicalizes the RAW surface form before checking
        # membership, so an alias whose canonical form IS a declared
        # predicate would never be consulted — that's a modeling mistake,
        # not a harmless redundancy. Same canon rule as the binding's.
        if canon_predicate(alias) in predicates:
            raise OntologyValidationError(
                f"predicate_aliases key {alias!r} collides with a declared "
                f"predicate — an alias must be a surface VARIANT, not the "
                f"canonical form")
        if isinstance(target, str):
            target_pred = target
        elif isinstance(target, dict):
            unknown = sorted(set(target) - _ALLOWED_ALIAS_KEYS)
            if unknown:
                raise OntologyValidationError(
                    f"predicate_aliases[{alias!r}] has unknown key(s) "
                    f"{unknown} — allowed: {sorted(_ALLOWED_ALIAS_KEYS)}")
            if "predicate" not in target:
                raise OntologyValidationError(
                    f"predicate_aliases[{alias!r}] object form requires a "
                    f"'predicate' key")
            if not isinstance(target.get("swap", False), bool):
                raise OntologyValidationError(
                    f"predicate_aliases[{alias!r}].swap must be true/false")
            target_pred = target["predicate"]
        else:
            raise OntologyValidationError(
                f"predicate_aliases[{alias!r}] must be a predicate string "
                f"or {{'predicate': ..., 'swap': true|false}}")
        if target_pred not in predicates:
            raise OntologyValidationError(
                f"predicate_aliases[{alias!r}] targets {target_pred!r}, "
                f"which is not a declared predicate — alias data can't "
                f"invent vocabulary")
    return aliases


def validate_ontology_set(data: Any) -> OntologySet:
    """The import gate. Deterministic; every rejection names its reason.
    Returns the set with `definition` in exactly the JSONB shape the
    binding consumes (allowlists + optional examples/aliases; notes ride
    the table column, not the definition)."""
    if not isinstance(data, dict):
        raise OntologyValidationError(
            f"an ontology set must be a JSON object, got "
            f"{type(data).__name__}")
    unknown = sorted(set(data) - _ALLOWED_KEYS)
    if unknown:
        raise OntologyValidationError(
            f"unknown key(s) {unknown} — an ontology set carries only "
            f"{sorted(_ALLOWED_KEYS)} (the ontology is an allowlist, not a "
            f"schema; structural fields are out of scope by design)")

    version = data.get("version")
    if not isinstance(version, str) or not version.strip():
        raise OntologyValidationError(
            "version must be a non-empty string")
    if not _VERSION.match(version):
        raise OntologyValidationError(
            f"version {version!r} must match {_VERSION.pattern} — it is "
            f"also the portable filename")

    missing = [f for f in ("entity_types", "predicates") if f not in data]
    if missing:
        raise OntologyValidationError(
            f"missing required key(s): {missing} — both allowlists must be "
            f"present")
    entity_types = _require_name_list(data["entity_types"], "entity_types")
    predicates = _require_name_list(data["predicates"], "predicates")

    non_canon = sorted(p for p in predicates if p != canon_predicate(p))
    if non_canon:
        raise OntologyValidationError(
            f"predicates must be lowercase_underscore form (the form the "
            f"binding matches against); fix: "
            f"{ {p: canon_predicate(p) for p in non_canon} }")

    notes = data.get("notes")
    if notes is not None and not isinstance(notes, str):
        raise OntologyValidationError("notes must be a string when present")

    definition: dict[str, Any] = {"entity_types": entity_types,
                                  "predicates": predicates}
    if "examples" in data:
        definition["examples"] = _validate_examples(
            data["examples"], set(entity_types), set(predicates))
    if "predicate_aliases" in data:
        definition["predicate_aliases"] = _validate_aliases(
            data["predicate_aliases"], set(predicates))
    return OntologySet(version=version, definition=definition, notes=notes)


# ---------------------------------------------------------- portable files --
def ontology_dir() -> Path:
    """The git-tracked portable folder. Relative settings resolve against
    the working directory — the deployment home under khctl, the infra
    root on the dev bench — matching the house pattern (tokenizer path,
    usage log)."""
    return Path(settings.ontology_dir)


def save_ontology_file(onto: OntologySet,
                       folder: Optional[Path] = None) -> Path:
    """Write the portable form, atomically (tmp + os.replace — a crash
    mid-write never leaves a half file). Idempotent on identical content;
    a DIFFERENT file already holding this version is an error, mirroring
    the immutability rule the loaded form enforces."""
    folder = folder or ontology_dir()
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{onto.version}.json"
    payload = dict(onto.definition)
    payload = {"version": onto.version, **payload}
    if onto.notes is not None:
        payload["notes"] = onto.notes
    rendered = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    if target.exists():
        try:
            existing = validate_ontology_set(
                json.loads(target.read_text(encoding="utf-8")))
        except (OntologyValidationError, ValueError):
            raise OntologyValidationError(
                f"{target} exists but does not parse as a valid ontology "
                f"set — refusing to overwrite; move it aside and re-import")
        if (existing.definition == onto.definition
                and existing.notes == onto.notes):
            return target  # identical content already on disk
        raise OntologyValidationError(
            f"{target} already holds version {onto.version!r} with "
            f"DIFFERENT content — versions are immutable; publish a new "
            f"version string instead")
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(rendered, encoding="utf-8")
    os.replace(tmp, target)
    return target


def load_ontology_file(path: Path | str) -> OntologySet:
    """Parse + validate one portable file (the drop-a-file-in-the-folder
    import path)."""
    raw = Path(path).read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except ValueError as e:
        raise OntologyValidationError(f"{path} is not valid JSON: {e}")
    return validate_ontology_set(data)
