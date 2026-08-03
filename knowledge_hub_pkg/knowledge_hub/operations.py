"""Operation registry, base fact/graph ops, composites (Build Prompt S3).

Implements the S1 `OperationRegistry` seam: the fact/graph query surface —
Path C's default — and the authoring machinery behind it. Everything this
module registers routes through the S2 gate; there is no other door to
Postgres on the serve path.

AN OPERATION IS A DECLARATIVE SPEC, NOT CODE. `OperationSpec` carries a
name, typed params, a `{sec:<alias>}`-marked SQL template, a permission
scope, and a latency class — the generator binds params, routes the
template through `PostgresChokePoint.read()`, and wraps the rows in S1
envelopes. Registration is BUILD-TIME: the system never mints an op or a
query at runtime; a caller can only invoke what an author registered.

"UNFILTERED OP IS UNWRITABLE" EXTENDS TO AUTHORING: the generator REJECTS
any spec whose SQL template lacks a `{sec:<alias>}` security marker, is not
a single read-only statement, binds parameters it did not declare, or
carries brace tokens outside the marker grammar. The S2 gate would refuse
such a template at run time anyway — authoring rejection just moves the
failure to registration, where the author is watching.

A COMPOSITE IS A FIXED DECLARED PLAN, NOT FREE LOGIC. `CompositeSpec` is an
ordered list of already-registered ops with explicit data-flow (parameter
bindings: composite param, constant, or a fixed extractor over an earlier
step's envelopes). It may call base ops and previously-registered
composites — DOWNWARD-ACYCLIC, flattened and termination-checked at
registration — and it cannot introduce a new query, table, or traversal. A
fixed fallback chain (try A, else B) is allowed; data-dependent looping and
content-dependent plan shape are structurally unexpressible (`extra=
"forbid"` on the step model — there is no field to hang an `if` on), so
every op a composite could ever run is enumerable from its spec. Because
every step is a registered op transiting the gate, a composite inherits its
steps' bounds and permission filtering automatically. Execution preserves
per-step envelopes (facts as facts, evidence as evidence, tagged by step —
never flattened) and emits an execution trace.

SECURITY POSTURE (inherited from S2, verified in tests, not reimplemented):

  * Ops receive params, mint nothing: each run calls `enforce()` and passes
    the resulting FilteredQuery to `read()`. The compiled op holds a choke
    point, never a connection.
  * Fact templates join `entities` under `{sec:}` markers for BOTH ends of
    the triple: a fact whose subject or entity-object the principal may not
    see is silently absent (the absence rule) — a served fact never names a
    hidden entity.
  * Traversals walk `facts` via recursive CTEs (AGE cypher() cannot bind
    params — S2 decision); the walk itself is label-filtered on every hop
    (facts AND the entities it steps through), so a chain never extends
    through a hidden edge or node.
  * Grounding is served via a JOIN, not a column: `facts` has no grounding
    column — the verdict lives on `pending_facts.grounding` (migration 004)
    and survives promotion via `promoted_fact_id`.

ENTITIES ARE SERVED AS `EntityRef` INSIDE FACT ENVELOPES. A bare entity
cannot build a `ProvenanceSpine` (no document provenance on the registry
row), which is the S1 type system saying entities are not standalone
servables. `get_entity` / `get_by_key` are therefore resolution sugar: they
resolve id-or-strong-key and serve the entity's facts, with the canonical
entity riding in each envelope's `subject` ref. An entity with no visible
facts yields an empty result — indistinguishable from absent, which is
exactly what the absence rule requires of an entity you cannot see.

Keep this module in lock-step with serving.py (the envelope shapes and the
OPERATION_RETURNS vocabulary — 'composite' was added there with this
module) and with choke_point.py (marker grammar, reserved param names).
"""
from __future__ import annotations

import re
import time
from typing import Any, Callable, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from knowledge_hub.choke_point import PostgresChokePoint
from knowledge_hub.factstore_pg import vector_literal
from knowledge_hub.interfaces import Embedder
from knowledge_hub.serving import (
    EntityRef,
    EvidenceEnvelope,
    FactEnvelope,
    Operation,
    OperationRegistry,
    Principal,
    ProvenanceSpine,
    RetrievalQuery,
    RetrievalSignal,
    UncertaintyState,
    UnknownOperation,
)

# Mirror of the S2 gateway grammar (choke_point.py) — authoring-side. The
# gate re-checks all of this at run time; compiling here means a bad spec
# fails at registration, where the author is watching.
_SEC_MARKER = re.compile(r"\{sec:([A-Za-z_][A-Za-z0-9_]*)\}")
_CUR_MARKER = re.compile(r"\{cur:([A-Za-z_][A-Za-z0-9_]*)\}")
_TEMPORAL_TABLE = re.compile(
    r"\b(?:FROM|JOIN)\s+(?:facts|documents)\b\s+(?:AS\s+)?"
    r"([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)
_BRACE_TOKEN = re.compile(r"\{[^{}]*\}")
_MARKER_GRAMMAR = re.compile(r"\{(?:sec|tenant|cur):[A-Za-z_][A-Za-z0-9_]*\}")
_PLACEHOLDER = re.compile(r"%\(([A-Za-z_][A-Za-z0-9_]*)\)s")
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_RESERVED_PREFIX = "kh_"                # gateway-owned bind-param namespace

# The query-level temporal audit flag (serving.RetrievalQuery). Every base op
# accepts it implicitly: CompiledOperation.run pops it off the params and
# carries it onto the RetrievalQuery it enforces, where the S2 {cur:}
# predicate honors it. Authors may not declare a param with this name — it
# belongs to the query scope, not to any one op's SQL.
RESERVED_QUERY_PARAM = "include_retracted"

PARAM_TYPES = ("str", "int", "float", "bool", "list[str]", "list[int]",
               "embedding_text")
LATENCY_CLASSES = ("lookup", "traversal", "search")

# Traversal ceiling: a spec may declare a smaller depth cap, never a larger
# one. Recursive CTEs are hop-bounded so cycles terminate; this bounds cost.
MAX_TRAVERSAL_DEPTH = 5

# Grounding verdicts that mean "asserted but weakly supported" (the grounder
# flags instead of rejecting — migration 004).
_FLAGGED_GROUNDING = ("span_missing", "components_missing")


# ---------------------------------------------------------------- refusals --
class OperationRejected(Exception):
    """The authoring machinery refused a spec (registration time) or a
    template's projection did not honor its contract (which is a spec bug
    surfacing at run time, not a caller error)."""


class OperationCallError(Exception):
    """A syntactically valid call to a registered op carried bad params
    (missing required, wrong type, out of bounds, unknown name)."""


# ------------------------------------------------------------- param specs --
class ParamSpec(BaseModel):
    """One typed parameter of an operation — declarative, like everything
    else in a spec. `embedding_text` params take a string from the caller;
    the generator embeds it and binds the pgvector literal (pair with a
    `::vector` cast in the template)."""
    model_config = ConfigDict(extra="forbid")

    type: str = "str"
    required: bool = False
    default: Any = None
    choices: Optional[list[Any]] = None
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    description: str = ""

    @model_validator(mode="after")
    def _check(self) -> "ParamSpec":
        if self.type not in PARAM_TYPES:
            raise ValueError(f"param type must be one of {PARAM_TYPES}")
        return self


def _coerce_param(op: str, name: str, spec: ParamSpec, value: Any) -> Any:
    """Validate one call argument against its declared type. Fail closed:
    anything off-spec raises OperationCallError, nothing is silently cast."""
    if value is None:
        if spec.required and spec.default is None:
            raise OperationCallError(f"{op}: param {name!r} is required")
        value = spec.default
    if value is None:
        return None
    t = spec.type
    ok = (
        isinstance(value, str) if t in ("str", "embedding_text")
        else isinstance(value, bool) if t == "bool"
        else isinstance(value, int) and not isinstance(value, bool) if t == "int"
        else isinstance(value, (int, float)) and not isinstance(value, bool) if t == "float"
        else isinstance(value, list) and all(isinstance(v, str) for v in value) if t == "list[str]"
        else isinstance(value, list) and all(
            isinstance(v, int) and not isinstance(v, bool) for v in value) if t == "list[int]"
        else False
    )
    if not ok:
        raise OperationCallError(
            f"{op}: param {name!r} must be {t}, got {type(value).__name__}")
    if spec.choices is not None and value not in spec.choices:
        raise OperationCallError(
            f"{op}: param {name!r} must be one of {spec.choices}")
    if t in ("int", "float"):
        if spec.minimum is not None and value < spec.minimum:
            raise OperationCallError(
                f"{op}: param {name!r} must be >= {spec.minimum}")
        if spec.maximum is not None and value > spec.maximum:
            raise OperationCallError(
                f"{op}: param {name!r} must be <= {spec.maximum}")
    return value


# -------------------------------------------------------------- the specs --
class OperationSpec(Operation):
    """One declarative base operation: the S1 catalog shape plus what the
    generator needs to make it executable. Registering one is a data write;
    the generator does the rest. `scope` is a role list (ANY-of) gating who
    may even see the op — empty means every resolved principal of the
    tenant; a scope miss answers UnknownOperation, never 'forbidden'
    (permission-invisibility applies to the catalog too)."""

    params: dict[str, ParamSpec] = Field(default_factory=dict)
    sql: str
    scope: list[str] = Field(default_factory=list)
    latency: str = "lookup"
    require_any: list[str] = Field(default_factory=list)  # >=1 must be non-None

    @model_validator(mode="after")
    def _check_spec(self) -> "OperationSpec":
        if self.returns == "composite":
            raise ValueError("a base OperationSpec cannot return 'composite'"
                             " — use CompositeSpec")
        if self.latency not in LATENCY_CLASSES:
            raise ValueError(f"latency must be one of {LATENCY_CLASSES}")
        return self


BINDING_SOURCES = ("param", "const", "step")


class ParamBinding(BaseModel):
    """Where one step-param value comes from: a composite param, a constant
    declared in the plan, or a FIXED extractor over an earlier step's
    envelopes. That vocabulary is the entire data-flow grammar — there is
    nothing here that can change the plan's shape."""
    model_config = ConfigDict(extra="forbid")

    source: str
    name: Optional[str] = None      # source='param': composite param name
    value: Any = None               # source='const'
    step: Optional[str] = None      # source='step': earlier step label
    extract: Optional[str] = None   # source='step': extractor name

    @model_validator(mode="after")
    def _check(self) -> "ParamBinding":
        if self.source not in BINDING_SOURCES:
            raise ValueError(f"binding source must be one of {BINDING_SOURCES}")
        if self.source == "param" and not self.name:
            raise ValueError("param binding requires 'name'")
        if self.source == "step" and not (self.step and self.extract):
            raise ValueError("step binding requires 'step' and 'extract'")
        return self


class CompositeStep(BaseModel):
    """One step of a fixed plan: a registered op, its param bindings, and
    optionally ONE declared fallback (tried only when the primary returns
    zero envelopes). extra='forbid' is the structural guarantee that a plan
    cannot express content-dependent control flow — there is no field to
    hang an `if`, a loop, or a router on."""
    model_config = ConfigDict(extra="forbid")

    step: str
    op: str
    bind: dict[str, ParamBinding] = Field(default_factory=dict)
    fallback_op: Optional[str] = None
    fallback_bind: Optional[dict[str, ParamBinding]] = None


class CompositeSpec(Operation):
    """A fixed declared plan over already-registered ops. May reference base
    ops and PREVIOUSLY-registered composites; the registry flattens the
    closure and rejects cycles at registration, so termination is a
    registration property, not a runtime hope."""

    returns: str = "composite"
    params: dict[str, ParamSpec] = Field(default_factory=dict)
    scope: list[str] = Field(default_factory=list)
    steps: list[CompositeStep] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_spec(self) -> "CompositeSpec":
        if self.returns != "composite":
            raise ValueError("a CompositeSpec always returns 'composite'")
        labels = [s.step for s in self.steps]
        if len(labels) != len(set(labels)):
            raise ValueError("step labels must be unique")
        return self


# Step extractors: the FIXED vocabulary a binding may pull from an earlier
# step's envelopes. Each entry: (envelope kind it reads, callable). Returning
# _EMPTY means "nothing to extract" — the dependent step is skipped and the
# skip is recorded in the trace.
_EMPTY = object()

STEP_EXTRACTORS: dict[str, tuple[str, Callable[[list[Any]], Any]]] = {
    "first_subject_id": ("facts", lambda envs: envs[0].subject.entity_id),
    "first_subject_name": ("facts", lambda envs: envs[0].subject.canonical_name),
    "first_object_id": ("facts", lambda envs: next(
        (e.object_entity.entity_id for e in envs if e.object_entity is not None),
        _EMPTY)),
    "first_chunk_id": ("evidence", lambda envs: envs[0].spine.chunk_id),
}


# ----------------------------------------------------- trace + step results --
class TraceEntry(BaseModel):
    """One executed (or skipped) plan step, for the execution trace. Params
    are the caller-visible values (embedding_text params appear as their raw
    text, never as vectors). `gated` is structural: an executed step can only
    have reached Postgres through the S2 gate, because the compiled op holds
    a choke point and no connection."""
    model_config = ConfigDict(extra="forbid")

    step: str
    op: str
    status: str                    # 'ok' | 'fallback_used' | 'skipped_empty_input'
    params: dict[str, Any] = Field(default_factory=dict)
    envelopes: int = 0
    wall_ms: float = 0.0
    gated: bool = True


class StepResult(BaseModel):
    """One step's envelopes, tagged by step and kind — facts as facts,
    evidence as evidence, never flattened together."""
    model_config = ConfigDict(extra="forbid")

    step: str
    op: str
    returns: str                   # 'facts' | 'evidence'
    facts: list[FactEnvelope] = Field(default_factory=list)
    evidence: list[EvidenceEnvelope] = Field(default_factory=list)


class CompositeResult(BaseModel):
    """What a composite run returns: per-step envelopes plus the execution
    trace. This is an S3 orchestration record AROUND S1 envelopes — the
    envelopes inside are the response shapes; this wrapper adds no new one."""
    model_config = ConfigDict(extra="forbid")

    name: str
    tenant_id: str
    params: dict[str, Any] = Field(default_factory=dict)
    steps: list[StepResult] = Field(default_factory=list)
    trace: list[TraceEntry] = Field(default_factory=list)


# ------------------------------------------------------- envelope building --
def fact_state_from_row(row: Mapping[str, Any]) -> UncertaintyState:
    """Deterministic state assignment for a served (promoted) fact.

    * valid_to set     -> retracted (reachable ONLY under an explicit
                          include_retracted audit query — the {cur:}
                          predicate excludes these rows by default; when
                          asked for, they must say what they are, never
                          masquerade as current)
    * oversized        -> under_review (the 'oversized_fact' review_queue
                          feeder owns it right now)
    * grounding flagged
      or needs_review  -> known_low_confidence (asserted, weakly supported:
                          the grounder flags instead of rejecting)
    * otherwise        -> known_confident ('pass', 'construction', or never
                          span-grounded structured facts)

    `unresolved` never applies here — these ops serve `facts`, which only
    holds resolved rows; pending_facts stay behind the review surface."""
    if row.get("valid_to") is not None:
        return UncertaintyState.retracted
    if row.get("oversized"):
        return UncertaintyState.under_review
    if row.get("needs_review") or row.get("grounding") in _FLAGGED_GROUNDING:
        return UncertaintyState.known_low_confidence
    return UncertaintyState.known_confident


_FACT_COLUMNS = (
    "fact_id", "tenant_id", "document_id", "chunk_id", "char_start",
    "char_end", "locator", "security_label", "security_label_id",
    "subject_entity_id", "subject_name", "subject_type", "predicate",
    "object_entity_id", "object_name", "object_type", "object_literal",
    "attributes", "valid_from", "valid_to", "ontology_version", "extractor",
    "extractor_version", "confidence", "oversized", "grounding",
    "needs_review",
)

_EVIDENCE_COLUMNS = (
    "chunk_id", "document_id", "tenant_id", "content", "contextual_prefix",
    "document_title", "char_start", "char_end", "locator", "security_label",
    "security_label_id", "source_timestamp", "score",
)


def _require_columns(op: str, row: Mapping[str, Any], columns: tuple) -> None:
    missing = [c for c in columns if c not in row]
    if missing:
        raise OperationRejected(
            f"op {op!r}: template projection is missing column(s) {missing} "
            f"— the canonical projection contract was not honored")


def fact_envelope_from_row(op: str, row: Mapping[str, Any]) -> FactEnvelope:
    """Canonical row -> FactEnvelope. The template must project the
    _FACT_COLUMNS contract (fact_template() does)."""
    _require_columns(op, row, _FACT_COLUMNS)
    obj = None
    if row["object_entity_id"] is not None:
        obj = EntityRef(entity_id=row["object_entity_id"],
                        canonical_name=row["object_name"],
                        entity_type=row["object_type"])
    return FactEnvelope(
        spine=ProvenanceSpine(
            tenant_id=row["tenant_id"], document_id=row["document_id"],
            chunk_id=row["chunk_id"], char_start=row["char_start"],
            char_end=row["char_end"], locator=row["locator"],
            security_label=row["security_label"],
            security_label_id=row["security_label_id"]),
        fact_id=row["fact_id"],
        subject=EntityRef(entity_id=row["subject_entity_id"],
                          canonical_name=row["subject_name"],
                          entity_type=row["subject_type"]),
        predicate=row["predicate"],
        object_entity=obj,
        object_literal=row["object_literal"],
        attributes=row["attributes"] or {},
        state=fact_state_from_row(row),
        confidence=row["confidence"],
        valid_from=row["valid_from"],
        valid_to=row["valid_to"],
        ontology_version=row["ontology_version"],
        extractor=row["extractor"],
        extractor_version=row["extractor_version"],
        grounding=row["grounding"],
    )


def evidence_envelope_from_row(op: str, row: Mapping[str, Any], *, rank: int,
                               query_text: str,
                               mode: str = "dense") -> EvidenceEnvelope:
    """Canonical row -> EvidenceEnvelope. Context fields (contextual_prefix,
    document_title, section) are default-on — part of the envelope, not a
    knob; `section` is derived from the chunk locator's heading path. `score`
    is retrieval relevance, a statement about the query, never about the
    world. grounded_facts stays [] here — S4's `enrich` owns it."""
    _require_columns(op, row, _EVIDENCE_COLUMNS)
    locator = row["locator"] or {}
    return EvidenceEnvelope(
        spine=ProvenanceSpine(
            tenant_id=row["tenant_id"], document_id=row["document_id"],
            chunk_id=row["chunk_id"], char_start=row["char_start"],
            char_end=row["char_end"], locator=row["locator"],
            security_label=row["security_label"],
            security_label_id=row["security_label_id"]),
        content=row["content"],
        contextual_prefix=row["contextual_prefix"],
        document_title=row["document_title"],
        section=locator.get("heading_path") or locator.get("heading"),
        signal=RetrievalSignal(score=float(row["score"]), rank=rank,
                               mode=mode, query=query_text),
        source_timestamp=row["source_timestamp"],
    )


# ------------------------------------------------------ template authoring --
def fact_template(where: str, *, head: str = "",
                  tail: str = "ORDER BY f.id") -> str:
    """The canonical fact-envelope SQL body: one place composes the
    projection contract, the entity-ref joins (both ends label-checked),
    the chunk backfill for document_id, the grounding join
    (pending_facts.promoted_fact_id — facts has no grounding column), and
    the {cur:f} temporal gate (current facts only; an include_retracted
    audit query widens it AT THE CHOKE POINT — never per-op). Authors
    supply the WHERE clause (and optionally a WITH ... head for
    traversals); the alias vocabulary is f/se/oe/c/sl/pf."""
    return f"""{head}SELECT
    f.id AS fact_id,
    f.tenant_id,
    COALESCE(f.source_document_id, c.document_id) AS document_id,
    f.source_chunk_id AS chunk_id,
    f.char_start,
    f.char_end,
    f.locator,
    COALESCE(sl.label, 'public') AS security_label,
    f.security_label_id,
    f.subject_entity_id,
    se.canonical_name AS subject_name,
    se.entity_type AS subject_type,
    f.predicate,
    f.object_entity_id,
    oe.canonical_name AS object_name,
    oe.entity_type AS object_type,
    f.object_literal,
    f.attributes,
    f.valid_from,
    f.valid_to,
    f.ontology_version,
    f.extractor,
    f.extractor_version,
    f.confidence,
    f.oversized,
    pf.grounding,
    COALESCE(pf.needs_review, false) AS needs_review
FROM facts f
JOIN entities se ON se.id = f.subject_entity_id AND {{sec:se}}
LEFT JOIN entities oe ON oe.id = f.object_entity_id AND {{sec:oe}}
LEFT JOIN chunks c ON c.id = f.source_chunk_id AND {{tenant:c}}
LEFT JOIN security_labels sl ON sl.id = f.security_label_id
LEFT JOIN pending_facts pf ON pf.promoted_fact_id = f.id AND {{sec:pf}}
WHERE {{sec:f}}
  AND {{cur:f}}
  AND (f.object_entity_id IS NULL OR oe.id IS NOT NULL)
  AND ({where})
{tail}"""


# The strong-key match, shared by get_by_key / get_entity: exact value match
# against the canonical registry's attributes (where resolution merges each
# mention's extracted_keys — email, tax_id, customer_id, asset_id, ...).
# Named key when given, any-key value scan when not. Verbatim identifiers
# come HERE, never through embedding retrieval (Axis-C round-3 caveat).
def _key_match(alias: str) -> str:
    return f"""((%(key)s::text IS NOT NULL
        AND {alias}.attributes ->> %(key)s = %(identifier)s)
       OR (%(key)s::text IS NULL AND EXISTS (
             SELECT 1 FROM jsonb_each_text({alias}.attributes) kv
             WHERE kv.value = %(identifier)s)))"""


# ------------------------------------------------------------- compilation --
class CompiledOperation:
    """A registered base op, generated from its spec: coerce params, embed
    embedding_text params, enforce, read through the gate, wrap rows in S1
    envelopes. Holds a choke point and an (optional) embedder — never a
    connection."""

    kind = "operation"

    def __init__(self, spec: OperationSpec, choke: PostgresChokePoint,
                 embedder: Optional[Embedder]):
        self.spec = spec
        self._choke = choke
        self._embedder = embedder
        self._referenced = set(_PLACEHOLDER.findall(spec.sql))

    def run(self, principal: Principal, params: Mapping[str, Any],
            *, catalog: Optional["InProcessOperationCatalog"] = None
            ) -> list[FactEnvelope] | list[EvidenceEnvelope]:
        spec = self.spec
        _check_scope(spec.scope, principal, spec.name)
        given = dict(params or {})
        # The reserved temporal audit flag rides the QUERY, not the SQL: it
        # never binds into the template; it flips the S2 {cur:} predicate.
        raw_flag = given.pop(RESERVED_QUERY_PARAM, None)
        if raw_flag is None:
            include_retracted = False
        elif isinstance(raw_flag, bool):
            include_retracted = raw_flag
        else:
            raise OperationCallError(
                f"{spec.name}: {RESERVED_QUERY_PARAM} must be a boolean")
        unknown = set(given) - set(spec.params)
        if unknown:
            raise OperationCallError(
                f"{spec.name}: unknown param(s) {sorted(unknown)}")
        coerced = {name: _coerce_param(spec.name, name, ps, given.get(name))
                   for name, ps in spec.params.items()}
        if spec.require_any and all(coerced.get(n) is None
                                    for n in spec.require_any):
            raise OperationCallError(
                f"{spec.name}: at least one of {spec.require_any} is required")

        # embedding_text params bind as pgvector literals; raw text is kept
        # for the retrieval signal (a statement about the query).
        bind = dict(coerced)
        query_text = ""
        for name, ps in spec.params.items():
            if ps.type == "embedding_text" and coerced[name] is not None:
                if self._embedder is None:
                    raise OperationCallError(
                        f"{spec.name}: param {name!r} needs an embedder and "
                        f"none is wired into this catalog")
                query_text = coerced[name]
                bind[name] = vector_literal(
                    self._embedder.embed([coerced[name]])[0])

        fq = self._choke.enforce(
            RetrievalQuery(text=f"op:{spec.name}",
                           include_retracted=include_retracted), principal)
        rows = self._choke.read(
            fq, spec.sql, {n: bind[n] for n in self._referenced})
        if spec.returns == "facts":
            return [fact_envelope_from_row(spec.name, r) for r in rows]
        return [evidence_envelope_from_row(spec.name, r, rank=i + 1,
                                           query_text=query_text)
                for i, r in enumerate(rows)]


class CompiledComposite:
    """A registered plan. Steps resolve against the live catalog by name (so
    a replaced op is picked up — the catalog re-validates coherence on every
    registration), bindings are evaluated per the fixed grammar, and every
    step runs as its own gated op."""

    kind = "composite"

    def __init__(self, spec: CompositeSpec):
        self.spec = spec

    def run(self, principal: Principal, params: Mapping[str, Any],
            *, catalog: "InProcessOperationCatalog") -> CompositeResult:
        spec = self.spec
        tenant_id = principal.tenant_id
        _check_scope(spec.scope, principal, spec.name)
        for op_name in catalog.closure(tenant_id, spec.name):
            _check_scope(catalog.scope_of(tenant_id, op_name), principal,
                         spec.name)

        given = dict(params or {})
        unknown = set(given) - set(spec.params)
        if unknown:
            raise OperationCallError(
                f"{spec.name}: unknown param(s) {sorted(unknown)}")
        coerced = {name: _coerce_param(spec.name, name, ps, given.get(name))
                   for name, ps in spec.params.items()}

        results: dict[str, StepResult] = {}
        out_steps: list[StepResult] = []
        trace: list[TraceEntry] = []

        for step in spec.steps:
            bound = self._bind(step.bind, coerced, results, spec.name)
            if bound is _EMPTY:
                trace.append(TraceEntry(step=step.step, op=step.op,
                                        status="skipped_empty_input",
                                        gated=False))
                continue

            started = time.perf_counter()
            envelopes, ran_op, status = self._run_step(
                step, bound, coerced, results, principal, catalog)
            wall_ms = (time.perf_counter() - started) * 1000

            if isinstance(envelopes, CompositeResult):
                # A nested (previously-registered) composite: surface its
                # step results and trace under dotted labels — still tagged,
                # still never flattened.
                for sub in envelopes.steps:
                    tagged = sub.model_copy(
                        update={"step": f"{step.step}.{sub.step}"})
                    out_steps.append(tagged)
                for sub_t in envelopes.trace:
                    trace.append(sub_t.model_copy(
                        update={"step": f"{step.step}.{sub_t.step}"}))
                results[step.step] = StepResult(step=step.step, op=ran_op,
                                                returns="facts")
                continue

            returns = catalog.returns_of(tenant_id, ran_op)
            result = StepResult(
                step=step.step, op=ran_op, returns=returns,
                facts=envelopes if returns == "facts" else [],
                evidence=envelopes if returns == "evidence" else [])
            results[step.step] = result
            out_steps.append(result)
            trace.append(TraceEntry(step=step.step, op=ran_op, status=status,
                                    params=bound, envelopes=len(envelopes),
                                    wall_ms=wall_ms))

        return CompositeResult(name=spec.name, tenant_id=tenant_id,
                               params=coerced, steps=out_steps, trace=trace)

    # ------------------------------------------------------------- helpers
    @staticmethod
    def _bind(bindings: dict[str, ParamBinding], params: dict[str, Any],
              results: dict[str, StepResult], composite: str):
        bound: dict[str, Any] = {}
        for target, b in bindings.items():
            if b.source == "param":
                bound[target] = params.get(b.name)
            elif b.source == "const":
                bound[target] = b.value
            else:
                source = results.get(b.step)
                envs = (source.facts or source.evidence) if source else []
                if not envs:
                    return _EMPTY
                value = STEP_EXTRACTORS[b.extract][1](envs)
                if value is _EMPTY:
                    return _EMPTY
                bound[target] = value
        return bound

    def _run_step(self, step: CompositeStep, bound: dict[str, Any],
                  params: dict[str, Any], results: dict[str, StepResult],
                  principal: Principal,
                  catalog: "InProcessOperationCatalog"):
        primary = catalog.compiled(principal.tenant_id, step.op)
        envelopes = primary.run(principal, bound, catalog=catalog)
        if _count(envelopes) or step.fallback_op is None:
            return envelopes, step.op, "ok"
        fb_bound = self._bind(step.fallback_bind
                              if step.fallback_bind is not None else step.bind,
                              params, results, self.spec.name)
        if fb_bound is _EMPTY:
            return envelopes, step.op, "ok"
        fallback = catalog.compiled(principal.tenant_id, step.fallback_op)
        return (fallback.run(principal, fb_bound, catalog=catalog),
                step.fallback_op, "fallback_used")


def _count(result) -> int:
    if isinstance(result, CompositeResult):
        return sum(len(s.facts) + len(s.evidence) for s in result.steps)
    return len(result)


def _check_scope(scope: list[str], principal: Principal, name: str) -> None:
    """Role-scope gate: a principal outside an op's scope gets
    UnknownOperation — the op is invisible, not forbidden (the absence rule
    applies to the catalog too)."""
    if scope and not set(scope) & set(principal.roles):
        raise UnknownOperation(principal.tenant_id, name)


# --------------------------------------------------------------- generator --
class OperationGenerator:
    """Turns declarative specs into executable, registered operations — and
    REFUSES anything outside the grammar. This is where 'unfiltered op is
    unwritable' extends to authoring: no `{sec:}` marker, no registration."""

    def __init__(self, choke: PostgresChokePoint,
                 embedder: Optional[Embedder] = None):
        self._choke = choke
        self._embedder = embedder

    # ------------------------------------------------------------- base ops
    def compile(self, spec: OperationSpec) -> CompiledOperation:
        name = spec.name
        if not _NAME.match(name):
            raise OperationRejected(
                f"op name {name!r} must match {_NAME.pattern}")
        self._check_params(name, spec.params)
        if any(p not in spec.params for p in spec.require_any):
            raise OperationRejected(
                f"op {name!r}: require_any names undeclared params")

        body = spec.sql.strip()
        if not body:
            raise OperationRejected(f"op {name!r}: empty SQL template")
        if ";" in body:
            raise OperationRejected(
                f"op {name!r}: multi-statement templates are refused")
        if not body.upper().startswith(("SELECT", "WITH")):
            raise OperationRejected(
                f"op {name!r}: template must be a single SELECT/WITH")
        if not _SEC_MARKER.search(body):
            raise OperationRejected(
                f"op {name!r}: template carries no {{sec:<alias>}} security "
                f"marker — an unfiltered op is unwritable")
        stray = [t for t in _BRACE_TOKEN.findall(body)
                 if not _MARKER_GRAMMAR.fullmatch(t)]
        if stray:
            raise OperationRejected(
                f"op {name!r}: brace token(s) {stray} are outside the "
                f"marker grammar ({{sec:alias}} / {{tenant:alias}} / "
                f"{{cur:alias}})")
        undeclared = set(_PLACEHOLDER.findall(body)) - set(spec.params)
        if undeclared:
            raise OperationRejected(
                f"op {name!r}: template binds undeclared param(s) "
                f"{sorted(undeclared)}")
        # Temporal discipline (migration 009), authoring-side mirror of the
        # S2 gate: reading facts/documents without a {cur:<alias>} marker is
        # unwritable at registration, exactly like a missing {sec:}.
        # (Checked after the finer-grained template errors so those keep
        # their own rejection messages.)
        unmarked = (set(_TEMPORAL_TABLE.findall(body))
                    - set(_CUR_MARKER.findall(body)))
        if unmarked:
            raise OperationRejected(
                f"op {name!r}: template reads temporal table alias(es) "
                f"{sorted(unmarked)} without a {{cur:<alias>}} marker — a "
                f"temporally unfiltered op is unwritable")

        embed_params = [n for n, p in spec.params.items()
                        if p.type == "embedding_text"]
        if spec.returns == "evidence" and len(embed_params) != 1:
            raise OperationRejected(
                f"op {name!r}: an evidence op declares exactly one "
                f"embedding_text param (the retrieval signal's query); "
                f"got {embed_params}")
        return CompiledOperation(spec, self._choke, self._embedder)

    # ----------------------------------------------------------- composites
    def compile_composite(self, spec: CompositeSpec,
                          specs: Mapping[str, Operation]) -> CompiledComposite:
        """Validate a plan against the tenant's (hypothetical) catalog:
        every referenced op registered, bindings within the grammar and
        compatible with their targets, data-flow strictly downward, closure
        acyclic and terminating in base ops."""
        name = spec.name
        if not _NAME.match(name):
            raise OperationRejected(
                f"composite name {name!r} must match {_NAME.pattern}")
        self._check_params(name, spec.params)

        seen_labels: list[str] = []
        for step in spec.steps:
            for op_name in filter(None, (step.op, step.fallback_op)):
                target = specs.get(op_name)
                if target is None:
                    raise OperationRejected(
                        f"composite {name!r}: step {step.step!r} references "
                        f"unregistered op {op_name!r}")
            self._check_bindings(name, step, step.op, step.bind, spec,
                                 specs, seen_labels)
            if step.fallback_op is not None:
                if specs[step.op].returns != specs[step.fallback_op].returns:
                    raise OperationRejected(
                        f"composite {name!r}: step {step.step!r} fallback "
                        f"must return the same envelope kind as the primary")
                self._check_bindings(
                    name, step, step.fallback_op,
                    step.fallback_bind if step.fallback_bind is not None
                    else step.bind,
                    spec, specs, seen_labels)
            seen_labels.append(step.step)

        _flatten(name, {**specs, name: spec}, name, stack=())
        return CompiledComposite(spec)

    # -------------------------------------------------------------- helpers
    @staticmethod
    def _check_params(name: str, params: dict[str, ParamSpec]) -> None:
        for pname in params:
            if not _NAME.match(pname):
                raise OperationRejected(
                    f"op {name!r}: param name {pname!r} must match "
                    f"{_NAME.pattern}")
            if pname.startswith(_RESERVED_PREFIX):
                raise OperationRejected(
                    f"op {name!r}: param prefix {_RESERVED_PREFIX!r} is "
                    f"reserved by the gateway")
            if pname == RESERVED_QUERY_PARAM:
                raise OperationRejected(
                    f"op {name!r}: {RESERVED_QUERY_PARAM!r} is the reserved "
                    f"query-level temporal audit flag — every op accepts it "
                    f"implicitly; it is never an op-local param")

    @staticmethod
    def _check_bindings(name: str, step: CompositeStep, target_name: str,
                        bindings: dict[str, ParamBinding],
                        spec: CompositeSpec, specs: Mapping[str, Operation],
                        earlier: list[str]) -> None:
        target_op = specs[target_name]
        target_params = getattr(target_op, "params", {}) or {}
        for pname, b in bindings.items():
            if pname not in target_params:
                raise OperationRejected(
                    f"composite {name!r}: step {step.step!r} binds unknown "
                    f"param {pname!r} of op {target_name!r}")
            if b.source == "param" and b.name not in spec.params:
                raise OperationRejected(
                    f"composite {name!r}: step {step.step!r} binds from "
                    f"undeclared composite param {b.name!r}")
            if b.source == "step":
                if b.step not in earlier:
                    raise OperationRejected(
                        f"composite {name!r}: step {step.step!r} binds from "
                        f"step {b.step!r}, which is not an EARLIER step — "
                        f"data flows strictly downward")
                if b.extract not in STEP_EXTRACTORS:
                    raise OperationRejected(
                        f"composite {name!r}: unknown extractor "
                        f"{b.extract!r} (vocabulary: "
                        f"{sorted(STEP_EXTRACTORS)})")
                source_step = next(s for s in spec.steps if s.step == b.step)
                source_op = specs.get(source_step.op)
                kind = STEP_EXTRACTORS[b.extract][0]
                if source_op is None or source_op.returns != kind:
                    raise OperationRejected(
                        f"composite {name!r}: extractor {b.extract!r} reads "
                        f"{kind} envelopes but step {b.step!r} runs "
                        f"{source_step.op!r} "
                        f"(returns {getattr(source_op, 'returns', '?')!r})")
        # Required-without-default target params must be covered.
        for pname, ps in target_params.items():
            if ps.required and ps.default is None and pname not in bindings:
                raise OperationRejected(
                    f"composite {name!r}: step {step.step!r} leaves required "
                    f"param {pname!r} of op {target_name!r} unbound")


def _flatten(root: str, specs: Mapping[str, Operation], name: str,
             stack: tuple) -> None:
    """Termination check: walk the plan closure; every leaf must be a
    registered base op and no name may recur on the walk (cycle)."""
    if name in stack:
        raise OperationRejected(
            f"composite {root!r}: cyclic plan "
            f"({' -> '.join((*stack, name))}) is rejected")
    spec = specs.get(name)
    if spec is None:
        raise OperationRejected(
            f"composite {root!r}: references unregistered op {name!r}")
    if isinstance(spec, CompositeSpec):
        for step in spec.steps:
            for op_name in filter(None, (step.op, step.fallback_op)):
                _flatten(root, specs, op_name, (*stack, name))


# ---------------------------------------------------------------- registry --
class InProcessOperationCatalog(OperationRegistry):
    """The per-tenant operation catalog (S1 seam), backed by the generator.

    Registration is BUILD-TIME authoring: `register` accepts an
    OperationSpec or CompositeSpec (both ARE S1 Operations — the spec is
    data), compiles it through the generator, and re-validates every plan in
    the tenant against the hypothetical catalog before committing — so a
    replacement that would orphan or entangle an existing composite is
    rejected atomically. A bare S1 Operation (no template, no plan) is
    unregistrable: nothing executable — and nothing filtered — can be
    generated from it.

    `get`/`list_ops` return the SANITIZED public Operation (name, typed
    params, returns, description) — the catalog surface agents see; SQL
    templates and plans stay authoring-side."""

    def __init__(self, choke: PostgresChokePoint,
                 embedder: Optional[Embedder] = None):
        self._generator = OperationGenerator(choke, embedder)
        self._specs: dict[str, dict[str, Operation]] = {}
        self._compiled: dict[str, dict[str, Any]] = {}

    # -------------------------------------------------------------- S1 seam
    def register(self, tenant_id: str, op: Operation) -> None:
        tenant_specs = self._specs.setdefault(tenant_id, {})
        hypothetical = {**tenant_specs, op.name: op}
        if isinstance(op, CompositeSpec):
            compiled = self._generator.compile_composite(op, hypothetical)
        elif isinstance(op, OperationSpec):
            compiled = self._generator.compile(op)
        else:
            raise OperationRejected(
                f"op {op.name!r}: a bare Operation carries no SQL template "
                f"and no plan — nothing executable (and nothing filtered) "
                f"can be generated from it")
        # Replacement coherence: every existing plan must still flatten,
        # terminate, and bind correctly against the hypothetical catalog.
        for existing in hypothetical.values():
            if isinstance(existing, CompositeSpec):
                self._generator.compile_composite(
                    existing, {**hypothetical, existing.name: existing})
        tenant_specs[op.name] = op
        self._compiled.setdefault(tenant_id, {})[op.name] = compiled

    def get(self, tenant_id: str, name: str) -> Operation:
        spec = self._specs.get(tenant_id, {}).get(name)
        if spec is None:
            raise UnknownOperation(tenant_id, name)
        return self._public_view(spec)

    def list_ops(self, tenant_id: str) -> list[Operation]:
        return [self._public_view(s) for _, s in
                sorted(self._specs.get(tenant_id, {}).items())]

    # ------------------------------------------------------------ execution
    def execute(self, name: str, params: Mapping[str, Any],
                principal: Principal
                ) -> list[FactEnvelope] | list[EvidenceEnvelope] | CompositeResult:
        """Run one registered op or composite for this principal. Every
        Postgres touch happens inside CompiledOperation.run -> choke.read;
        an unregistered (or out-of-scope) name fails closed."""
        return self.compiled(principal.tenant_id, name).run(
            principal, params, catalog=self)

    def list_for(self, principal: Principal) -> list[Operation]:
        """The catalog as THIS principal sees it: scope-filtered (S5's
        `operations()` surface). Out-of-scope ops are absent, not marked."""
        visible = []
        for name, spec in sorted(self._specs.get(principal.tenant_id, {}).items()):
            scope = getattr(spec, "scope", []) or []
            if scope and not set(scope) & set(principal.roles):
                continue
            if isinstance(spec, CompositeSpec):
                try:
                    for dep in self.closure(principal.tenant_id, name):
                        _check_scope(self.scope_of(principal.tenant_id, dep),
                                     principal, dep)
                except UnknownOperation:
                    continue
            visible.append(self._public_view(spec))
        return visible

    # -------------------------------------------------------------- lookups
    def compiled(self, tenant_id: str, name: str):
        entry = self._compiled.get(tenant_id, {}).get(name)
        if entry is None:
            raise UnknownOperation(tenant_id, name)
        return entry

    def returns_of(self, tenant_id: str, name: str) -> str:
        spec = self._specs.get(tenant_id, {}).get(name)
        if spec is None:
            raise UnknownOperation(tenant_id, name)
        return spec.returns

    def scope_of(self, tenant_id: str, name: str) -> list[str]:
        spec = self._specs.get(tenant_id, {}).get(name)
        if spec is None:
            raise UnknownOperation(tenant_id, name)
        return getattr(spec, "scope", []) or []

    def closure(self, tenant_id: str, name: str) -> set[str]:
        """Every op name a plan could run (the enumerability guarantee made
        callable). A base op's closure is itself."""
        specs = self._specs.get(tenant_id, {})
        out: set[str] = set()

        def walk(n: str) -> None:
            if n in out:
                return
            out.add(n)
            spec = specs.get(n)
            if isinstance(spec, CompositeSpec):
                for step in spec.steps:
                    for op_name in filter(None, (step.op, step.fallback_op)):
                        walk(op_name)

        walk(name)
        return out

    @staticmethod
    def _public_view(spec: Operation) -> Operation:
        params = getattr(spec, "params", {}) or {}
        public = {n: p.model_dump(exclude_defaults=True)
                  for n, p in params.items()} if params else {}
        if isinstance(spec, OperationSpec):
            # Every base op accepts the reserved query-level audit flag
            # (popped in run(), honored by the S2 {cur:} predicate) — the
            # catalog advertises it so callers can discover the audit path.
            public[RESERVED_QUERY_PARAM] = {
                "type": "bool", "default": False,
                "description": "audit/history: also serve retracted"
                               " (valid_to-set) items, honestly labeled"
                               " state='retracted'; default serves only"
                               " current"}
        return Operation(
            name=spec.name, description=spec.description,
            returns=spec.returns,
            params=public,
            notes=spec.notes)


# ======================= the standard serving surface =======================

_NEIGHBORS_HEAD = """WITH RECURSIVE walk(edge_id, next_id, hop) AS (
    SELECT f0.id, f0.object_entity_id, 1
    FROM facts f0
    JOIN entities e0 ON e0.id = f0.subject_entity_id AND {sec:e0}
    WHERE {sec:f0}
      AND {cur:f0}
      AND f0.subject_entity_id = %(entity_id)s
      AND f0.object_entity_id IS NOT NULL
      AND (%(predicate)s::text IS NULL OR f0.predicate = %(predicate)s)
    UNION ALL
    SELECT fn.id, fn.object_entity_id, w.hop + 1
    FROM walk w
    JOIN entities en ON en.id = w.next_id AND {sec:en}
    JOIN facts fn ON fn.subject_entity_id = w.next_id
    WHERE {sec:fn}
      AND {cur:fn}
      AND fn.object_entity_id IS NOT NULL
      AND (%(predicate)s::text IS NULL OR fn.predicate = %(predicate)s)
      AND w.hop < %(depth)s
)
"""

# The canonical dense-ANN evidence template: the ONE query text both the
# minimal S3 `retrieve` op and S4's DenseRetrievalService route through the
# gate. Every candidate is tenant+label filtered ({sec:d} on the label-
# bearing document, {tenant:c} on the label-less chunk) BEFORE it can become
# an EvidenceEnvelope.
DENSE_RETRIEVE_SQL = """SELECT
    c.id AS chunk_id,
    c.document_id,
    c.tenant_id,
    c.content,
    c.contextual_prefix,
    d.title AS document_title,
    c.char_start,
    c.char_end,
    c.locator,
    COALESCE(sl.label, 'public') AS security_label,
    d.security_label_id,
    d.source_timestamp,
    1 - (c.embedding <=> %(query)s::vector) AS score
FROM chunks c
JOIN documents d ON d.id = c.document_id AND {sec:d} AND {cur:d}
LEFT JOIN security_labels sl ON sl.id = d.security_label_id
WHERE {tenant:c}
  AND c.level = 'child'
  AND c.embedding IS NOT NULL
ORDER BY c.embedding <=> %(query)s::vector
LIMIT %(k)s"""


def base_operation_specs() -> list[OperationSpec]:
    """The standard fact/graph surface, as pure spec DATA. Registering these
    for a tenant is the build-time bootstrap; authoring a new op means
    appending a spec shaped like these (see OPERATIONS_NOTES.md)."""
    return [
        OperationSpec(
            name="get_facts",
            description="Facts about a canonical entity, optionally filtered"
                        " by predicate and by the entity's role in the"
                        " triple. Grounding rides the pending_facts join.",
            returns="facts",
            latency="lookup",
            params={
                "entity_id": ParamSpec(type="int", required=True),
                "predicate": ParamSpec(type="str"),
                "role": ParamSpec(type="str", default="any",
                                  choices=["subject", "object", "any"]),
            },
            notes="The old include_expired param was subsumed by the"
                  " query-level include_retracted audit flag (migration"
                  " 009): currency is enforced at the choke point's {cur:}"
                  " predicate now, so an op-local escape could never widen"
                  " it anyway.",
            sql=fact_template(where="""(
        (%(role)s::text IN ('subject', 'any')
         AND f.subject_entity_id = %(entity_id)s)
     OR (%(role)s::text IN ('object', 'any')
         AND f.object_entity_id = %(entity_id)s))
  AND (%(predicate)s::text IS NULL OR f.predicate = %(predicate)s)"""),
        ),
        OperationSpec(
            name="get_by_key",
            description="Resolve a strong identifier (email, tax_id,"
                        " customer_id, asset_id, ...) against the canonical"
                        " registry's attributes and serve the holder's"
                        " current facts — the exact-identifier path;"
                        " verbatim IDs come here, not through retrieval.",
            returns="facts",
            latency="lookup",
            params={
                "identifier": ParamSpec(type="str", required=True),
                "key": ParamSpec(type="str",
                                 description="attribute key to match; any"
                                             " key when omitted"),
                "role": ParamSpec(type="str", default="subject",
                                  choices=["subject", "any"]),
            },
            notes="Two registry entities sharing one identifier value (ER"
                  " under-merge noise) BOTH serve — surfacing the conflict"
                  " beats hiding it.",
            sql=fact_template(where=f"""EXISTS (
        SELECT 1 FROM entities ke
        WHERE {{sec:ke}} AND ke.valid_to IS NULL
          AND (CASE WHEN %(role)s::text = 'any'
               THEN (ke.id = f.subject_entity_id
                     OR ke.id = f.object_entity_id)
               ELSE ke.id = f.subject_entity_id END)
          AND {_key_match('ke')})"""),
        ),
        OperationSpec(
            name="get_entity",
            description="Canonical entity by id OR strong key; serves its"
                        " current facts with the entity riding in each"
                        " envelope's subject ref (a bare registry row has no"
                        " provenance spine, so entities are never served"
                        " standalone).",
            returns="facts",
            latency="lookup",
            params={
                "entity_id": ParamSpec(type="int"),
                "identifier": ParamSpec(type="str"),
                "key": ParamSpec(type="str"),
            },
            require_any=["entity_id", "identifier"],
            sql=fact_template(where=f"""f.subject_entity_id IN (
        SELECT ge.id FROM entities ge
        WHERE {{sec:ge}} AND ge.valid_to IS NULL
          AND ((%(entity_id)s::bigint IS NOT NULL
                AND ge.id = %(entity_id)s)
               OR (%(entity_id)s::bigint IS NULL
                   AND %(identifier)s::text IS NOT NULL
                   AND {_key_match('ge')})))"""),
        ),
        OperationSpec(
            name="neighbors",
            description="Depth-bounded chain traversal over facts"
                        " (subject -> object), optionally single-predicate."
                        " Returns the edge facts along every path; the walk"
                        " is label-filtered on each hop, so a chain never"
                        " extends through a hidden fact or entity.",
            returns="facts",
            latency="traversal",
            params={
                "entity_id": ParamSpec(type="int", required=True),
                "predicate": ParamSpec(type="str"),
                "depth": ParamSpec(type="int", default=1, minimum=1,
                                   maximum=MAX_TRAVERSAL_DEPTH),
            },
            sql=fact_template(head=_NEIGHBORS_HEAD,
                              where="f.id IN (SELECT edge_id FROM walk)"),
        ),
        OperationSpec(
            name="facts_citing",
            description="Facts grounded to one chunk — the surgical"
                        " fact-link from evidence back to assertions"
                        " (complements S4 bare retrieval).",
            returns="facts",
            latency="lookup",
            params={"chunk_id": ParamSpec(type="int", required=True)},
            sql=fact_template(where="f.source_chunk_id = %(chunk_id)s"),
        ),
        OperationSpec(
            name="retrieve",
            description="Dense evidence retrieval (child chunks, best"
                        " first), context fields on. Minimal S3 surface so"
                        " composites can declare evidence steps; S4's"
                        " RetrievalService owns the enrich knob and the"
                        " rerank seam — this op never fills grounded_facts.",
            returns="evidence",
            latency="search",
            params={
                "query": ParamSpec(type="embedding_text", required=True),
                "k": ParamSpec(type="int", default=10, minimum=1, maximum=50),
            },
            sql=DENSE_RETRIEVE_SQL,
        ),
    ]


def entity_dossier_spec() -> CompositeSpec:
    """The first composite: resolve a strong identifier, pull the entity's
    facts, retrieve supporting evidence — a fixed three-step plan whose
    every query is a registered op transiting the gate."""
    return CompositeSpec(
        name="entity_dossier",
        description="Strong identifier -> canonical entity -> its facts +"
                    " bare supporting evidence, per-step enveloped with an"
                    " execution trace.",
        params={
            "identifier": ParamSpec(type="str", required=True),
            "key": ParamSpec(type="str"),
        },
        steps=[
            CompositeStep(
                step="resolve", op="get_by_key",
                bind={
                    "identifier": ParamBinding(source="param",
                                               name="identifier"),
                    "key": ParamBinding(source="param", name="key"),
                    "role": ParamBinding(source="const", value="subject"),
                }),
            CompositeStep(
                step="facts", op="get_facts",
                bind={
                    "entity_id": ParamBinding(source="step", step="resolve",
                                              extract="first_subject_id"),
                    "role": ParamBinding(source="const", value="any"),
                }),
            CompositeStep(
                step="evidence", op="retrieve",
                bind={
                    "query": ParamBinding(source="step", step="resolve",
                                          extract="first_subject_name"),
                }),
        ],
    )


def register_serving_defaults(catalog: InProcessOperationCatalog,
                              tenant_id: str) -> None:
    """Build-time bootstrap: the base surface + entity_dossier for one
    tenant."""
    for spec in base_operation_specs():
        catalog.register(tenant_id, spec)
    catalog.register(tenant_id, entity_dossier_spec())
