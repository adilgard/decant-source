"""parser_supplied END TO END on the real stack: capture -> process ->
extract -> resolve -> promote, driven by a registered plugin.

The unit tests beside this one (test_parser_supplied.py) prove the pieces.
This proves the wiring, which is a different claim and the one the build
prompt actually asked for: a source config value reaches the router, the
router builds the right strategy, a plugin's output survives the ontology
gate, its computed offsets survive verification, and the resulting facts
promote and are citable — with a REAL Docling parse, REAL bge-m3
embeddings, and a REAL Postgres underneath.

What must hold, end to end:

* CONFIG ROUTES. Nothing here calls the parser_supplied path directly. A
  `source_registry` row says `extraction_strategy: parser_supplied` and
  names a plugin, and that is the only reason it runs.
* THE LLM NEVER RUNS. The service is wired with a real LLM strategy that
  counts its calls. For this source the count stays zero. Determinism here
  is structural, not a claim in a docstring.
* PROSE STILL CHUNKS AND EMBEDS. The document is fully retrievable —
  parents, children, and real vectors — because only the fact producer
  changed. This is the half of the decision that is easy to break and
  invisible if untested.
* SPANS SURVIVE THE ROUND TRIP. The plugin computes offsets against the
  text it was handed; the flow rebuilds that text from persisted chunks and
  verifies by slicing. Facts land grounded 'declared_span', and slicing the
  DOCUMENT's own chunk at the stored offsets returns the claimed sentence.
* THE GATE HOLDS THROUGH THE WHOLE PIPELINE. An out-of-allowlist emission
  from the plugin reaches `quarantined_extractions`, not `pending_facts`,
  with the plugin's raw output attached.
* FACTS PROMOTE AND STAY CITABLE. After the resolver sweeps, real `facts`
  rows exist carrying the plugin's provenance and a chunk anchor.
"""
from __future__ import annotations

import uuid

import pytest

from factories import ONTOLOGY

from knowledge_hub.capture import CaptureService
from knowledge_hub.extraction import ExtractionService
from knowledge_hub.interfaces import FactParser, ParsedFact
from knowledge_hub.plugins import FACT_PARSERS
from knowledge_hub.sources_fs import FilesystemSourceAdapter

PLUGIN_NAME = "e2e_demo"
PLUGIN_REF = "e2e_demo_fact_parser"

# One prose document. Deliberately ordinary: the seam is corpus-agnostic and
# this file must not need a domain to make its point.
DOC = b"""# Site Overview

Granite Works is part of Northwind Holdings.

# Reporting

Granite Works reports to Northwind Holdings for quarterly figures.
"""

EDGE = "Granite Works is part of Northwind Holdings."
BOGUS = "Granite Works reports to Northwind Holdings for quarterly figures."


class E2EFactParser(FactParser):
    """Finds two assertions and computes their offsets against the text it
    is handed — the realistic shape, since a real parser knows where things
    are rather than quoting them.

    The second emission uses a predicate the baseline ontology does not
    contain, on purpose: a plugin that never emits anything off-vocabulary
    would not prove the gate does anything.
    """

    name = PLUGIN_NAME
    version = "1.0.0"

    def __init__(self) -> None:
        self.seen: list[int] = []

    def parse_facts(self, document, text, content):
        self.seen.append(len(text))
        out: list[ParsedFact] = []

        start = text.find(EDGE)
        if start >= 0:
            out.append(ParsedFact(
                subject_text="Granite Works", subject_type="Organization",
                predicate="part_of",
                object_text="Northwind Holdings",
                object_type="Organization",
                char_start=start, char_end=start + len(EDGE),
                span_text=text[start:start + len(EDGE)],
                subject_char_start=start,
                subject_char_end=start + len("Granite Works"),
                locator={"section": "Site Overview"}))

        bogus = text.find(BOGUS)
        if bogus >= 0:
            out.append(ParsedFact(
                subject_text="Granite Works", subject_type="Organization",
                predicate="secretly_controls",       # not in the ontology
                object_text="Northwind Holdings",
                object_type="Organization",
                char_start=bogus, char_end=bogus + len(BOGUS),
                span_text=text[bogus:bogus + len(BOGUS)]))
        return out


class CountingStrategy:
    """Wraps the real LLM strategy and refuses to be silent about being
    called. Asserting 'no model ran' by inspecting output would prove
    nothing; this proves the call never happened."""

    def __init__(self, inner):
        self._inner = inner
        self.extractor = inner.extractor
        self.version = inner.version
        self.calls = 0

    def extract(self, unit):
        self.calls += 1
        return self._inner.extract(unit)


@pytest.fixture(scope="module")
def e2e(store, pipeline, raw_store, dispatcher, parser, chunker, embedder,
        binding, llm_strategy, structured_strategy, grounder,
        extraction_dispatcher, scorer, tmp_path_factory):
    """Run the whole slice once through the plugin; tests below read the
    residue, same shape as test_full_slice."""
    from knowledge_hub.processing import ProcessingService
    from knowledge_hub.resolution import ResolutionService

    plugin = E2EFactParser()
    FACT_PARSERS.register(PLUGIN_REF, lambda: plugin)

    tenant = f"t-{uuid.uuid4().hex[:12]}"
    root = tmp_path_factory.mktemp("ps-e2e")
    (root / "overview.md").write_bytes(DOC)

    capture = CaptureService(pipeline, raw_store, dispatcher)
    # THE ONLY REASON THE PLUGIN RUNS. No call site below names it.
    capture.registry.register(tenant, "fs-ps", "filesystem",
                              config={"data_track": "prose",
                                      "doc_type": "prose",
                                      "extraction_strategy": "parser_supplied",
                                      "fact_parser": PLUGIN_REF})
    landed = capture.run_source(
        tenant, FilesystemSourceAdapter(source_ref="fs-ps", root=root),
        mode="backfill")
    assert landed.landed == 1

    processing = ProcessingService(
        pipeline, raw_store, parser, chunker, embedder,
        dispatcher=dispatcher, extraction_dispatcher=extraction_dispatcher)
    processed = processing.consume(tenant, limit=10)
    assert len(processed) == 1 and processed[0].status == "processed"

    counting = CountingStrategy(llm_strategy)
    extraction = ExtractionService(
        pipeline, raw_store, binding, counting, structured_strategy,
        grounder, dispatcher=extraction_dispatcher)
    extracted = extraction.consume(tenant, limit=10)
    assert len(extracted) == 1, extracted

    resolution = ResolutionService(pipeline, scorer, embedder)
    summary = resolution.sweep(tenant)
    return {"tenant": tenant, "processed": processed[0],
            "extracted": extracted[0], "llm": counting, "plugin": plugin,
            "summary": summary}


def rows(store, tenant: str, table: str) -> list[dict]:
    with store.transaction(tenant) as conn:
        return conn.execute(
            f"SELECT * FROM {table} WHERE tenant_id = %s ORDER BY id",
            (tenant,)).fetchall()


# ---------------------------------------------------------------------------
def test_config_alone_routed_the_document_to_the_plugin(e2e):
    assert e2e["extracted"].status == "extracted"
    assert e2e["plugin"].seen, "the plugin was never handed a document"


def test_the_language_model_never_ran(e2e):
    """The determinism claim, structurally. A real LLM strategy was wired
    into the service and its call count is zero."""
    assert e2e["llm"].calls == 0


def test_prose_was_still_chunked_and_embedded(e2e, store):
    """Facts changed producer; retrieval did not change at all."""
    result = e2e["processed"]
    assert result.parents >= 1 and result.children >= 1
    children = [c for c in rows(store, e2e["tenant"], "chunks")
                if c["level"] == "child"]
    assert children, "no child chunks — the document is not retrievable"
    assert all(c["embedding"] is not None for c in children)
    assert all(c["embedding_model"] == "bge-m3" for c in children)


def test_the_permitted_fact_staged_with_a_verified_span(e2e, store):
    staged = rows(store, e2e["tenant"], "pending_facts")
    assert len(staged) == 1, "exactly the in-vocabulary emission should stage"
    fact = staged[0]
    assert fact["predicate"] == "part_of"
    assert fact["grounding"] == "declared_span"   # sliced and compared
    assert fact["needs_review"] is False
    assert fact["ontology_version"] == ONTOLOGY
    # Provenance names the PLUGIN, not just the seam it arrived through.
    assert fact["extractor"] == f"parser_supplied:{PLUGIN_NAME}"
    assert fact["extractor_version"] == "1.0.0"
    assert fact["locator"] == {"section": "Site Overview"}


def test_the_stored_offsets_still_point_at_the_sentence(e2e, store):
    """The whole point of span provenance: the numbers must survive the
    round trip through the database and still slice correctly."""
    fact = rows(store, e2e["tenant"], "pending_facts")[0]
    parents = [c for c in rows(store, e2e["tenant"], "chunks")
               if c["level"] == "parent"]
    holder = next(c for c in parents if c["id"] == fact["source_chunk_id"])
    local_start = fact["char_start"] - holder["char_start"]
    local_end = fact["char_end"] - holder["char_start"]
    assert holder["content"][local_start:local_end] == EDGE


def test_the_fact_is_chunk_anchored_so_retrieval_can_cite_it(e2e, store):
    """facts_citing joins on source_chunk_id. A document-anchored fact
    would be invisible to retrieval enrichment."""
    fact = rows(store, e2e["tenant"], "pending_facts")[0]
    assert fact["source_chunk_id"] is not None
    assert fact["source_document_id"] is not None


def test_the_off_vocabulary_emission_was_quarantined_not_staged(e2e, store):
    """The gate, exercised through the entire pipeline rather than against
    a hand-built unit."""
    staged = rows(store, e2e["tenant"], "pending_facts")
    assert all(f["predicate"] != "secretly_controls" for f in staged)

    quarantined = rows(store, e2e["tenant"], "quarantined_extractions")
    assert len(quarantined) == 1
    q = quarantined[0]
    assert q["reason"] == "unbound_predicate"
    assert q["detail"] == "secretly_controls"
    assert q["extractor"] == f"parser_supplied:{PLUGIN_NAME}"
    # The raw emission is kept — the signal that grows an ontology.
    assert q["raw_output"]["parsed_fact"]["predicate"] == "secretly_controls"


def test_facts_promoted_and_kept_the_plugin_provenance(e2e, store):
    facts = rows(store, e2e["tenant"], "facts")
    assert len(facts) == 1
    fact = facts[0]
    assert fact["predicate"] == "part_of"
    assert fact["extractor"] == f"parser_supplied:{PLUGIN_NAME}"
    assert fact["source_chunk_id"] is not None
    assert fact["subject_entity_id"] and fact["object_entity_id"]
    assert fact["subject_entity_id"] != fact["object_entity_id"]


def test_the_run_is_on_the_ledger_and_replays_as_a_no_op(e2e, store,
                                                         pipeline, raw_store,
                                                         binding,
                                                         llm_strategy,
                                                         structured_strategy,
                                                         grounder):
    """Re-extracting the same document under the same plugin version must
    stage nothing new — the idempotency ledger keys on the plugin name and
    version, which is exactly why a plugin UPGRADE would be fresh work."""
    tenant = e2e["tenant"]
    runs = rows(store, tenant, "extraction_runs")
    assert [r["extractor"] for r in runs] == [f"parser_supplied:{PLUGIN_NAME}"]

    before = len(rows(store, tenant, "pending_facts"))
    raw_id = e2e["processed"].raw_document_id
    again = ExtractionService(pipeline, raw_store, binding, llm_strategy,
                              structured_strategy, grounder).extract(tenant,
                                                                     raw_id)
    assert again.status == "replayed"
    assert len(rows(store, tenant, "pending_facts")) == before
