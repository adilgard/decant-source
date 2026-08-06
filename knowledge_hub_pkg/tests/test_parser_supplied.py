"""The parser_supplied seam: a registered plugin produces facts, core
validates them, prose keeps chunking, and no LLM is involved anywhere.

What must hold:

* SELECTION IS CONFIG. A source names its extraction strategy and its
  plugin on its registry row; core reaches the plugin through a registry
  and never imports it. A source that names nothing behaves exactly as it
  did before the seam existed (the old data_track branch, reproduced).
* THE ONTOLOGY GATE IS NOT BYPASSABLE. A plugin's emissions go through the
  SAME allowlist checks as an LLM's. An out-of-vocabulary predicate or
  entity type is quarantined with the plugin's raw output attached, never
  staged. This is the property that makes a plugin a producer rather than
  a hole in the ontology.
* SPANS ARE VERIFIED, NOT TRUSTED. A plugin declares character offsets and
  names the text there; core slices the document text and compares. A
  correct claim grounds as 'declared_span'. A wrong one grounds as
  'span_mismatch', takes the confidence penalty and the review flag, and
  is still staged — same treatment a model's unfindable quote gets.
* FACTS STAY CITABLE. A verified span is anchored to the parent chunk that
  contains it, so retrieval's grounded-facts enrichment (which joins on
  source_chunk_id) surfaces plugin facts like any other.
* THE BOUNDARY IS MECHANICAL. A plugin reference pointing inside
  knowledge_hub is refused at resolution time, because a plugin living in
  core is exactly the corpus-agnostic violation the seam exists to prevent.
* PROVENANCE NAMES THE PRODUCER. The extractor envelope says which plugin
  and which plugin version, so the idempotency ledger treats a plugin
  upgrade as fresh work rather than replaying the old plugin's verdict.

Everything here is domain-free on purpose. The fake plugin emits a
permitted entity and predicate from the baseline ontology; no statute, no
tax, no corpus of any kind. If this file ever needs domain vocabulary to
make its point, the seam has leaked.
"""
from __future__ import annotations

from typing import Any, Optional, Sequence

import pytest

from factories import ONTOLOGY, make_raw, sha

from knowledge_hub import plugins
from knowledge_hub.extraction import (
    ExtractionService,
    document_text_from_chunks,
)
from knowledge_hub.extraction_parser_supplied import ParserSuppliedStrategy
from knowledge_hub.grounding import SpanGrounder
from knowledge_hub.interfaces import (
    ExtractionUnit,
    FactParser,
    OntologyBinding,
    ParsedFact,
    Parser,
)
from knowledge_hub.models import Chunk, ChunkLevel, DocType, Document

# The document every test parses. Offsets below are into THIS string, which
# is also what the parent chunks are cut from — the same anchoring the real
# pipeline uses.
TEXT = ("# Section One\n"                       # 0..14
        "Acme Corp is part of Northwind Group.\n"   # 14..52
        "\n"
        "# Section Two\n"
        "Acme Corp reports to Northwind Group.\n")

ACME = TEXT.index("Acme Corp")
SENTENCE_ONE = TEXT.index("Acme Corp is part of Northwind Group.")
SENTENCE_ONE_END = SENTENCE_ONE + len("Acme Corp is part of Northwind Group.")
SECTION_TWO = TEXT.index("# Section Two")


# --------------------------------------------------------------- doubles --
class FakeBinding(OntologyBinding):
    """A two-word vocabulary. Small on purpose: the test is about the gate,
    not about any particular ontology."""

    version = ONTOLOGY

    def __init__(self, entity_types=("Organization",),
                 predicates=("part_of", "reports_to"),
                 aliases: Optional[dict] = None):
        self._types = set(entity_types)
        self._predicates = set(predicates)
        self._aliases = aliases or {}

    def is_entity_type(self, entity_type: str) -> bool:
        return entity_type in self._types

    def is_predicate(self, predicate: str) -> bool:
        return predicate in self._predicates

    def normalize_predicate(self, raw: str):
        canon = raw.strip().lower().replace(" ", "_").replace("-", "_")
        if canon in self._predicates:
            return canon, False
        if canon in self._aliases:
            target, swap = self._aliases[canon]
            if target in self._predicates:
                return target, swap
        return None

    def output_schema(self, data_track: str) -> dict[str, Any]:
        return {}

    def prompt_vocabulary(self) -> str:
        return ""


class FakeFactParser(FactParser):
    """Emits whatever it was handed. A real plugin computes its facts from
    the document; this one is told them, so each test states exactly the
    shape it is probing."""

    name = "fake"

    def __init__(self, facts: Sequence[ParsedFact] = (), version="1.0.0"):
        self._facts = list(facts)
        self.version = version
        self.calls: list[tuple[int, str]] = []

    def parse_facts(self, document, text, content):
        self.calls.append((len(text), text[:20]))
        return self._facts


class FakeParser(Parser):
    """A Parser that is not Docling, to prove the parser seam actually
    switches rather than always landing on the default."""

    def parse(self, raw, content):
        return Document(tenant_id=raw.tenant_id, raw_document_id=raw.id or 1,
                        doc_type=DocType.prose, title="from the plugin",
                        metadata={"data_track": "prose", "parser": "fake"})

    def extract_text(self, raw, content):
        return TEXT


def part_of_fact(**overrides) -> ParsedFact:
    """The canonical well-formed emission: an edge between two permitted
    entities, with a span that really is where it says it is."""
    fields = dict(
        subject_text="Acme Corp", subject_type="Organization",
        predicate="part_of",
        object_text="Northwind Group", object_type="Organization",
        char_start=SENTENCE_ONE, char_end=SENTENCE_ONE_END,
        span_text=TEXT[SENTENCE_ONE:SENTENCE_ONE_END],
    )
    fields.update(overrides)
    return ParsedFact(**fields)


def unit_for(text: str = TEXT, document_id: int = 1) -> ExtractionUnit:
    document = Document(tenant_id="t", raw_document_id=1,
                        doc_type=DocType.prose, id=document_id,
                        metadata={"data_track": "prose"})
    return ExtractionUnit(document=document, source_system="filesystem",
                          chunk=None, text=text, payload=text.encode())


def run(facts, binding: Optional[FakeBinding] = None):
    strategy = ParserSuppliedStrategy(binding or FakeBinding(),
                                      FakeFactParser(facts))
    return strategy, strategy.extract(unit_for())


# ===================================================== the conformance gate
def test_well_formed_emission_becomes_candidates():
    strategy, result = run([part_of_fact()])

    assert not result.quarantined
    assert len(result.facts) == 1
    fact = result.facts[0]
    assert fact.predicate == "part_of"
    assert fact.char_start == SENTENCE_ONE and fact.char_end == SENTENCE_ONE_END
    # Two distinct entities, each with its own candidate key, and the fact
    # references them rather than repeating their text.
    assert len(result.entities) == 2
    keys = {e.key for e in result.entities}
    assert fact.subject_key in keys and fact.object_key in keys
    # Provenance names the plugin, not just the seam.
    assert strategy.extractor == "parser_supplied:fake"
    assert strategy.version == "1.0.0"


def test_out_of_allowlist_predicate_is_quarantined_not_staged():
    """THE load-bearing test. A plugin is not a way around the ontology."""
    _, result = run([part_of_fact(predicate="secretly_controls")])

    assert result.facts == []
    assert len(result.quarantined) == 1
    q = result.quarantined[0]
    assert q.reason == "unbound_predicate"
    assert q.detail == "secretly_controls"
    # The raw emission is retained, the same way a model's off-ontology
    # attempt is: it is the signal that grows the vocabulary.
    assert q.raw_output["parsed_fact"]["predicate"] == "secretly_controls"
    assert q.extractor == "parser_supplied:fake"
    assert q.ontology_version == ONTOLOGY


def test_out_of_allowlist_entity_type_is_quarantined():
    _, result = run([part_of_fact(subject_type="Spaceship")])

    assert result.facts == []
    assert [q.reason for q in result.quarantined] == ["unbound_entity_type"]
    assert result.quarantined[0].detail == "Spaceship"


def test_object_entity_type_is_checked_too():
    _, result = run([part_of_fact(object_type="Spaceship")])

    assert result.facts == []
    assert [q.reason for q in result.quarantined] == ["unbound_entity_type"]


@pytest.mark.parametrize("overrides, why", [
    ({"object_literal": "both"}, "both object flavors"),
    ({"object_text": None, "object_type": None}, "neither object flavor"),
    ({"subject_text": "   "}, "empty subject"),
    ({"object_type": None}, "entity object without a type"),
])
def test_malformed_shapes_quarantine_as_validation_failure(overrides, why):
    """Shape is checked before vocabulary so the review queue distinguishes
    'the plugin is broken' from 'the ontology is missing a word'. Those
    have completely different fixes."""
    _, result = run([part_of_fact(**overrides)])

    assert result.facts == [], why
    assert [q.reason for q in result.quarantined] == ["validation_failure"], why


def test_literal_valued_fact_is_allowed():
    _, result = run([part_of_fact(object_text=None, object_type=None,
                                  object_literal="Net-30")])

    assert len(result.facts) == 1
    assert result.facts[0].object_literal == "Net-30"
    assert result.facts[0].object_key is None
    assert len(result.entities) == 1  # subject only


def test_alias_swap_flips_the_triple():
    binding = FakeBinding(aliases={"owned_by": ("part_of", True)})
    _, result = run([part_of_fact(predicate="owned by")], binding)

    fact = result.facts[0]
    by_key = {e.key: e.surface_text for e in result.entities}
    assert fact.predicate == "part_of"
    # 'Acme owned by Northwind' means 'Acme part_of Northwind' with the
    # ends swapped by the alias, so Northwind must end up the subject.
    assert by_key[fact.subject_key] == "Northwind Group"
    assert by_key[fact.object_key] == "Acme Corp"


def test_swap_on_a_literal_fact_is_refused_rather_than_guessed():
    binding = FakeBinding(aliases={"owned_by": ("part_of", True)})
    _, result = run([part_of_fact(predicate="owned by", object_text=None,
                                  object_type=None, object_literal="x")],
                    binding)

    assert result.facts == []
    assert result.quarantined[0].reason == "validation_failure"


def test_one_entity_per_surface_and_type_across_facts():
    """Two facts about the same pair produce two facts and TWO entities,
    not four — identity within a document is core's rule, so no plugin has
    to implement it and none can implement it differently."""
    _, result = run([part_of_fact(),
                     part_of_fact(predicate="reports_to")])

    assert len(result.facts) == 2
    assert len(result.entities) == 2
    assert result.facts[0].subject_key == result.facts[1].subject_key


def test_conflicting_keys_for_one_surface_are_quarantined():
    """One surface cannot be two entities.

    Identity within a document is core's rule, and the rule used to be
    'whichever extracted_keys arrived first wins' — silently, with the later
    ones dropped. That is the wrong answer precisely because the surviving
    key is what the resolver blocks on: core would pick one arbitrarily and
    the corpus would carry the consequence with nothing recorded. It is also
    a contradiction no deterministic producer should ever emit, so refusing
    it turns 'plugins are consistent' from an assumption into a guarantee.
    """
    _, result = run([
        part_of_fact(subject_keys={"registry_id": "A-1"}),
        part_of_fact(subject_keys={"registry_id": "A-2"}),
    ])

    assert len(result.facts) == 1          # the first stands
    (q,) = result.quarantined
    assert q.reason == "validation_failure"
    assert "conflicting keys" in q.detail
    assert "A-1" in q.detail and "A-2" in q.detail


def test_extra_or_missing_keys_on_a_later_mention_are_fine():
    """Only a key present on BOTH sides with two DIFFERENT values is a
    contradiction. Facts about one thing legitimately arrive knowing
    different amounts about it, and treating that as a clash would
    quarantine ordinary emissions."""
    _, result = run([
        part_of_fact(subject_keys={"registry_id": "A-1"}),
        part_of_fact(predicate="reports_to", subject_keys={}),
        part_of_fact(subject_keys={"registry_id": "A-1", "tax_id": "99"}),
    ])

    assert not result.quarantined
    assert len(result.facts) == 3
    assert len({f.subject_key for f in result.facts}) == 1


def test_no_llm_touches_this_path():
    """Structural, not aspirational: the strategy holds a binding and a
    plugin and nothing else, so there is no client it could call. The
    plugin is the only thing that ran, and it ran on the document text."""
    strategy, _ = run([part_of_fact()])

    assert set(vars(strategy)) == {"binding", "plugin", "extractor", "version"}
    assert strategy.plugin.calls == [(len(TEXT), TEXT[:20])]


def test_stats_report_no_tokens():
    _, result = run([part_of_fact()])
    assert result.stats.prompt_tokens is None
    assert result.stats.output_tokens is None
    assert result.stats.repairs == 0
    assert result.stats.wall_ms is not None


# ======================================================== span verification
def test_correct_declared_span_grounds():
    grounder = SpanGrounder()
    verdict = grounder.verify_span(TEXT[SENTENCE_ONE:SENTENCE_ONE_END],
                                   SENTENCE_ONE, SENTENCE_ONE_END, TEXT)

    assert verdict.status == "declared_span"
    assert verdict.passed
    assert (verdict.char_start, verdict.char_end) == (SENTENCE_ONE,
                                                      SENTENCE_ONE_END)


def test_wrong_declared_span_is_caught():
    """The point of verifying instead of trusting. The plugin's arithmetic
    is off by a section and core says so."""
    grounder = SpanGrounder()
    verdict = grounder.verify_span("Acme Corp is part of Northwind Group.",
                                   SECTION_TWO, SECTION_TWO + 37, TEXT)

    assert verdict.status == "span_mismatch"
    assert not verdict.passed
    assert "producer named" in verdict.note


def test_span_outside_the_text_is_caught():
    verdict = SpanGrounder().verify_span("anything", 0, 10_000, TEXT)
    assert verdict.status == "span_mismatch"
    assert "outside the source text" in verdict.note


def test_offsets_with_no_declared_text_are_not_waved_through():
    """An unverifiable claim is reported as unverified. A plugin already
    has the text it sliced, so declaring it costs nothing."""
    verdict = SpanGrounder().verify_span("", SENTENCE_ONE, SENTENCE_ONE_END,
                                         TEXT)
    assert verdict.status == "span_mismatch"
    assert not verdict.passed


def test_reflowed_whitespace_still_grounds():
    """Right characters, different rendering. Failing this would flag a
    whole corpus for review over a newline."""
    declared = "Acme Corp is part of\nNorthwind   Group."
    verdict = SpanGrounder().verify_span(declared, SENTENCE_ONE,
                                         SENTENCE_ONE_END, TEXT)

    assert verdict.status == "declared_span"
    assert "normalization" in (verdict.note or "")


def test_half_declared_span_falls_back_to_no_span():
    """char_start without char_end is not half a provenance claim; it is
    no claim, and the strategy drops it rather than staging an offset that
    would slice the wrong text."""
    _, result = run([part_of_fact(char_end=None)])

    assert len(result.facts) == 1
    assert result.facts[0].char_start is None
    assert result.facts[0].char_end is None


def test_inverted_span_is_dropped():
    _, result = run([part_of_fact(char_start=100, char_end=50)])
    assert result.facts[0].char_start is None


# ====================================================== registry + boundary
def test_registered_short_name_builds():
    registry = plugins.PluginRegistry("fact_parser", FactParser)
    registry.register("fake", lambda: FakeFactParser())
    assert isinstance(registry.build("fake"), FactParser)
    assert registry.names() == ["fake"]


def test_reference_into_core_is_refused():
    """THE boundary guard. A 'plugin' inside knowledge_hub would be domain
    logic in the corpus-agnostic package, which is the one thing this seam
    exists to prevent — so it is refused where every plugin must pass."""
    registry = plugins.PluginRegistry("fact_parser", FactParser)
    with pytest.raises(plugins.BoundaryViolation, match="corpus-agnostic"):
        registry.build("knowledge_hub.extraction_parser_supplied:"
                       "ParserSuppliedStrategy")


def test_unknown_name_names_what_was_available():
    registry = plugins.PluginRegistry("parser", Parser)
    registry.register("docling", lambda: FakeParser())
    with pytest.raises(plugins.PluginError, match="docling"):
        registry.build("doclign")  # a typo, not a reference


def test_uninstallable_module_says_so():
    registry = plugins.PluginRegistry("fact_parser", FactParser)
    with pytest.raises(plugins.PluginError, match="not importable"):
        registry.build("no_such_plugin_package.parser:Thing")


def test_wrong_type_is_caught_at_build_not_at_first_call():
    registry = plugins.PluginRegistry("fact_parser", FactParser)
    registry.register("wrong", lambda: FakeParser())
    with pytest.raises(plugins.PluginError, match="does not implement"):
        registry.build("wrong")


# ========================================================= config selection
def test_absent_config_reproduces_the_old_routing():
    """The seam is opt-in. Every source that predates it must route exactly
    where it always did."""
    assert plugins.strategy_name_for({}, "prose") == plugins.LLM_STRATEGY
    assert plugins.strategy_name_for({}, "structured") == \
        plugins.STRUCTURED_STRATEGY


def test_config_overrides_the_track_derived_default():
    config = {"extraction_strategy": "parser_supplied"}
    assert plugins.strategy_name_for(config, "prose") == \
        plugins.PARSER_SUPPLIED_STRATEGY


def test_unknown_strategy_is_refused():
    with pytest.raises(plugins.PluginError, match="is not one of"):
        plugins.strategy_name_for({"extraction_strategy": "vibes"}, "prose")


def test_parser_defaults_to_docling():
    assert plugins.parser_ref_for({}) == plugins.DEFAULT_PARSER


# ============================================ text rebuild from chunk spans
def make_parent(document_id: int, seq: int, start: int, end: int,
                text: str = TEXT) -> Chunk:
    return Chunk(tenant_id="t", document_id=document_id,
                 level=ChunkLevel.parent, seq=seq, content=text[start:end],
                 content_hash=sha(f"{seq}:{start}"), char_start=start,
                 char_end=end, id=100 + seq)


def test_text_rebuild_preserves_every_offset():
    """Extraction has no Parser, so it rebuilds the text from the chunks it
    was cut from. The only property that matters: an offset valid against
    the parser's output is valid against the rebuild."""
    parents = [make_parent(1, 0, 0, SECTION_TWO),
               make_parent(1, 1, SECTION_TWO, len(TEXT))]
    rebuilt = document_text_from_chunks(parents)

    assert rebuilt == TEXT
    assert rebuilt[SENTENCE_ONE:SENTENCE_ONE_END] == \
        TEXT[SENTENCE_ONE:SENTENCE_ONE_END]


def test_text_rebuild_pads_a_dropped_blank_section():
    """SectionChunker drops whitespace-only sections. The gap is refilled so
    later characters keep their original index instead of sliding left."""
    parents = [make_parent(1, 0, 0, 14),
               make_parent(1, 1, SECTION_TWO, len(TEXT))]
    rebuilt = document_text_from_chunks(parents)

    assert len(rebuilt) == len(TEXT)
    assert rebuilt[SECTION_TWO:SECTION_TWO + 13] == "# Section Two"


def test_no_parents_rebuilds_to_empty():
    assert document_text_from_chunks([]) == ""


# ================================================= chunk anchoring of spans
def test_span_is_anchored_to_its_containing_parent():
    parents = [(0, SECTION_TWO, 100), (SECTION_TWO, len(TEXT), 101)]
    resolve = ExtractionService._chunk_for_span

    assert resolve(parents, SENTENCE_ONE, SENTENCE_ONE_END) == 100
    assert resolve(parents, SECTION_TWO + 2, SECTION_TWO + 8) == 101


def test_a_span_straddling_two_parents_stays_document_anchored():
    """A weaker citation that is true beats a precise one that is invented."""
    parents = [(0, SECTION_TWO, 100), (SECTION_TWO, len(TEXT), 101)]
    assert ExtractionService._chunk_for_span(
        parents, SECTION_TWO - 5, SECTION_TWO + 5) is None


def test_unverified_span_has_no_chunk():
    parents = [(0, SECTION_TWO, 100)]
    assert ExtractionService._chunk_for_span(parents, None, None) is None
