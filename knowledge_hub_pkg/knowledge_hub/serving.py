"""Serving contracts (Build Prompt S1): envelopes, uncertainty states, seams.

This module is to the serving layer what models.py + interfaces.py are to
ingestion: the shapes everything else returns and the ABCs everything else
implements. S2 (choke point), S3 (operations), S4 (retrieval), S5 (API
surface) all depend on THIS module and add no new response shapes.

Two envelopes over one spine — and the distinction is TYPED, not documented:

  * FactEnvelope     — an assertion the agent may ACT on: a resolved triple
                       with an uncertainty STATE, temporal validity, lineage,
                       and a grounding verdict. It has no similarity score —
                       a fact is not "0.83 relevant", it is asserted.
  * EvidenceEnvelope — relevant TEXT, not asserted truth: a chunk plus the
                       retrieval signal that surfaced it. It has no
                       confidence-of-truth field — retrieval relevance is a
                       statement about the QUERY, never about the world.

Both are `extra="forbid"`, so the missing field on each side is structurally
unconstructable, not merely absent by convention. They are distinct types,
never a union: a call site that wants to act must hold a FactEnvelope.

Both carry the shared spine: tenant + the provenance triple (document_id ->
chunk_id -> char span, plus the structured-track locator) + the
security_label the item was SERVED under. Nothing leaves the hub without a
pointer back to the exact bytes it came from.

Uncertainty is a first-class STATE (UncertaintyState), over-provisioned by
design — five states now, folded later on evidence (the usage logs below),
never grown ad hoc. Numeric `confidence` rides along but is PROVISIONAL:
uncalibrated until the Axis B (ER) and Axis D (extraction) benchmarks
calibrate it, so the discrete state is the primary trust signal.

THE ABSENCE RULE: absence is never "false" and never "unknown". An item the
caller isn't permitted to see is filtered out silently by the choke point
(S2) — logically BEFORE uncertainty states apply. The states describe only
the knowledge status of things the caller IS allowed to see; `unknown` means
"the hub has no assertion either way", never "there might be something you
can't read".

Instrumentation (Decision 4a/4b): envelopes are served MAXIMAL now and
stripped LATER, on evidence. UsageTracker/EnvelopeUsage record which fields
each request actually read and which states it branched on — a field is
stripped only when the logs show non-use, never on intuition.

Keep this file in lock-step with models.py and the schema (same commit,
always): the spine mirrors the facts/chunks provenance columns, grounding
mirrors pending_facts.grounding (migration 004; joined to promoted facts via
promoted_fact_id), and security labels mirror the security_labels /
label_role_grants reference tables.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from enum import Enum
from typing import Any, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

# =============================================================================
# Uncertainty states (Deliverable 2) — the primary trust signal
# =============================================================================

class UncertaintyState(str, Enum):
    """Discrete knowledge status of one served fact.

    Over-provisioned by design: rarely-used states get FOLDED TOGETHER later
    when the usage logs show a branch nobody takes — cheaper than discovering
    a missing state after agents depend on the vocabulary.

    * known_confident      — resolved, grounded, high-band: act on it.
    * known_low_confidence — asserted but weakly supported (grounding flag,
                             low extraction confidence, gray-band ER):
                             act with corroboration.
    * under_review         — a human currently owns it (review_queue:
                             needs_review, quarantine, gray-band candidate);
                             served so the agent can say "being verified",
                             not so it can act.
    * unresolved           — the assertion exists but identity isn't settled
                             (still a pending_fact; refs not canonical).
    * unknown              — the hub has no assertion either way. Explicitly
                             NOT "false", and NOT "hidden": permission
                             filtering already happened, silently, before
                             states were assigned (the absence rule).
    * retracted            — the assertion is no longer current: valid_to is
                             set (its source document was deleted and the
                             retraction propagated, or — future — it was
                             superseded by a newer version). Served ONLY
                             under an explicit include_retracted audit
                             query; the default serve path never returns it.
                             A TEMPORAL state, never a permission one:
                             permission-filtered items are silently absent,
                             retracted items are honestly labeled when
                             explicitly asked for.
    """
    known_confident = "known_confident"
    known_low_confidence = "known_low_confidence"
    under_review = "under_review"
    unresolved = "unresolved"
    unknown = "unknown"
    retracted = "retracted"


# Mirrors pending_facts.grounding (migration 004) — the vocabulary a served
# fact's grounding verdict is drawn from (via promoted_fact_id join).
GROUNDING_STATUSES = ("pass", "span_missing", "components_missing",
                      "construction")


# =============================================================================
# The shared spine (Deliverable 1) — provenance + served-under label
# =============================================================================

class ProvenanceSpine(BaseModel):
    """What BOTH envelopes carry: tenant, the provenance triple, and the
    security label the item was served under.

    document_id -> chunk_id -> (char_start, char_end): chunk_id is None for
    structured-track facts (SoR rows, form fields produce facts with no
    chunk — `locator` carries {"sheet","row","col"} instead); the serving
    layer always resolves document_id, even when the fact row only recorded
    a chunk. Char offsets anchor into the DOCUMENT's extracted text.

    `security_label` is the resolved label TEXT (a NULL security_label_id
    serves as 'public', the seeded default) — recorded so an audit can
    replay exactly what label the item passed the choke point under."""
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    document_id: int
    chunk_id: Optional[int] = None
    char_start: Optional[int] = None
    char_end: Optional[int] = None
    locator: Optional[dict[str, Any]] = None
    security_label: str
    security_label_id: Optional[int] = None

    @model_validator(mode="after")
    def _check_span(self) -> "ProvenanceSpine":
        if (self.char_start is None) != (self.char_end is None):
            raise ValueError("char span must be both-or-neither")
        if self.char_start is not None and self.char_end < self.char_start:
            raise ValueError("char_end must be >= char_start")
        return self


class EntityRef(BaseModel):
    """A resolved canonical entity as it appears inside a FactEnvelope:
    enough to act on (id) and to display (name/type) without a second
    round-trip. The full registry row stays behind an operation."""
    model_config = ConfigDict(extra="forbid")

    entity_id: int
    canonical_name: str
    entity_type: str


# =============================================================================
# The two envelopes (Deliverable 1) — distinct types, never a union
# =============================================================================

class FactEnvelope(BaseModel):
    """An assertion the agent may ACT on.

    The discrete `state` is the primary trust signal and is REQUIRED — an
    envelope cannot be built without taking a position on how settled the
    knowledge is. `confidence` is PROVISIONAL: uncalibrated until Axes B/D
    calibrate it; treat it as ordering-within-a-state at most, never as a
    probability.

    No similarity/rank/score field exists here, and extra="forbid" keeps it
    that way: relevant ≠ true, and a fact is not "relevant" — it is asserted
    (or it isn't served)."""
    model_config = ConfigDict(extra="forbid")

    spine: ProvenanceSpine
    fact_id: int

    # The resolved triple. Mirrors facts.chk_object_present: entity object
    # OR literal object (both allowed, neither is not).
    subject: EntityRef
    predicate: str
    object_entity: Optional[EntityRef] = None
    object_literal: Optional[str] = None
    attributes: dict[str, Any] = Field(default_factory=dict)

    # Trust signals: state first, number second.
    state: UncertaintyState
    confidence: Optional[float] = None  # PROVISIONAL until Axes B/D calibrate

    # Temporal validity: valid_to=None means current.
    valid_from: Optional[datetime] = None
    valid_to: Optional[datetime] = None

    # Lineage: which vocabulary and which extractor stand behind this.
    ontology_version: str
    extractor: str
    extractor_version: str

    # Grounding verdict (pending_facts.grounding vocabulary), None when the
    # fact was never span-grounded.
    grounding: Optional[str] = None

    @model_validator(mode="after")
    def _check_object_and_grounding(self) -> "FactEnvelope":
        if self.object_entity is None and self.object_literal is None:
            raise ValueError(
                "fact envelope must have object_entity OR object_literal")
        if self.grounding is not None and self.grounding not in GROUNDING_STATUSES:
            raise ValueError(
                f"grounding must be one of {GROUNDING_STATUSES} or None")
        return self

    @property
    def is_current(self) -> bool:
        return self.valid_to is None


class RetrievalSignal(BaseModel):
    """Why this text surfaced: a statement about the QUERY, never about the
    world. `score` is retrieval relevance (cosine similarity for the pilot's
    dense mode; rank-fusion score if a fused mode ever ships) — it must never
    be presented to a caller as confidence-of-truth."""
    model_config = ConfigDict(extra="forbid")

    score: float
    rank: Optional[int] = None          # 1-based position in the result list
    mode: str                           # e.g. 'dense' (the pilot mode, Axis C)
    query: str                          # the query text that surfaced it


class EvidenceEnvelope(BaseModel):
    """Relevant TEXT, not asserted truth.

    Carries the chunk (the child/cite unit) plus the retrieval signal that
    surfaced it. There is NO confidence-of-truth field and extra="forbid"
    keeps it that way — if a caller wants something to act on, it takes the
    grounded_facts (each a full FactEnvelope with its own state) or calls a
    fact operation; the evidence text itself asserts nothing.

    `grounded_facts` is populated ONLY when the caller opts into enrichment
    (RetrievalService.retrieve(..., enrich=True)); the default is always []."""
    model_config = ConfigDict(extra="forbid")

    spine: ProvenanceSpine
    content: str
    contextual_prefix: Optional[str] = None  # context fields are default-on (S4)

    # Document/section ref for display; spine.document_id is the pointer.
    document_title: Optional[str] = None
    section: Optional[str] = None

    signal: RetrievalSignal
    source_timestamp: Optional[datetime] = None

    # Opt-in enrichment ONLY — default stays empty.
    grounded_facts: list[FactEnvelope] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_chunk_provenance(self) -> "EvidenceEnvelope":
        # Evidence IS chunk text; a chunk-less evidence envelope is a bug.
        if self.spine.chunk_id is None:
            raise ValueError("evidence envelope requires spine.chunk_id")
        return self


# =============================================================================
# Serving seams (Deliverable 3) — S2..S5 implement, nothing else defines shapes
# =============================================================================

class Principal(BaseModel):
    """Who is asking: the tenant plus the caller's identity and roles. Roles
    are the label_role_grants vocabulary — the choke point joins them against
    security labels; the serving layer never re-derives them per request."""
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    principal_id: str                   # user or agent identity from the IdP
    roles: list[str]


class RetrievalQuery(BaseModel):
    """A retrieval request BEFORE enforcement. Carries no tenant and no
    label predicates — those are the choke point's to attach, nobody
    else's.

    `include_retracted` is the audit/history escape hatch on the TEMPORAL
    axis: the choke point's {cur:} predicate defaults to current-only
    (valid_to IS NULL) and this flag — carried into the tamper-proof
    enforcement snapshot — widens it to include retracted items, which then
    serve honestly as state='retracted'. Orthogonal to permissions: the
    {sec:} predicates apply identically either way."""
    text: str
    mode: str = "dense"                 # the pilot mode (Axis C decision)
    k: int = 10
    filters: dict[str, Any] = Field(default_factory=dict)
    include_retracted: bool = False     # temporal audit escape, never default


class FilteredQuery(RetrievalQuery):
    """A retrieval request AFTER enforcement — proof-of-passage, only ever
    constructed by ChokePoint.enforce. Index implementations (S4) accept
    THIS type and refuse a bare RetrievalQuery, so 'forgot the permission
    filter' is a type error, not an incident."""
    tenant_id: str
    principal_id: str
    allowed_label_ids: list[int]        # the labels this principal may see


class ChokePoint(ABC):
    """THE single mandatory permission gate (S2). Every serving path goes
    through enforce() — there is no label-filtering logic anywhere else.

    Enforcement happens logically BEFORE uncertainty states: an item the
    principal may not see is dropped silently (the absence rule), it never
    becomes 'unknown' or 'false'. Implementations resolve the principal's
    roles against label_role_grants and attach the allowed label set as a
    mandatory predicate."""

    @abstractmethod
    def enforce(self, query: RetrievalQuery, principal: Principal) -> FilteredQuery:
        """Return the query with the principal's mandatory label predicates
        attached. Never raises on 'no access' — a principal with no grants
        gets a FilteredQuery whose allowed set yields nothing."""


class UnknownOperation(Exception):
    """No such operation is registered for this tenant. Carries WHERE, never
    payload."""

    def __init__(self, tenant_id: str, name: str):
        self.tenant_id, self.name = tenant_id, name
        super().__init__(
            f"unknown operation {name!r} (tenant {tenant_id!r})")


# 'composite' arrived with S3: a fixed declared plan over already-registered
# operations. Its result preserves per-step envelopes (facts as facts,
# evidence as evidence, tagged by step) — see operations.CompositeResult.
OPERATION_RETURNS = ("facts", "evidence", "composite")


class Operation(BaseModel):
    """One declarative serving operation (S3): DATA, not code. Adding an
    operation is a registry write, not a deploy — the spec says what it's
    called, what params it takes, and which envelope type it returns; the
    executor behind the registry interprets it."""
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    returns: str                        # 'facts' | 'evidence'
    params: dict[str, Any] = Field(default_factory=dict)  # declarative param spec
    notes: Optional[str] = None

    @model_validator(mode="after")
    def _check_returns(self) -> "Operation":
        if self.returns not in OPERATION_RETURNS:
            raise ValueError(f"returns must be one of {OPERATION_RETURNS}")
        return self


class OperationRegistry(ABC):
    """Per-tenant operation catalog (S3). The registry answers 'what can be
    asked here' — the ServingService consults it before executing anything,
    so an unregistered ask fails closed with UnknownOperation."""

    @abstractmethod
    def register(self, tenant_id: str, op: Operation) -> None:
        """Register (or replace, by name) an operation for this tenant."""

    @abstractmethod
    def get(self, tenant_id: str, name: str) -> Operation:
        """The named operation. Raises UnknownOperation."""

    @abstractmethod
    def list_ops(self, tenant_id: str) -> list[Operation]:
        """Every operation registered for this tenant, name order."""


class RetrievalService(ABC):
    """Evidence retrieval (S4): query -> enforced -> ranked EvidenceEnvelopes.

    Implementations call the ChokePoint FIRST and search only under the
    resulting FilteredQuery — retrieval never sees unfiltered scope.

    The ONE knob is `enrich`. enrich=False (default): grounded_facts stays
    [] — bare-fast. enrich=True populates each envelope's grounded_facts
    with the FactEnvelopes whose provenance lands in that chunk — the ONLY
    way evidence carries facts. Context fields (contextual_prefix, title,
    section) are default-on, part of the envelope: a `bare` context-stripping
    knob was considered and DROPPED as speculative — add one only if a
    measured payload/latency need appears in the usage logs."""

    @abstractmethod
    def retrieve(self, query: RetrievalQuery, principal: Principal, *,
                 enrich: bool = False) -> list[EvidenceEnvelope]:
        """Ranked evidence for `query`, best first, permission-filtered."""


class ServingResponse(BaseModel):
    """What one served request returns: envelopes plus enough identity to
    audit it (request_id keys the usage log below)."""
    model_config = ConfigDict(extra="forbid")

    request_id: str
    tenant_id: str
    operation: str
    facts: list[FactEnvelope] = Field(default_factory=list)
    evidence: list[EvidenceEnvelope] = Field(default_factory=list)
    served_at: Optional[datetime] = None
    wall_ms: Optional[int] = None


class ServingService(ABC):
    """The API surface (S5): the ONLY doorway agents/callers talk to. It
    resolves the operation (S3), enforces (S2), retrieves/looks up (S4),
    wraps envelopes, and flushes usage records (below) — one request_id
    across all of it."""

    @abstractmethod
    def execute(self, operation: str, params: Mapping[str, Any],
                principal: Principal) -> ServingResponse:
        """Execute a registered operation. Raises UnknownOperation; permission
        misses never raise — they silently narrow the result (absence rule)."""

    @abstractmethod
    def operations(self, principal: Principal) -> list[Operation]:
        """The operations this principal's tenant may call."""


# =============================================================================
# Envelope-usage instrumentation (Deliverable 4) — the strip-later evidence
# =============================================================================

ENVELOPE_KINDS = ("fact", "evidence")


class EnvelopeUsage(BaseModel):
    """One envelope's observed usage in one served request: which fields the
    caller actually read, and which uncertainty states it branched on. This
    is the Decision 4a/4b mechanism — serve maximal now, strip a field ONLY
    when these records show non-use, fold a state only when nobody branches
    on it."""
    model_config = ConfigDict(extra="forbid")

    request_id: str
    tenant_id: str
    envelope_kind: str                  # 'fact' | 'evidence'
    envelope_key: str                   # 'fact:<id>' | 'chunk:<id>'
    fields_read: list[str]              # sorted, deduplicated
    states_branched: list[str]          # state VALUES observed via .state

    @model_validator(mode="after")
    def _check_kind(self) -> "EnvelopeUsage":
        if self.envelope_kind not in ENVELOPE_KINDS:
            raise ValueError(f"envelope_kind must be one of {ENVELOPE_KINDS}")
        return self


class UsageRecorder(ABC):
    """Where usage records go (S5 wires the durable one). Implementations
    must be fire-and-forget cheap — instrumentation may never slow serving."""

    @abstractmethod
    def record(self, usage: EnvelopeUsage) -> None:
        """Persist one usage record."""


class InMemoryUsageRecorder(UsageRecorder):
    """Process-local recorder: the test double AND the aggregation shape the
    strip decision reads (field_read_counts answers 'has anyone read this
    field, ever')."""

    def __init__(self) -> None:
        self.records: list[EnvelopeUsage] = []

    def record(self, usage: EnvelopeUsage) -> None:
        self.records.append(usage)

    def field_read_counts(self, envelope_kind: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for rec in self.records:
            if rec.envelope_kind != envelope_kind:
                continue
            for field in rec.fields_read:
                counts[field] = counts.get(field, 0) + 1
        return counts


class TrackedEnvelope:
    """Read-through proxy over one envelope: every model-field access is
    recorded structurally (no caller cooperation needed); reading `state`
    also records the VALUE observed — the branch evidence. Non-field
    attributes (model_dump, properties, ...) delegate untouched."""

    __slots__ = ("_env", "_fields_read", "_states_branched")

    def __init__(self, env: FactEnvelope | EvidenceEnvelope):
        object.__setattr__(self, "_env", env)
        object.__setattr__(self, "_fields_read", set())
        object.__setattr__(self, "_states_branched", [])

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._env, name)
        if name in type(self._env).model_fields:
            self._fields_read.add(name)
            if isinstance(value, UncertaintyState):
                self._states_branched.append(value.value)
        return value


class UsageTracker:
    """Per-request instrumentation scope (S5 opens one per request_id):
    wrap every envelope before handing it to the caller, flush on the way
    out. Context-manager use flushes automatically."""

    def __init__(self, recorder: UsageRecorder, request_id: str,
                 tenant_id: str):
        self.recorder = recorder
        self.request_id = request_id
        self.tenant_id = tenant_id
        self._tracked: list[tuple[str, str, TrackedEnvelope]] = []

    def track(self, env: FactEnvelope | EvidenceEnvelope) -> TrackedEnvelope:
        """Wrap one envelope; the returned proxy is what the caller gets."""
        if isinstance(env, FactEnvelope):
            kind, key = "fact", f"fact:{env.fact_id}"
        else:
            kind, key = "evidence", f"chunk:{env.spine.chunk_id}"
        proxy = TrackedEnvelope(env)
        self._tracked.append((kind, key, proxy))
        return proxy

    def flush(self) -> list[EnvelopeUsage]:
        """Emit one EnvelopeUsage per tracked envelope and clear the scope."""
        flushed: list[EnvelopeUsage] = []
        for kind, key, proxy in self._tracked:
            usage = EnvelopeUsage(
                request_id=self.request_id,
                tenant_id=self.tenant_id,
                envelope_kind=kind,
                envelope_key=key,
                fields_read=sorted(proxy._fields_read),
                states_branched=list(proxy._states_branched),
            )
            self.recorder.record(usage)
            flushed.append(usage)
        self._tracked.clear()
        return flushed

    def __enter__(self) -> "UsageTracker":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.flush()
