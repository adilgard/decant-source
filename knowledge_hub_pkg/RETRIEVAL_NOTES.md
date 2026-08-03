# Retrieval path — semantic evidence surface (Build Prompt S4)

`knowledge_hub/retrieval.py` implements the S1 `RetrievalService` seam:
query → embed (bge-m3, prefix-free) → gated dense ANN → rerank seam →
`EvidenceEnvelope`s, grounded facts opt-in. Package 0.10.0.

## Served config = the Axis-C decision (verified, not assumed)

| Axis-C decision | Where it is enforced |
|---|---|
| bge-m3 | `OllamaEmbedder` (settings.embedding_model), asserted live in tests |
| prefix-free | `QUERY_PREFIX = ""` — the query text reaches the embedder VERBATIM; a spy test asserts it at the boundary |
| dense | `retrieval_mode="dense"` is the constructor default; the dense template is the only default-path SQL |

Nothing unvalidated rides the default path. `OLLAMA_HOST` /
`settings.ollama_host` points at the GPU host (Ollama runs native there).

## The ANN query is just another gated read

No new DB door, no special access. The service holds a `PostgresChokePoint`,
an `Embedder`, and the op catalog — never a connection. `retrieve()` calls
`enforce()` FIRST and runs the search only under the minted `FilteredQuery`,
through `choke.read()`, with `{sec:d}` on documents (label-bearing) and
`{tenant:c}` on chunks (label lives on the parent document) — so every
candidate is tenant+label filtered BEFORE it can become evidence. A chunk
the caller may not see never enters the candidate set
(permission-invisibility; the absence rule, inherited from S2).

The dense template is SHARED with the S3 `retrieve` op
(`operations.DENSE_RETRIEVE_SQL`) — one canonical query text, so the minimal
op (used by `entity_dossier`'s evidence step) and the real service cannot
drift. With S4, the canonical evidence projection carries the context fields
(`contextual_prefix`, `d.title AS document_title`; `section` is derived from
the chunk locator's heading path) — context is default-on everywhere
evidence is served.

## The ONE knob: `enrich` (Decision 2c)

- `enrich=False` (default): `grounded_facts` stays `[]` — bare-fast.
- `enrich=True`: each envelope's `grounded_facts` is filled by calling
  **S3's registered `facts_citing` op through the catalog** — never
  hand-rolled fact projection. The attached `FactEnvelope`s therefore
  inherit S3's referential filtering (BOTH triple ends label-checked: a
  fact naming a hidden entity is absent) and the grounding verdict via the
  `pending_facts.promoted_fact_id` join. Verified by test: hiding the
  object entity of a grounded relation removes that fact from enrichment
  while the literal-object fact stays.

The `bare` context-stripping knob that S1 sketched was **DROPPED** as
speculative (context fields are default-on, above). Do not add a
context-stripping knob unless the S1 usage logs show a measured
payload/latency need. The S1 ABC signature was updated accordingly
(`retrieve(query, principal, *, enrich=False)`), with the contract test.

## Rerank seam (Decision 2b): a stage, not a component

`Reranker.rerank(query, candidates)` sits between the ANN candidates and
the caller and is ALWAYS called; the shipped `PassThroughReranker` is a
clean no-op (ANN order = served order). When BGE-reranker-v2 gets built it
implements this seam and is handed to the constructor — no caller or
service restructuring — and the harness benchmarks with/without by
comparing rerankers, never code paths. Ranks (`signal.rank`) are stamped
AFTER the seam, so a real reranker's ordering is what rank reports. A
reranker may reorder/truncate, never add (everything it sees already
transited the gate).

## Hybrid: dormant, OFF by default

Axis C round 3: dense holds; hybrid measured WORSE on SOP. The RRF fusion
path (dense ANN + native tsvector keyword) exists in
`HYBRID_RETRIEVE_SQL` behind `retrieval_mode="hybrid"` so a future
benchmark flip is a config change, not a build — but the constructor
default is `"dense"`, an unknown mode is a construction error, and nothing
in the default path constructs hybrid. The dormant path carries the same
security markers and is tested for tenant isolation (dormant ≠ ungated).

## Envelope discipline (S1, unchanged)

Evidence carries the retrieval signal (`score`/`rank`/`mode`/`query`) plus
the provenance spine — never a confidence-of-truth field; `extra="forbid"`
keeps that structural. Retrieval relevance is a statement about the QUERY.

## Tests

`tests/test_retrieval.py` (real Postgres + LIVE bge-m3 embeddings on both
sides — chunks embedded at seed time, query embedded at retrieve time, so
relevance is real cosine similarity; 12 tests, full suite green):
relevance ranking with full citations and context fields; k bounds;
tenant + label filtering (identical text seeded cross-tenant and
above-grant — never surfaces; granted insider sees it); exactly one gate
transit per retrieve, marker-carrying, no connection held; enrich on/off,
routed through `facts_citing` (spied), referential filtering on hidden
entities; rerank seam called + no-op by default, a reversing reranker
slots in via the constructor with ranks re-stamped; served config =
Axis-C (dense default, empty prefix asserted at the embedder boundary,
bge-m3); hybrid constructible-but-not-default, unknown mode refused,
dormant hybrid still tenant-gated.

## Carried notes for S5

* S5 landed (service_http.py, SERVICE_NOTES.md): ONE `DenseRetrievalService`
  per process behind `POST /v1/retrieve`; `enrich` maps to a caller param.
* If/when the reranker lands: implement `Reranker`, hand it to the
  constructor, benchmark with/without through the harness (Decision 2b).
* Package 0.10.0 (pyproject + `__init__`; after `uv pip install -e`, check
  `importlib.metadata.version` — benchmark provenance pins it and it has
  drifted before).
