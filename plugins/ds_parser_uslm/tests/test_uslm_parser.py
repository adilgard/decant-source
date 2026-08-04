"""The USLM plugin, tested against a fixture.

Lives WITH the plugin, not in the core suite. Core's tests must stay
corpus-agnostic; a statute test in there would be the boundary leaking in
the direction nobody notices — not an import, just a dependency of
attention.

What must hold:

* THE VOCABULARY IS CLOSED. Every entity type and predicate the plugin can
  emit is in `ontology/tax-statute-0.1.json`. This is the property that
  makes the plugin safe to point a pipeline at: core will quarantine
  anything unrecognised, and this test says there should be nothing to
  quarantine.
* SPANS ARE COMPUTED, AND CORRECT. Every declared offset slices back to the
  text the fact claims. Proven on a document containing a repeated phrase,
  where a parser that searched instead of computing would pick the wrong
  occurrence and still look right.
* STRUCTURE IS RECOVERED. Subsections nest in sections, sections in
  chapters, up to the title — the spine prose retrieval cannot supply.
* CROSS-REFERENCES BECOME EDGES, including to provisions not in this
  document, which is the normal case and the one that makes the corpus
  connect up as more files land.
* DOCUMENT FURNITURE IS NOT LAW. Source credits and notes render into the
  text but never become provisions.
* SECTIONS ARE THE CHUNK UNIT. Headings stop at section level so a section
  and its subsections stay one retrievable passage.
* NO LLM. Structural, not asserted in prose: the module imports nothing
  that could call one.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from knowledge_hub.interfaces import FactParser, ParsedFact, ParseError, Parser
from knowledge_hub.models import DocType, RawDocument

from ds_parser_uslm.parser import (
    EMITTED_ENTITY_TYPES,
    EMITTED_PREDICATES,
    IDENTIFIER_KEY,
    UslmParser,
)
from ds_parser_uslm.uslm import UslmError, citation_for, parse_uslm

FIXTURE = Path(__file__).parent / "fixtures" / "usc26_excerpt.xml"
ONTOLOGY_FILE = (Path(__file__).parents[1] / "ds_parser_uslm" / "ontology"
                 / "tax-statute-0.1.json")


@pytest.fixture(scope="module")
def content() -> bytes:
    return FIXTURE.read_bytes()


@pytest.fixture(scope="module")
def raw() -> RawDocument:
    return RawDocument(tenant_id="t-uslm", source_system="filesystem",
                       source_native_id="usc26_excerpt.xml",
                       mime_type="application/xml", content_hash="deadbeef",
                       raw_uri="s3://kh-raw/usc26", id=1,
                       native_metadata={"source_ref": "usc-title26"})


@pytest.fixture(scope="module")
def parsed(content, raw):
    parser = UslmParser()
    document = parser.parse(raw, content)
    text = parser.extract_text(raw, content)
    document.id = 42
    facts = parser.parse_facts(document, text, content)
    return {"parser": parser, "document": document, "text": text,
            "facts": facts}


def of(facts, predicate) -> list[ParsedFact]:
    return [f for f in facts if f.predicate == predicate]


# ============================================================ the contracts
def test_it_satisfies_both_core_contracts():
    """One object, both roles — so the text and the fact offsets come from
    a single parse and cannot drift apart."""
    parser = UslmParser()
    assert isinstance(parser, Parser)
    assert isinstance(parser, FactParser)
    assert parser.name == "uslm" and parser.version


def test_the_document_is_prose_so_it_still_chunks_and_embeds(parsed):
    document = parsed["document"]
    assert document.doc_type is DocType.prose
    assert document.data_track == "prose"
    assert document.metadata["parser"].startswith("uslm ")
    assert document.metadata["uslm_identifier"] == "/us/usc/t26"
    assert document.title == "Internal Revenue Code"


def test_unreadable_input_raises_the_contract_error(raw):
    """Core nacks on ParseError so the item redelivers. Raising anything
    else would make a bad document vanish from the queue."""
    parser = UslmParser()
    with pytest.raises(ParseError):
        parser.parse(raw, b"<not-uslm><nope/></not-uslm>")
    with pytest.raises(ParseError):
        parser.parse(raw, b"this is not xml at all")


def test_a_non_uslm_root_is_named_not_guessed_at():
    with pytest.raises(UslmError, match="not a USLM document root"):
        parse_uslm(b"<html><main><p>hi</p></main></html>")


# ======================================================== the closed vocabulary
def test_every_emitted_term_is_in_the_shipped_ontology(parsed):
    """THE load-bearing test. Core quarantines anything the active ontology
    does not permit; this says the plugin gives it nothing to quarantine."""
    declared = json.loads(ONTOLOGY_FILE.read_text(encoding="utf-8"))
    allowed_types = set(declared["entity_types"])
    allowed_predicates = set(declared["predicates"])

    # The constants and the shipped file must not drift apart.
    assert EMITTED_ENTITY_TYPES == allowed_types
    assert EMITTED_PREDICATES == allowed_predicates

    facts = parsed["facts"]
    assert facts, "the fixture produced no facts at all"
    for fact in facts:
        assert fact.predicate in allowed_predicates, fact.predicate
        assert fact.subject_type in allowed_types, fact.subject_type
        if fact.object_type is not None:
            assert fact.object_type in allowed_types, fact.object_type


def test_the_ontology_set_is_valid_for_core_import(parsed):
    """It must be importable through the operator path unchanged — a
    vocabulary the console rejects is a vocabulary nobody can activate."""
    from knowledge_hub.ontology_registry import validate_ontology_set

    declared = json.loads(ONTOLOGY_FILE.read_text(encoding="utf-8"))
    validated = validate_ontology_set(declared)
    assert validated.version == "tax-statute-0.1"


def test_every_fact_is_well_formed_for_the_core_gate(parsed):
    """Exactly one object flavour, subject always present — the shape
    check core applies before it even looks at the vocabulary."""
    for fact in parsed["facts"]:
        has_entity = bool(fact.object_text)
        has_literal = bool(fact.object_literal)
        assert has_entity != has_literal, fact
        assert fact.subject_text.strip()
        if has_entity:
            assert fact.object_type


# ================================================================== the spans
def test_every_declared_span_slices_back_to_what_it_claims(parsed):
    """Computed, not searched. If any offset is off by even one character
    this fails, and in the pipeline it would surface as 'span_mismatch'."""
    text = parsed["text"]
    spanned = [f for f in parsed["facts"] if f.char_start is not None]
    assert spanned, "no fact declared a span"
    for fact in spanned:
        assert text[fact.char_start:fact.char_end] == fact.span_text, fact


def test_spans_survive_a_phrase_that_repeats(parsed):
    """The fixture says 'A tax is imposed on the taxable income of every'
    twice. A parser that located spans by searching the text would put both
    subsections' facts on the first occurrence and look perfectly fine."""
    text = parsed["text"]
    assert text.count("A tax is imposed on the taxable income of every") == 2

    headings = {f.object_literal: f for f in of(parsed["facts"], "has_heading")}
    a = headings["Married individuals filing joint returns"]
    b = headings["Heads of households"]
    assert a.char_start < b.char_start, "subsection (b) placed before (a)"
    assert text[a.char_start:a.char_end] != text[b.char_start:b.char_end]


def test_a_cross_reference_span_covers_the_link_text(parsed):
    text = parsed["text"]
    refs = of(parsed["facts"], "references")
    to_63 = next(f for f in refs if "63" in (f.object_text or ""))
    assert text[to_63.char_start:to_63.char_end] == "section 63"


# ============================================================== the structure
def test_the_nesting_spine_is_recovered(parsed):
    """subsection -> section -> chapter -> subtitle -> title. This is what
    prose retrieval cannot answer and the reason a parser is worth having."""
    edges = {(f.subject_text, f.object_text) for f in of(parsed["facts"],
                                                         "part_of")}
    assert ("26 U.S.C. § 1(a)", "26 U.S.C. § 1") in edges
    assert ("26 U.S.C. § 1(b)", "26 U.S.C. § 1") in edges
    assert ("26 U.S.C. § 1(b)(1)", "26 U.S.C. § 1(b)") in edges
    assert ("26 U.S.C. § 1", "26 U.S.C. ch. 1") in edges
    assert ("26 U.S.C. ch. 1", "26 U.S.C. subtitle A") in edges
    assert ("26 U.S.C. subtitle A", "26 U.S.C.") in edges


def test_provisions_carry_their_identifier_as_a_resolver_key(parsed):
    """A USLM identifier is globally unique and stable, which makes it a
    real deterministic key for the resolver's T0 tier rather than a name
    that merely looks unique."""
    fact = next(f for f in of(parsed["facts"], "part_of")
                if f.subject_text == "26 U.S.C. § 1(a)")
    assert fact.subject_keys == {IDENTIFIER_KEY: "/us/usc/t26/s1/a"}
    assert fact.object_keys == {IDENTIFIER_KEY: "/us/usc/t26/s1"}


def test_headings_become_queryable_literals(parsed):
    headings = {f.subject_text: f.object_literal
                for f in of(parsed["facts"], "has_heading")}
    assert headings["26 U.S.C. § 1"] == "Tax imposed"
    assert headings["26 U.S.C. § 63"] == "Taxable income defined"
    assert headings["26 U.S.C. ch. 1"] == "Normal Taxes and Surtaxes"


# ========================================================= cross-references
def test_a_reference_inside_this_document_becomes_an_edge(parsed):
    edges = {(f.subject_text, f.object_text)
             for f in of(parsed["facts"], "references")}
    assert ("26 U.S.C. § 1(a)", "26 U.S.C. § 63") in edges


def test_a_reference_to_a_provision_not_in_this_file_still_becomes_an_edge(
        parsed):
    """The normal case. § 2 is not in this document; the edge is emitted
    anyway, carrying the identifier, and the resolver joins it up when the
    file holding § 2 lands. Dropping it would mean the corpus never
    connects."""
    to_2 = [f for f in of(parsed["facts"], "references")
            if f.object_keys.get(IDENTIFIER_KEY) == "/us/usc/t26/s2"]
    assert len(to_2) == 1
    assert to_2[0].object_text == "26 U.S.C. § 2"
    assert to_2[0].subject_text == "26 U.S.C. § 1(b)"


# ============================================================== the furniture
def test_source_credits_and_notes_are_text_not_provisions(parsed):
    """They render — a reader wants them — but asserting 'the Effective
    Date note is part_of § 1' would put editorial apparatus into the
    knowledge graph as though it were law."""
    text = parsed["text"]
    assert "68A Stat. 5" in text
    assert "Effective Date" in text

    subjects = {f.subject_text for f in parsed["facts"]}
    assert all(s.startswith("26 U.S.C.") for s in subjects), subjects
    assert not any("Stat." in s for s in subjects)


# =================================================== chunking-shape decision
def test_headings_stop_at_section_so_a_section_is_one_chunk(parsed):
    """The core chunker splits on the dominant heading level, capped at 3.
    Sections render as '###' and everything below renders inline, so a
    section and its subsections stay one retrievable passage instead of
    being scattered."""
    lines = [ln for ln in parsed["text"].splitlines() if ln.startswith("#")]
    assert lines, "no headings at all — every section would be one chunk"
    assert all(len(ln) - len(ln.lstrip("#")) <= 3 for ln in lines)
    assert any(ln.startswith("### 26 U.S.C. § 1 ") for ln in lines)
    # Subsection labels are inline text, never headings.
    assert not any(ln.lstrip("#").strip().startswith("(a)") for ln in lines)
    assert "(a) Married individuals filing joint returns" in parsed["text"]


def test_levels_above_a_section_never_share_its_heading_depth(parsed):
    """Chapters cap at '##' so they cannot collide with a section's '###'.

    If they shared a level the chunker — which splits on the DOMINANT
    heading level — would cut at every chapter heading too, leaving
    heading-only chunks competing with real sections in retrieval. Cheap to
    get wrong, invisible until search quality sags."""
    depths = {}
    for line in parsed["text"].splitlines():
        if line.startswith("#"):
            depth = len(line) - len(line.lstrip("#"))
            depths.setdefault(depth, []).append(line.lstrip("# "))

    section_lines = [ln for lines in depths.values() for ln in lines
                     if ln.startswith("26 U.S.C. §")]
    assert len(section_lines) == 2
    section_depths = {d for d, lines in depths.items()
                      if any(ln.startswith("26 U.S.C. §") for ln in lines)}
    above_depths = {d for d, lines in depths.items()
                    if any(not ln.startswith("26 U.S.C. §") for ln in lines)}
    assert section_depths == {3}
    assert above_depths and max(above_depths) < 3


def test_the_dominant_heading_level_is_the_section_level(parsed):
    """What the core chunker actually computes: the most common heading
    depth, ties broken deeper. It must land on sections."""
    depths = [len(ln) - len(ln.lstrip("#"))
              for ln in parsed["text"].splitlines() if ln.startswith("#")]
    dominant = min(max(sorted(set(depths), reverse=True), key=depths.count), 3)
    assert dominant == 3


# ==================================================================== citations
@pytest.mark.parametrize("identifier, expected", [
    ("/us/usc/t26", "26 U.S.C."),
    ("/us/usc/t26/stA", "26 U.S.C. subtitle A"),
    ("/us/usc/t26/stA/ch1", "26 U.S.C. ch. 1"),
    ("/us/usc/t26/s1", "26 U.S.C. § 1"),
    ("/us/usc/t26/s1/a", "26 U.S.C. § 1(a)"),
    ("/us/usc/t26/s1/b/1", "26 U.S.C. § 1(b)(1)"),
    ("/us/usc/t26/s501/c/3", "26 U.S.C. § 501(c)(3)"),
])
def test_citations_are_derived_deterministically(identifier, expected):
    assert citation_for(identifier) == expected


def test_an_unreadable_identifier_is_passed_through_not_invented():
    """A wrong citation is worse than an ugly one."""
    assert citation_for("/eu/directive/2016/679") == "/eu/directive/2016/679"


# ================================================== reachable only by config
def test_core_resolves_the_plugin_from_a_config_string(parsed):
    """The intended selection path, exercised. Core is handed a STRING and
    produces the component; it never imports this package, and the string
    is the only coupling that exists."""
    from knowledge_hub.plugins import FACT_PARSERS, PARSERS

    ref = "ds_parser_uslm.parser:UslmParser"
    assert isinstance(PARSERS.build(ref), Parser)
    assert isinstance(FACT_PARSERS.build(ref), FactParser)


def test_core_does_not_ship_this_plugin_under_a_short_name():
    """A registered short name in core would be core knowing about a
    corpus. The reference must stay a dotted path supplied by config."""
    from knowledge_hub.plugins import FACT_PARSERS, PARSERS

    assert "uslm" not in PARSERS.names()
    assert "uslm" not in FACT_PARSERS.names()
    assert FACT_PARSERS.names() == []


def test_the_source_config_that_selects_it_validates(parsed):
    """The exact config an operator would save, run through core's own
    selection helpers — so 'here is the config to use' in the README is a
    tested claim rather than documentation."""
    from knowledge_hub import plugins

    config = {"data_track": "prose",
              "parser": "ds_parser_uslm.parser:UslmParser",
              "extraction_strategy": "parser_supplied",
              "fact_parser": "ds_parser_uslm.parser:UslmParser"}
    assert plugins.strategy_name_for(config, "prose") == \
        plugins.PARSER_SUPPLIED_STRATEGY
    assert plugins.parser_ref_for(config) == config["parser"]
    assert plugins.fact_parser_ref_for(config) == config["fact_parser"]


# ======================================================================= no LLM
def test_the_plugin_cannot_reach_a_model():
    """Structural. Determinism you can grep for beats determinism you
    assert in a docstring."""
    import ds_parser_uslm.parser as mod
    import ds_parser_uslm.uslm as uslm

    for module in (mod, uslm):
        source = Path(module.__file__).read_text(encoding="utf-8")
        for banned in ("ollama", "openai", "requests", "httpx", "urllib"):
            assert f"import {banned}" not in source, (module.__name__, banned)
