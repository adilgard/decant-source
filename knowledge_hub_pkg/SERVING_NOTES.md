# Serving contracts — Build Prompt S1 notes

`knowledge_hub/serving.py` is the serving layer's foundation, the way
`models.py` + `interfaces.py` were for ingestion: S2 (choke point), S3
(operations), S4 (retrieval), S5 (API surface) implement these ABCs and
return these shapes — they add no new response types.

## The two envelopes (relevant ≠ true, carried by the type system)

| | `FactEnvelope` | `EvidenceEnvelope` |
|---|---|---|
| Is | an assertion the agent may **act** on | relevant **text**, not asserted truth |
| Trust signal | `state` (UncertaintyState, required) + provisional `confidence` | none — `signal.score` is relevance to the *query* |
| Forbidden field | any similarity/rank/score | any confidence-of-truth / state / grounding |

Both are `extra="forbid"`, so the forbidden field is a `ValidationError`,
not a convention. They are distinct types, never a union — a call site that
wants to act must hold a `FactEnvelope` (evidence can carry them only via
opt-in `grounded_facts`, default `[]`).

Both carry the shared **spine** (`ProvenanceSpine`): `tenant_id`, the
provenance triple `document_id -> chunk_id -> char span` (+ structured-track
`locator`), and the `security_label` the item was served under.
`document_id` is always resolved by the serving layer; `chunk_id` is None
for structured-track facts and **required** for evidence (validated).

## Uncertainty states

`known_confident / known_low_confidence / under_review / unresolved /
unknown` — over-provisioned by design; fold rarely-used states later on the
usage-log evidence, never grow ad hoc.

**The absence rule:** absence is never "false" and never "unknown". The
choke point silently drops what the principal may not see — logically
*before* states apply. States describe only visible knowledge; `unknown`
means "no assertion either way", never "hidden".

`confidence` is PROVISIONAL (uncalibrated until Axes B/D); the discrete
state is the primary trust signal.

## Seams (stubs now; implementer in parens)

- `ChokePoint.enforce(query, principal) -> FilteredQuery` (S2). The single
  mandatory permission gate. `FilteredQuery` subclasses `RetrievalQuery` and
  is proof-of-passage: S4 index code accepts only `FilteredQuery`, so a
  missing permission filter is a *type error*.
- `Operation` (declarative spec, data-not-code) + `OperationRegistry`
  (per-tenant catalog; unregistered ask fails closed with
  `UnknownOperation`) (S3).
- `RetrievalService.retrieve(query, principal, *, enrich=False)
  -> list[EvidenceEnvelope]` (S4). `enrich` is the ONE knob: it populates
  `grounded_facts` (the only way evidence carries facts). The former
  `bare` context-stripping knob was DROPPED with S4 as speculative —
  context fields (`contextual_prefix`, title, section) are default-on,
  part of the envelope; reintroduce a strip knob only on a measured
  payload/latency need from the usage logs.
- `ServingService.execute(operation, params, principal) -> ServingResponse`
  + `.operations(principal)` (S5). The only doorway callers talk to.

## Usage instrumentation (Decision 4a/4b: serve maximal, strip on evidence)

`UsageTracker` (one per request_id, S5 opens it) wraps envelopes in
`TrackedEnvelope` — a read-through proxy that records every model-field
access structurally, plus the state *values* observed via `.state` (the
branch evidence). `flush()` emits one `EnvelopeUsage` per envelope to a
`UsageRecorder` (ABC; `InMemoryUsageRecorder` is the test double and shows
the aggregation shape — `field_read_counts` answers "has anyone ever read
this field"). A field is stripped / a state folded only when the logs show
non-use.

## Lock-step notes

- Spine mirrors the facts/chunks provenance columns; `security_label` is the
  resolved label text (NULL `security_label_id` serves as `'public'`).
- `grounding` on a served fact uses the `pending_facts.grounding` vocabulary
  (migration 004); for promoted facts it is joined via
  `pending_facts.promoted_fact_id` — there is no grounding column on `facts`.
- No new tables in S1. When S3/S5 persist operation registries or usage
  logs, that migration must update this note and models.py in the same
  commit.
- S3 added `'composite'` to `OPERATION_RETURNS`: a fixed declared plan over
  registered ops (operations.py). Its result (`CompositeResult`) preserves
  per-step envelopes — facts as facts, evidence as evidence — and adds no
  new envelope type. S3 registered ops in-process (build-time authoring);
  no registry table, so no migration.

## Tests

`tests/test_serving_contracts.py` — pure Pydantic, no DB: spine required on
both envelopes; fact requires a state; evidence rejects truth-confidence;
fact rejects similarity; span/object/grounding/returns/kind vocabularies;
ABCs abstract; `retrieve` keyword-only defaults; tracker records
fields-read + states-branched and ignores non-field access.
