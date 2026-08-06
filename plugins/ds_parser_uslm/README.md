# ds-parser-uslm

A US Code (USLM XML) parser plugin for decant.Source.

It reads the XML the House publishes the US Code in, renders it as readable
text for retrieval, and emits the structure and cross-references as
deterministic facts. No language model is involved at any point.

## Why it lives here and not in the core package

`knowledge_hub_pkg` is corpus-agnostic. It must not learn that
`<subsection>` nests inside `<section>`, or that `/us/usc/t26/s1/a` is
cited `26 U.S.C. § 1(a)`. All of that is domain knowledge, so all of it is
here.

The coupling runs one way and only one way:

```
ds_parser_uslm  ──imports──>  knowledge_hub   (contracts: Parser, FactParser)
knowledge_hub   ──/ /──────>  ds_parser_uslm  (never; refused at runtime)
```

Core reaches this package by resolving a **string** from a source's config
at runtime. It has no import of it, no mention of it, and a plugin
reference pointing back into `knowledge_hub` is refused with
`BoundaryViolation` — so the rule is enforced by the code, not by memory.

## Install

```bash
pip install -e plugins/ds_parser_uslm
```

## Point a source at it

Set this on the source, via the operator console (Data Landing → Extraction
setup) or `set_extraction_setup`:

```json
{
  "parser": "ds_parser_uslm.parser:UslmParser",
  "extraction_strategy": "parser_supplied",
  "fact_parser": "ds_parser_uslm.parser:UslmParser"
}
```

Three keys, which is exactly what `set_extraction_setup` accepts. It does
NOT take `data_track`, and this source does not need one: `data_track`
only picks a strategy when `extraction_strategy` is absent, and the parser
stamps `prose` on the document itself. An earlier draft of this file listed
it as a fourth key, which sent an operator looking for a field that isn't
there.

The same class fills both roles deliberately. Fact offsets have to index
the same string the chunker cut, and the only way to guarantee that is for
one parse to produce both.

Folder ingest also needs `.xml` in its **File types** field — the shipped
default is `.md .txt .pdf .docx`, and it is per-job so widening it here
leaves every other folder alone.

## Ontology

Requires `ontology/tax-statute-0.1.json` to be imported and active:

| | |
|---|---|
| entity types | `Provision` |
| predicates | `part_of`, `references`, `has_heading` |

Importing and activating it is an operator action; this package only
declares what it emits. Core validates against whatever is **active** and
quarantines anything else, so pointing a source here without the vocabulary
loaded produces a review queue full of quarantined facts rather than silent
wrong data.

The set is deliberately tiny. It covers what a parser can know for certain
from markup — where a provision sits, what it is headed, what it points at.
It is not a model of tax law. Definitions, rates, effective dates and
amendments are interpretation, and belong to something that extracts
meaning rather than structure.

## What it emits

| fact | from | span |
|---|---|---|
| `Provision part_of Provision` | element nesting | the child's heading/label |
| `Provision has_heading "..."` | `<heading>` | the heading line |
| `Provision references Provision` | `<ref href>` | the link text itself |

Every provision carries `uslm_identifier` as an extracted key. A USLM
identifier is globally unique and stable, which makes it a real
deterministic key for the resolver's T0 tier rather than a name that merely
looks unique — two files mentioning `26 U.S.C. § 63` resolve to one entity
without a model being asked.

That last sentence is only true because core is told to believe the key.
The `Provision` row in `resolution_policy` carries
`keys_are_authoritative = true` (migration 014), which is what makes an
unseen identifier mean a NEW provision instead of a fuzzy-match candidate.
Without it the resolver falls back to name similarity, and citations are
the worst possible input for that: `26 U.S.C. § 63` and `26 U.S.C. § 163`
score 0.97 while being unrelated sections. The first real ingest resolved
4 of 73 mentions that way. If a deployment ever ships without that row,
this is where it will show up.

Cross-references to provisions **not** in the current file are still
emitted. That is the normal case, and it is what makes the corpus connect
up as more files land.

Source credits and notes render into the text, because a reader wants them,
but never become provisions.

## Chunking shape

Titles and chapters render as `#`/`##`, sections as `###`, and everything
below a section renders as inline labelled text. The core chunker splits on
the dominant heading level, so this makes the **section** the parent chunk —
the unit citations are normally written against — and keeps a section's
subsections together in one retrievable passage.

## Tests

```bash
pip install -e plugins/ds_parser_uslm
python -m pytest plugins/ds_parser_uslm/tests -q
```

They live here rather than in the core suite on purpose: a statute test in
there would be the boundary leaking in the direction nobody notices — not
an import, just a dependency of attention.

The fixture is hand-authored and faithful to USLM 1.0 rather than a
downloaded slice of Title 26. It carries the cases that are easy to get
wrong: three nesting depths, a nested paragraph, a reference to a provision
outside the file, document furniture that must not become law, and a
repeated phrase that would catch a parser locating spans by searching
instead of computing them.
