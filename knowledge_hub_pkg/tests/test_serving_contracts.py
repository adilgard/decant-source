"""Serving contracts (Build Prompt S1): pure-Pydantic validator tests.

No DB, no services — these tests pin the SHAPE guarantees S2–S5 build on:
the spine is required on both envelopes, a FactEnvelope cannot exist without
an uncertainty state, the relevant≠true separation is structurally enforced
(no truth-confidence on evidence, no similarity on facts), and the usage
instrumentation records exactly what was read/branched.
"""
from __future__ import annotations

import inspect

import pytest
from pydantic import ValidationError

from knowledge_hub.serving import (
    ChokePoint,
    EntityRef,
    EnvelopeUsage,
    EvidenceEnvelope,
    FactEnvelope,
    FilteredQuery,
    InMemoryUsageRecorder,
    Operation,
    OperationRegistry,
    ProvenanceSpine,
    RetrievalQuery,
    RetrievalService,
    RetrievalSignal,
    ServingResponse,
    ServingService,
    UncertaintyState,
    UnknownOperation,
    UsageTracker,
)

TENANT = "t-serving-contracts"


def spine(**over) -> ProvenanceSpine:
    base = dict(tenant_id=TENANT, document_id=7, chunk_id=42,
                char_start=10, char_end=55, security_label="public",
                security_label_id=1)
    base.update(over)
    return ProvenanceSpine(**base)


def fact_kwargs(**over) -> dict:
    base = dict(
        spine=spine(),
        fact_id=101,
        subject=EntityRef(entity_id=1, canonical_name="Acme GmbH",
                          entity_type="organization"),
        predicate="supplies",
        object_entity=EntityRef(entity_id=2, canonical_name="Diversified Botanics",
                                entity_type="organization"),
        state=UncertaintyState.known_confident,
        ontology_version="baseline-0.1",
        extractor="qwen3.6-joint",
        extractor_version="sha256:abc",
    )
    base.update(over)
    return base


def evidence_kwargs(**over) -> dict:
    base = dict(
        spine=spine(),
        content="Acme GmbH supplies Diversified Botanics with substrate.",
        signal=RetrievalSignal(score=0.83, rank=1, mode="dense",
                               query="who supplies substrate?"),
    )
    base.update(over)
    return base


# ---------------------------------------------------------------- the spine --
def test_spine_required_on_fact_envelope():
    kwargs = fact_kwargs()
    del kwargs["spine"]
    with pytest.raises(ValidationError):
        FactEnvelope(**kwargs)


def test_spine_required_on_evidence_envelope():
    kwargs = evidence_kwargs()
    del kwargs["spine"]
    with pytest.raises(ValidationError):
        EvidenceEnvelope(**kwargs)


def test_spine_char_span_is_both_or_neither():
    with pytest.raises(ValidationError):
        spine(char_start=10, char_end=None)
    with pytest.raises(ValidationError):
        spine(char_start=None, char_end=55)
    assert spine(char_start=None, char_end=None).char_start is None


def test_spine_char_span_ordering():
    with pytest.raises(ValidationError):
        spine(char_start=55, char_end=10)


def test_spine_carries_tenant_and_served_label():
    s = spine()
    assert s.tenant_id == TENANT
    assert s.security_label == "public"


def test_structured_fact_spine_has_no_chunk_but_a_locator():
    s = spine(chunk_id=None, char_start=None, char_end=None,
              locator={"sheet": "Q2", "row": 42, "col": "terms"})
    env = FactEnvelope(**fact_kwargs(spine=s))
    assert env.spine.chunk_id is None
    assert env.spine.locator["row"] == 42


# ------------------------------------------------------------- fact envelope --
def test_fact_envelope_requires_a_state():
    kwargs = fact_kwargs()
    del kwargs["state"]
    with pytest.raises(ValidationError):
        FactEnvelope(**kwargs)


def test_fact_envelope_has_no_similarity_or_rank_fields():
    for retrieval_field in ("score", "rank", "similarity", "signal"):
        assert retrieval_field not in FactEnvelope.model_fields
    # extra="forbid": smuggling one in is a hard error, not a silent extra.
    with pytest.raises(ValidationError):
        FactEnvelope(**fact_kwargs(), similarity=0.83)


def test_fact_envelope_requires_an_object():
    with pytest.raises(ValidationError):
        FactEnvelope(**fact_kwargs(object_entity=None, object_literal=None))
    env = FactEnvelope(**fact_kwargs(object_entity=None,
                                     object_literal="EUR 12000"))
    assert env.object_literal == "EUR 12000"


def test_fact_envelope_grounding_vocabulary():
    assert FactEnvelope(**fact_kwargs(grounding="pass")).grounding == "pass"
    assert FactEnvelope(**fact_kwargs()).grounding is None
    with pytest.raises(ValidationError):
        FactEnvelope(**fact_kwargs(grounding="vibes"))


def test_fact_envelope_temporal_current():
    assert FactEnvelope(**fact_kwargs()).is_current
    ended = FactEnvelope(**fact_kwargs(valid_to="2026-01-01T00:00:00Z"))
    assert not ended.is_current


def test_uncertainty_states_are_the_agreed_six():
    # The original five (S1), plus 'retracted' (migration 009): the temporal
    # state a valid_to-set fact serves under — reachable only through an
    # explicit include_retracted audit query. Growing this vocabulary is a
    # deliberate S1 contract change; this pin exists so it never happens by
    # accident.
    assert {s.value for s in UncertaintyState} == {
        "known_confident", "known_low_confidence", "under_review",
        "unresolved", "unknown", "retracted"}


# --------------------------------------------------------- evidence envelope --
def test_evidence_envelope_has_no_truth_confidence_field():
    for truth_field in ("confidence", "state", "grounding"):
        assert truth_field not in EvidenceEnvelope.model_fields
    with pytest.raises(ValidationError):
        EvidenceEnvelope(**evidence_kwargs(), confidence=0.9)
    with pytest.raises(ValidationError):
        EvidenceEnvelope(**evidence_kwargs(),
                         state=UncertaintyState.known_confident)


def test_evidence_envelope_requires_retrieval_signal():
    kwargs = evidence_kwargs()
    del kwargs["signal"]
    with pytest.raises(ValidationError):
        EvidenceEnvelope(**kwargs)


def test_evidence_envelope_requires_a_chunk():
    with pytest.raises(ValidationError):
        EvidenceEnvelope(**evidence_kwargs(
            spine=spine(chunk_id=None, char_start=None, char_end=None)))


def test_evidence_grounded_facts_default_empty():
    env = EvidenceEnvelope(**evidence_kwargs())
    assert env.grounded_facts == []
    enriched = EvidenceEnvelope(**evidence_kwargs(
        grounded_facts=[FactEnvelope(**fact_kwargs())]))
    assert enriched.grounded_facts[0].state is UncertaintyState.known_confident


def test_envelopes_are_distinct_types_not_a_union():
    assert not issubclass(FactEnvelope, EvidenceEnvelope)
    assert not issubclass(EvidenceEnvelope, FactEnvelope)


# ------------------------------------------------------------------- seams --
def test_serving_abcs_are_abstract():
    for abc in (ChokePoint, OperationRegistry, RetrievalService,
                ServingService):
        with pytest.raises(TypeError):
            abc()  # type: ignore[abstract]


def test_retrieve_signature_has_enrich_keyword_only():
    """`enrich` is the ONE knob (Decision 2c) — keyword-only, default off
    (bare-fast). The `bare` context-stripping knob was DROPPED as
    speculative: context fields are default-on, not parameters."""
    sig = inspect.signature(RetrievalService.retrieve)
    param = sig.parameters["enrich"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is False
    assert "bare" not in sig.parameters


def test_filtered_query_is_proof_of_enforcement():
    assert issubclass(FilteredQuery, RetrievalQuery)
    bare = RetrievalQuery(text="who supplies substrate?")
    assert not isinstance(bare, FilteredQuery)
    enforced = FilteredQuery(text=bare.text, tenant_id=TENANT,
                             principal_id="agent-7", allowed_label_ids=[1])
    assert enforced.allowed_label_ids == [1]


def test_operation_returns_vocabulary():
    op = Operation(name="facts_about", description="Facts about one entity",
                   returns="facts")
    assert op.returns == "facts"
    with pytest.raises(ValidationError):
        Operation(name="bad", description="", returns="text")


def test_unknown_operation_carries_where_never_payload():
    err = UnknownOperation(TENANT, "no_such_op")
    assert TENANT in str(err) and "no_such_op" in str(err)


def test_serving_response_wraps_both_envelope_kinds():
    resp = ServingResponse(request_id="req-1", tenant_id=TENANT,
                           operation="facts_about",
                           facts=[FactEnvelope(**fact_kwargs())],
                           evidence=[EvidenceEnvelope(**evidence_kwargs())])
    assert resp.facts[0].fact_id == 101
    assert resp.evidence[0].signal.mode == "dense"


# --------------------------------------------------------- instrumentation --
def test_usage_tracker_records_fields_read_and_states_branched():
    recorder = InMemoryUsageRecorder()
    fact = FactEnvelope(**fact_kwargs())
    with UsageTracker(recorder, request_id="req-1", tenant_id=TENANT) as t:
        tracked = t.track(fact)
        _ = tracked.predicate
        if tracked.state is UncertaintyState.known_confident:
            _ = tracked.subject.canonical_name

    assert len(recorder.records) == 1
    usage = recorder.records[0]
    assert usage.envelope_kind == "fact"
    assert usage.envelope_key == "fact:101"
    assert usage.fields_read == ["predicate", "state", "subject"]
    assert usage.states_branched == ["known_confident"]
    # The strip signal: what was NEVER read is what may be stripped.
    assert "valid_from" not in usage.fields_read
    assert "confidence" not in usage.fields_read


def test_usage_tracker_evidence_key_and_field_counts():
    recorder = InMemoryUsageRecorder()
    tracker = UsageTracker(recorder, request_id="req-2", tenant_id=TENANT)
    for _ in range(2):
        tracked = tracker.track(EvidenceEnvelope(**evidence_kwargs()))
        _ = tracked.content
    tracker.flush()

    assert [r.envelope_key for r in recorder.records] == ["chunk:42", "chunk:42"]
    counts = recorder.field_read_counts("evidence")
    assert counts == {"content": 2}
    assert recorder.field_read_counts("fact") == {}


def test_tracked_envelope_delegates_without_recording_non_fields():
    recorder = InMemoryUsageRecorder()
    tracker = UsageTracker(recorder, request_id="req-3", tenant_id=TENANT)
    tracked = tracker.track(FactEnvelope(**fact_kwargs()))
    dumped = tracked.model_dump()          # method access: delegated, unrecorded
    assert dumped["fact_id"] == 101
    assert tracked.is_current              # property: delegated, unrecorded
    usage = tracker.flush()[0]
    assert usage.fields_read == []
    assert usage.states_branched == []


def test_envelope_usage_kind_vocabulary():
    with pytest.raises(ValidationError):
        EnvelopeUsage(request_id="r", tenant_id=TENANT, envelope_kind="blob",
                      envelope_key="x", fields_read=[], states_branched=[])
