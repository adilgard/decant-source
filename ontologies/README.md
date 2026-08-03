# Ontology sets — the portable source of truth

One file per ontology version, named `<version>.json`. This folder is
git-tracked; the database's `ontology_versions` table is the LOADED form,
this folder is the PORTABLE form. Importing (operator console →
`import_ontology`, or dropping a file here and importing it) writes both.

## Shape

```json
{
  "version": "real-1.0",
  "entity_types": ["Vendor", "Customer", "..."],
  "predicates": ["supplies", "invoiced_by", "..."],
  "examples":          { "entity_types": {}, "predicates": {} },
  "predicate_aliases": { "supplied_by": {"predicate": "supplies", "swap": true} },
  "notes": "optional free text"
}
```

- `version` — unique, immutable. Letters/digits/`._-`, starts alphanumeric
  (it is also the filename). To change a published set, publish a NEW
  version; imports never overwrite.
- `entity_types` / `predicates` — the two allowlists. The ontology is an
  allowlist, not a schema: these are flat vocabularies the extractor is
  held to, nothing structural. Predicates must be `lowercase_underscore`
  form (that is the form the binding matches against).
- `examples` (optional) — per-name example strings, included verbatim in
  the extraction prompt. Keys must name declared types/predicates.
- `predicate_aliases` (optional) — unambiguous surface variants,
  normalized deterministically by the binding (`swap: true` flips
  subject/object). Targets must be declared predicates; an alias may not
  shadow a declared predicate.

Validation is deterministic code (`knowledge_hub/ontology_registry.py`) —
no LLM anywhere in the import path.

## What activating a version does — and does not do

Selecting a version as active (operator console → `select_ontology`)
changes which vocabulary FUTURE ingests extract against. It does NOT touch
existing facts: every fact keeps the `ontology_version` that actually
produced it. Re-extracting existing documents under a new version is a
separate, deliberate, scoped operator action (d.s Stage 3).

## Out of scope, on purpose

- Structural ontology fields beyond the two allowlists (attribute schemas,
  type hierarchies) — not part of this contract.
- Watched-folder auto-import — importing is always an explicit, audited act.
