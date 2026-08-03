# Operation registry, base ops, composites — Build Prompt S3 notes

`knowledge_hub/operations.py` implements the S1 `OperationRegistry` seam:
the fact/graph query surface (Path C's default) and the authoring machinery
behind it. Every op it registers routes through the S2 gate
(`PostgresChokePoint.enforce()` → `read()`) — there is no other door to
Postgres on the serve path, and a compiled op holds a choke point, never a
connection.

## The shape of the thing

| Piece | What it is |
|---|---|
| `OperationSpec` | One base op as DATA: name, typed params (`ParamSpec`), a `{sec:<alias>}`-marked SQL template, permission `scope` (roles, ANY-of), `latency` class (`lookup`/`traversal`/`search`) |
| `OperationGenerator` | Compiles specs into executables; REJECTS anything outside the grammar |
| `CompositeSpec` | A fixed declared plan: ordered `CompositeStep`s over already-registered ops with explicit `ParamBinding` data-flow |
| `InProcessOperationCatalog` | The per-tenant S1 registry; `register` = build-time authoring, `execute` = the run path, `get`/`list_ops` = the sanitized public catalog (no SQL leaves the authoring side) |
| `CompositeResult` | Per-step envelopes (facts as facts, evidence as evidence, tagged by step — never flattened) + the execution trace |

Both spec types SUBCLASS the S1 `Operation` — a spec *is* catalog data, and
S3 adds no new envelope shape. One vocabulary word was added to S1 in
lock-step: `OPERATION_RETURNS` gained `'composite'`.

## "Unfiltered op is unwritable" extends to authoring

The generator refuses, at registration:

* a template with no `{sec:<alias>}` marker (the headline rule);
* multi-statement SQL, or anything not starting `SELECT`/`WITH`;
* brace tokens outside the `{sec:x}` / `{tenant:x}` marker grammar;
* bind placeholders the spec did not declare as params;
* param names in the gateway's reserved `kh_` namespace;
* an evidence op that does not declare exactly one `embedding_text` param;
* a bare S1 `Operation` (data with no template — nothing executable, and
  nothing filtered, can be generated from it).

The S2 gate re-checks all of it at run time; authoring rejection just moves
the failure to registration, where the author is watching. Registration is
BUILD-TIME — the system never mints an op or a query at runtime.

## Base operations (all transit the gate)

* `get_facts(entity_id, predicate?, role?, include_expired?)` — facts about
  a canonical entity; `role` picks subject/object/any side of the triple.
* `get_by_key(identifier, key?, role?)` — THE exact-identifier path
  (resolves the Axis-C round-3 leakage caveat: verbatim IDs come here, not
  through retrieval). Matches strong keys (email, tax_id, customer_id,
  asset_id, …) against `entities.attributes`, where resolution merges each
  mention's `extracted_keys`. Two entities sharing one identifier value
  (ER noise) BOTH serve — surfacing the conflict beats hiding it.
* `get_entity(entity_id | identifier[, key])` — id-or-key resolution sugar.
  Entities are served as the `subject` `EntityRef` inside fact envelopes: a
  bare registry row cannot build a `ProvenanceSpine`, which is the S1 type
  system saying entities are not standalone servables. An entity with no
  visible facts serves empty — indistinguishable from absent, exactly what
  the absence rule requires.
* `neighbors(entity_id, predicate?, depth<=5)` — chain traversal as a
  recursive CTE over `facts` (AGE `cypher()` can't bind params — S2
  decision). Hop-bounded, so cycles terminate; the walk is label-filtered
  on EVERY hop — facts and the entities it steps through — so a chain
  never extends through a hidden edge or node.
* `facts_citing(chunk_id)` — the surgical fact-link from one chunk back to
  the facts grounded in it (also the enrichment route for S4 retrieval).
* `retrieve(query, k<=50)` — minimal dense evidence retrieval so composites
  can declare evidence steps; it shares S4's canonical template
  (`DENSE_RETRIEVE_SQL`), so context fields (prefix/title/section) are on.
  S4's `RetrievalService` (retrieval.py) owns the `enrich` knob and the
  rerank seam; this op never fills `grounded_facts`.

Envelope mechanics, common to every fact op:

* **Grounding via JOIN, not column**: `facts` has no grounding column; the
  verdict is joined from `pending_facts.grounding` via `promoted_fact_id`
  (migration 004).
* **document_id always resolves**: chunk-only provenance backfills through
  the chunks join (`COALESCE(f.source_document_id, c.document_id)`).
* **Both triple ends label-checked**: subject entities join under `{sec:}`;
  a fact whose entity-object is hidden is dropped whole — a served fact
  never names a hidden entity.
* **State assignment is deterministic**: `oversized` → `under_review` (the
  review queue owns it); flagged grounding (`span_missing` /
  `components_missing`) or a promoted `needs_review` flag →
  `known_low_confidence`; otherwise `known_confident`. `unresolved` never
  applies — these ops serve `facts`, which only holds resolved rows.

`scope` is enforced as *invisibility*: a principal outside an op's role
scope gets `UnknownOperation` and the op is absent from `list_for()` — the
absence rule applied to the catalog itself.

## Composites: fixed plans, enumerable by construction

* A `CompositeStep` names a registered op, binds its params from a fixed
  grammar (`param:` composite input, `const:`, or `step:<label>.<extractor>`
  over an EARLIER step's envelopes), and may declare ONE `fallback_op`
  (tried only when the primary returns zero envelopes; must return the
  same envelope kind).
* Extractors are a fixed vocabulary (`STEP_EXTRACTORS`: first_subject_id,
  first_subject_name, first_object_id, first_chunk_id). There is no field
  to hang an `if`, a loop, or a router on — `extra="forbid"` makes
  content-dependent plan shape unexpressible, so every op a composite could
  run is enumerable from its spec (`catalog.closure()`).
* Registration flattens the plan against the live catalog: unregistered
  refs, forward/self references, unknown extractors, kind-mismatched
  extractors, unbound required params, and cycles are all REJECTED — and
  every existing plan is re-validated on ANY registration, so a
  replace-by-name that would entangle a dependent composite is refused
  atomically.
* Execution: each step runs as its own gated op (fresh `enforce()` per
  step); a step whose binding source produced nothing is SKIPPED and
  recorded (`skipped_empty_input`) — the degenerate case of data-flow, not
  control flow. Results stay per-step (`StepResult`), and the trace records
  op, status, caller-visible params (raw text, never vectors), envelope
  count, and wall time. Permission filtering and bounds are inherited from
  the steps — the S2 boundary makes this free.

First composite: `entity_dossier(identifier[, key])` =
`get_by_key(role=subject)` → `get_facts(role=any)` → `retrieve` (bare,
enrich-free). Nested composites are allowed (downward-acyclic); their steps
surface under dotted labels (`outer.inner`).

## Authoring workflow (adding an op / composite)

1. **Base op**: append an `OperationSpec` to your registration site (or
   `base_operation_specs()` if it's standard surface). Compose the SQL with
   `fact_template(where=..., head=...)` for fact ops — it owns the
   projection contract, entity-ref joins, chunk backfill, and grounding
   join; you write the WHERE. Alias vocabulary: `f/se/oe/c/sl/pf` (+ your
   own aliases, each label-bearing table marked `{sec:x}`, label-less
   `{tenant:x}`).
2. Declare every bind placeholder as a typed `ParamSpec` (use
   `embedding_text` for text the op should embed; pair with `::vector`).
3. `catalog.register(tenant_id, spec)` — at build time. If it registers, it
   is filtered; if it isn't filtered, it doesn't register.
4. **Composite**: write a `CompositeSpec` naming only registered ops, bind
   params via the fixed grammar, register the same way. If you find
   yourself wanting a condition or a loop, you are writing a router — stop;
   that shape belongs to the (future) agent layer, not the registry.
5. Add a test that runs the op through the catalog against the real stack —
   tenant isolation and label absence come free, but assert them anyway
   (verify, don't trust).

## Tests

`tests/test_operations.py` (real Postgres + live bge-m3, no mocks; 19
tests, full suite 147 green): unmarked/off-grammar/bare specs rejected at
registration; a generated op reaches Postgres exactly once per run through
`PostgresChokePoint.read` and an unregistered ask never touches the gate;
correct envelopes incl. the grounding join and state assignment; strong-key
resolution (named-key, any-key, miss); depth-bounded recursive CTE with
predicate filter and depth-cap refusal; tenant/label filtering verified on
every op shape (identical sibling-tenant data, restricted labels, silent
absence); traversal never bridges a hidden node; scoped ops invisible, not
forbidden; composite rejections (unregistered ref, content-dependent shape
unexpressible, forward refs, unknown extractors, cycle via replace-by-name
— refused without clobbering); fallback chain traced; `entity_dossier`
end-to-end (facts as facts, evidence as evidence, per-step trace, three
gate transits, skip-on-empty, permission inheritance for blind vs granted
callers).

## Carried notes for S4/S5

* S4 landed (retrieval.py, RETRIEVAL_NOTES.md): `DenseRetrievalService`
  reuses the gate the same way and shares the `retrieve` op's canonical
  template, so the op and the service cannot drift; enrichment routes
  through this catalog's `facts_citing`.
* S5 landed (service_http.py, SERVICE_NOTES.md): opens the `UsageTracker`
  per request and flattens facts/evidence into `ServingResponse`;
  `CompositeResult` keeps the per-step tags for the HTTP trace/audit
  surface, and the endpoint set is generated from THIS catalog.
* Package 0.9.0 (pyproject + `__init__`, dist metadata refreshed via
  `uv pip install -e` — it had silently drifted to 0.6.0 since the
  benchmark-era reinstall; check `importlib.metadata.version` after every
  bump because benchmark provenance pins it).
