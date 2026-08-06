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
    assert ("26 U.S.C. § 1", "26 U.S.C. subtitle A, ch. 1") in edges
    assert ("26 U.S.C. subtitle A, ch. 1", "26 U.S.C. subtitle A") in edges
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
    assert headings["26 U.S.C. subtitle A, ch. 1"] == "Normal Taxes and Surtaxes"


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
    # Structural citations carry the full path (2026-08-04): 46 parts
    # numbered II exist, so the last label alone names a KIND of place.
    ("/us/usc/t26/stA/ch1", "26 U.S.C. subtitle A, ch. 1"),
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


# ================================================== past law and collisions
# Written against inline markup rather than the shared fixture on purpose:
# these are cases the REAL Title 26 contains and the fixture deliberately does
# not, and folding them in would move the counts every other test asserts.
NS = 'xmlns="http://xml.house.gov/schemas/uslm/1.0"'


def doc_of(body: str) -> bytes:
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<uscDoc {NS} identifier="/us/usc/t26"><main>{body}</main>'
            f'</uscDoc>').encode("utf-8")


def test_repealed_elements_render_but_are_not_provisions():
    """Same rule the source credits and notes already follow: a reader
    searching the corpus should still find that § 4521 was repealed, and the
    graph should not carry it as current law."""
    parsed = parse_uslm(doc_of(
        '<section identifier="/us/usc/t26/s4611">'
        '<num value="4611">§ 4611.</num><heading>Imposition of tax</heading>'
        '<content>A tax is imposed.</content></section>'
        '<section status="repealed" identifier="/us/usc/t26/s4521">'
        '<num value="4521">[§ 4521.</num><heading>Repealed.</heading>'
        '<content>Repealed by Pub. L. 87-456.</content></section>'))

    assert [p.identifier for p in parsed.provisions] == ["/us/usc/t26/s4611"]
    assert parsed.skipped_by_status == 1
    # Rendered, not dropped: still retrievable prose.
    assert "4521" in parsed.text and "Repealed" in parsed.text


def test_a_wholly_repealed_document_is_not_a_parse_error():
    """Title 26's repealed chapter 38 is entirely past law. Raising here
    would nack the queue item and redeliver the file forever; the truthful
    answer is a readable document that yields no facts."""
    parsed = parse_uslm(doc_of(
        '<chapter status="repealed" identifier="/us/usc/t26/stD/ch38">'
        '<num value="38">[CHAPTER 38—</num><heading>REPEALED]</heading>'
        '</chapter>'))

    assert parsed.provisions == []
    assert parsed.skipped_by_status == 1
    assert parsed.text.strip()          # it still rendered

    # And the Parser contract survives having no provision to name itself after.
    raw = RawDocument(tenant_id="t-uslm", source_system="filesystem",
                      source_native_id="ch38.xml", mime_type="application/xml",
                      content_hash="c0ffee", raw_uri="s3://kh-raw/ch38", id=7)
    document = UslmParser().parse(raw, doc_of(
        '<chapter status="repealed" identifier="/us/usc/t26/stD/ch38">'
        '<num value="38">[CHAPTER 38—</num><heading>REPEALED]</heading>'
        '</chapter>'))
    assert document.metadata["provisions"] == 0
    assert document.metadata["uslm_identifier"] is None
    assert document.metadata["skipped_by_status"] == 1
    # Named after the file, because there is no provision left to name it
    # after and Title 26 has 21 of these — one shared placeholder would make
    # a document list unreadable.
    assert document.title == "ch38.xml — repealed, no current provisions"


def test_bytes_with_no_numbered_elements_at_all_still_raise():
    """The guard that catches 'these bytes are not really a statute' has to
    survive the one above relaxing it."""
    with pytest.raises(UslmError, match="no numbered provisions"):
        parse_uslm(doc_of("<content>Prose with nothing numbered.</content>"))


def test_a_repeated_identifier_keeps_the_first_and_records_the_rest():
    """A USLM identifier is supposed to name one thing. Title 26 breaks that
    twice — two <chapter> elements claim /us/usc/t26/stD/ch38, and § 7508A has
    two subsections lettered (f). Building a dict would keep whichever came
    last, silently. This keeps the first and says so."""
    parsed = parse_uslm(doc_of(
        '<section identifier="/us/usc/t26/s7508A">'
        '<num value="7508A">§ 7508A.</num><heading>Postponement</heading>'
        '<subsection identifier="/us/usc/t26/s7508A/f">'
        '<num value="f">(f)</num><content>First (f).</content></subsection>'
        '<subsection identifier="/us/usc/t26/s7508A/f">'
        '<num value="f">(f)</num><heading>Application to limitation</heading>'
        '<content>Second (f).</content></subsection></section>'))

    ids = [p.identifier for p in parsed.provisions]
    assert ids == ["/us/usc/t26/s7508A", "/us/usc/t26/s7508A/f"]
    assert parsed.duplicate_identifiers == ["/us/usc/t26/s7508A/f"]
    # The first occurrence is the one kept — not the last, and not a merge.
    kept = parsed.by_identifier()["/us/usc/t26/s7508A/f"]
    assert kept.heading is None
    # The second still renders, so nothing is lost from the readable document.
    assert "Second (f)." in parsed.text


# ======================================================= quoted amendment text
# D4 (Job 8): notes quote the verbatim text of the amending Public Law in
# `<quotedContent>`, and that quote carries `<paragraph>`/`<subparagraph>`
# markup with num and heading but NO identifier. Treating it as law inflated
# the provision count 17.5% and — through by_identifier()'s '' entry meeting
# ownerless refs — leaked ~128 keyless mentions into the resolver's fuzzy
# path. The rule since 0.1.2: quoted content renders, and nothing inside it
# is ever indexed.

LEAK_DOC = doc_of(
    # The t26_s185 shape: the real section is REPEALED, so every ref in its
    # notes is ownerless (owner=None -> stored ''), which is exactly the
    # lookup that used to find a quoted junk provision under index[''].
    '<section status="repealed" identifier="/us/usc/t26/s185">'
    '<num value="185">[§ 185.</num><heading>Repealed.</heading>'
    '<notes><note topic="effectiveDate"><heading>Effective Date</heading>'
    '<p><ref href="/us/pl/99/514/tII/s242/c">Pub. L. 99–514, § 242(c)</ref>'
    ' provided that: '
    '<quotedContent origin="/us/pl/99/514/tII/s242/c">'
    '<paragraph><num value="1">“(1)</num><heading>In general.</heading>'
    '<content>Quoted first paragraph, citing '
    '<ref href="/us/usc/t26/s63">section 63</ref>.</content></paragraph>'
    '<subparagraph><num value="A">“(A)</num>'
    '<content>Quoted subparagraph.</content></subparagraph>'
    '</quotedContent></p></note></notes></section>')


def test_quoted_markup_renders_but_never_becomes_a_provision():
    parsed = parse_uslm(LEAK_DOC)
    # Nothing inside the quote is indexed...
    assert parsed.provisions == []
    assert parsed.quoted_elements == 2
    assert parsed.skipped_by_status == 1          # the repealed real section
    assert "" not in parsed.by_identifier()       # the leak's entry point
    # ...and everything still renders for the reader, labels included.
    assert "Quoted first paragraph" in parsed.text
    assert "(1) In general." in parsed.text
    assert "(A) Quoted subparagraph." in parsed.text


def test_the_keyless_mention_leak_shape_produces_zero_facts():
    """THE regression. Under 0.1.1 this exact shape emitted `references`
    facts whose subject was the quoted junk provision with
    subject_keys={'uslm_identifier': ''} — the resolver drops empty-string
    key values, so each became a keyless mention in the fuzzy path."""
    raw = RawDocument(tenant_id="t-uslm", source_system="filesystem",
                      source_native_id="s185.xml",
                      mime_type="application/xml", content_hash="beef185",
                      raw_uri="s3://kh-raw/s185", id=9)
    parser = UslmParser()
    document = parser.parse(raw, LEAK_DOC)
    document.id = 91
    text = parser.extract_text(raw, LEAK_DOC)
    facts = parser.parse_facts(document, text, LEAK_DOC)

    assert facts == []
    # The junk-title symptom is gone with the junk provision: a file whose
    # only current-law content is quoted text names itself honestly.
    assert document.metadata["uslm_identifier"] is None
    assert document.metadata["quoted_elements"] == 2


BOTH_DIRECTIONS_DOC = doc_of(
    '<section identifier="/us/usc/t26/s280F">'
    '<num value="280F">§ 280F.</num><heading>Limitation</heading>'
    '<content>Live text citing '
    '<ref href="/us/usc/t26/s179">section 179</ref>.</content>'
    '<notes><note topic="effectiveDateOfAmendment">'
    '<p>Adjacent cite of <ref href="/us/usc/t26/s168">section 168</ref>, '
    'provided that: <quotedContent origin="/us/pl/115/97/tI/s13202/b">'
    '“The amendments to <ref href="/us/usc/t26/s179">section 179</ref> '
    '[amending <ref href="/us/usc/t26/s168">section 168</ref>] shall apply.'
    '<paragraph><num value="2">“(2)</num><heading>Exception.</heading>'
    '<content>Quoted paragraph text.</content></paragraph>'
    '</quotedContent></p></note></notes></section>')


def test_a_ref_beside_a_quote_still_resolves_and_one_inside_does_not():
    """Both directions of the fix, in one document. The quote and the note
    cite the SAME sections (179, 168), so a fix that filtered by target or
    by proximity — instead of by position inside the quote — fails one half
    of this test."""
    parsed = parse_uslm(BOTH_DIRECTIONS_DOC)
    assert [p.identifier for p in parsed.provisions] == ["/us/usc/t26/s280F"]
    assert parsed.quoted_elements == 1

    raw = RawDocument(tenant_id="t-uslm", source_system="filesystem",
                      source_native_id="s280F.xml",
                      mime_type="application/xml", content_hash="beef280f",
                      raw_uri="s3://kh-raw/s280F", id=10)
    parser = UslmParser()
    document = parser.parse(raw, BOTH_DIRECTIONS_DOC)
    document.id = 92
    text = parser.extract_text(raw, BOTH_DIRECTIONS_DOC)
    facts = parser.parse_facts(document, text, BOTH_DIRECTIONS_DOC)

    refs = of(facts, "references")
    targets = sorted(f.object_keys[IDENTIFIER_KEY] for f in refs)
    # 179 from the live body, 168 from the note text NEXT TO the quote —
    # each exactly once. The quote's own refs to the same two sections
    # emitted nothing.
    assert targets == ["/us/usc/t26/s168", "/us/usc/t26/s179"]
    for fact in refs:
        assert fact.subject_keys == {IDENTIFIER_KEY: "/us/usc/t26/s280F"}
    # The quoted ref text still rendered — it is part of the quote.
    assert "[amending section 168]" in text

    # The blanket property the leak broke: every key on every fact carries a
    # real value. (parse_facts reads structure, so assert over the parsed
    # facts, not over strings.)
    for fact in facts:
        for value in {**fact.subject_keys, **(fact.object_keys or {})}.values():
            assert value


def test_an_unidentified_numbered_element_outside_a_quote_is_refused():
    """The second door. A real provision always carries an identifier
    (proven corpus-wide below); one without it would land in
    by_identifier() under '' and re-open the leak, so it renders as text
    instead of indexing."""
    parsed = parse_uslm(doc_of(
        '<section identifier="/us/usc/t26/s1">'
        '<num value="1">§ 1.</num><heading>Tax imposed</heading>'
        '<content>Text.</content>'
        '<subsection><num value="a">(a)</num>'
        '<content>No identifier on this one.</content></subsection>'
        '</section>'))
    assert [p.identifier for p in parsed.provisions] == ["/us/usc/t26/s1"]
    assert "" not in parsed.by_identifier()
    assert "No identifier on this one." in parsed.text


# ============================================== citations of real identifiers
def test_en_dashed_section_numbers_keep_their_section():
    """OLRC punctuates compound section numbers with an EN DASH (U+2013), not
    an ASCII hyphen: 261 identifiers in Title 26 alone. An ASCII-only number
    class dropped the whole segment and rendered '26 U.S.C.' — which is not
    just ugly, it is the SAME string for every en-dashed section in a title,
    so unrelated provisions became one surface. Found on the first full-title
    run, 2026-08-04."""
    assert citation_for("/us/usc/t26/s1400Z–2") == "26 U.S.C. § 1400Z–2"
    assert citation_for("/us/usc/t26/s1400Z–2/d/2/B") == \
        "26 U.S.C. § 1400Z–2(d)(2)(B)"
    assert citation_for("/us/usc/t42/s300gg–44/c/2") == \
        "42 U.S.C. § 300gg–44(c)(2)"
    assert citation_for("/us/usc/t42/s1320a–1") == "42 U.S.C. § 1320a–1"
    # And the two dashes stay DISTINCT surfaces, because they are distinct
    # sections — the fix must not normalise one into the other.
    assert citation_for("/us/usc/t42/s300gg-44") != \
        citation_for("/us/usc/t42/s300gg–44")


def test_an_unreadable_identifier_degrades_to_unique_not_to_a_collision():
    """The structural guard behind the fix above.

    A citation that stops at the title does not identify a provision, and
    every identifier that produced one produced the SAME one — which is how a
    parser quietly merges unrelated law. Returning the identifier is ugly and
    unique; the docstring's rule is that a wrong citation beats an ugly one,
    and a colliding citation is worse than either."""
    weird = "/us/usc/t26/s4531 /us/usc/t26/s4532"   # real: repealed ch. 38
    other = "/us/usc/t26/s4541 /us/usc/t26/s4542"
    for ident in (weird, other):
        assert citation_for(ident) == ident
        assert citation_for(ident) != "26 U.S.C."
    assert citation_for(weird) != citation_for(other)
    # A bare title still renders as the title: it genuinely has no section.
    assert citation_for("/us/usc/t26") == "26 U.S.C."


def test_structural_citations_carry_their_full_path():
    """A structural label repeats: Title 26 has 46 parts numbered II. The
    citation must therefore carry the path down to it, or 46 different places
    share one name. This was the THIRD citation collision found on the first
    full-title run — after the en dash and the title-only fallback — all three
    variants of one property, which the test below pins corpus-wide."""
    assert citation_for("/us/usc/t26/stA/ch1/schC/ptI/sptC") == \
        "26 U.S.C. subtitle A, ch. 1, subch. C, pt. I, subpt. C"
    assert citation_for("/us/usc/t26/stA/ch1/schC/ptIII/sptC") == \
        "26 U.S.C. subtitle A, ch. 1, subch. C, pt. III, subpt. C"
    assert citation_for("/us/usc/t26/stA/ch1") == "26 U.S.C. subtitle A, ch. 1"
    # Sections are untouched: the familiar short form stays the short form.
    assert citation_for("/us/usc/t26/s63/b/1") == "26 U.S.C. § 63(b)(1)"


CORPUS = Path(r"C:\Users\adilg\Documents\Documents Workspace\Corpus"
              r"\us-code-title-26")


@pytest.mark.skipif(not CORPUS.is_dir(), reason="Title 26 corpus not present")
def test_no_two_identifiers_in_the_real_corpus_share_a_citation():
    """THE PROPERTY, pinned against all 287 real files rather than a fixture:
    distinct identifiers must render distinct citations, because identical
    surfaces are how unrelated law becomes one entity.

    Every citation bug so far — the en dash (90 identifiers -> '26 U.S.C.'),
    the title-only fallback, the last-label-only structural form (46 places
    named 'pt. II') — is an instance of this one property failing. A fixture
    cannot pin it, because each bug came from a corner the fixture's author
    did not know the real data had."""
    import re

    ident_pat = re.compile(
        rb'<(?:title|subtitle|chapter|subchapter|part|subpart|division'
        rb'|subdivision|section|subsection|paragraph|subparagraph|clause'
        rb'|subclause|item|subitem|subsubitem)\b[^>]*identifier="([^"]+)"')
    by_citation: dict[str, set[str]] = {}
    for f in CORPUS.glob("*.xml"):
        for m in ident_pat.finditer(f.read_bytes()):
            ident = m.group(1).decode("utf-8")
            by_citation.setdefault(citation_for(ident), set()).add(ident)

    collisions = {c: sorted(ids) for c, ids in by_citation.items()
                  if len(ids) > 1
                  # The KNOWN duplicate-identifier defects in the published
                  # data itself (repealed ch. 38 vs current, s7508A(f) twice,
                  # ...) are one identifier used by two elements — the same
                  # STRING, so they collapse to one set member and pass here.
                  # A citation collision is two DIFFERENT identifiers.
                  }
    assert not collisions, (
        f"{len(collisions)} citation(s) shared by distinct identifiers; "
        f"worst: {sorted(collisions.items(), key=lambda kv: -len(kv[1]))[:3]}")


@pytest.mark.skipif(not CORPUS.is_dir(), reason="Title 26 corpus not present")
def test_no_provision_in_the_real_corpus_is_keyless():
    """THE PROPERTY behind D4, pinned against all 287 real files: every
    indexed provision carries a non-empty identifier, so by_identifier()
    can never hold the '' entry that handed ownerless refs a junk subject.
    Job 8 measured 12,408 identifier-less elements (17.5% of the old count),
    every one of them quoted PL text."""
    total, quoted = 0, 0
    for f in sorted(CORPUS.glob("*.xml")):
        parsed = parse_uslm(f.read_bytes())
        assert all(p.identifier for p in parsed.provisions), f.name
        assert "" not in parsed.by_identifier(), f.name
        total += len(parsed.provisions)
        quoted += parsed.quoted_elements
    # Job 8's D4 finding measured 58,654 identified provisions, recovered
    # here exactly. quoted_elements is 12,415, not the finding's 12,408:
    # the finding counted IDENTIFIER-LESS provisions, and 7 elements inside
    # quotes carry an identifier (a quote of this title's own text) — under
    # 0.1.1 those claimed or duplicated against the real element; under
    # 0.1.2 they are quote-demoted like everything else in a quote.
    assert total == 58_654, total
    assert quoted == 12_415, quoted


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
