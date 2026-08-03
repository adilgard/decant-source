"""Retrieval path — the semantic evidence surface (Build Prompt S4).

Implements the S1 `RetrievalService` seam: query -> embed -> dense ANN ->
(rerank seam) -> `EvidenceEnvelope`s. S3 registered a MINIMAL `retrieve` op
so composites could declare evidence steps; THIS module is the real service
— the enrich knob, the rerank seam, the dormant hybrid mode. Both route the
same canonical template (operations.DENSE_RETRIEVE_SQL) through the same
gate, so `entity_dossier`'s evidence step and this service can never drift.

THE ANN QUERY IS JUST ANOTHER GATED READ. The query text is embedded, then
the pgvector search runs as a SELECT with `{sec:<alias>}` security markers
through `PostgresChokePoint.read(fq, sql, params)` — the candidate set is
tenant+label filtered BEFORE it becomes evidence. No new DB door, no special
access: the service holds a choke point and an embedder, never a connection.
Retrieval cannot surface a chunk the caller may not see, because such a
chunk never enters the candidate set (permission-invisibility, the absence
rule — inherited from S2, not reimplemented here).

SERVED CONFIG IS EXACTLY THE AXIS-C DECISION: bge-m3, PREFIX-FREE, dense.
The query text goes to the embedder VERBATIM — no instruction prefix, no
task template (QUERY_PREFIX below is the decision made greppable). Hybrid
dense+sparse fusion sits dormant behind `retrieval_mode` and is OFF by
default (Axis C round 3: dense holds; hybrid measured WORSE on SOP) — the
code exists so flipping the mode is a config change, not a build, but the
served default is dense and nothing unvalidated rides the default path.

THE ONE MEANINGFUL KNOB IS `enrich` (Decision 2c: grounded_facts opt-in;
default off = bare-fast). Enrichment REUSES S3: each envelope's
grounded_facts come from the registered `facts_citing` op via the catalog —
so every attached FactEnvelope inherits S3's referential filtering (both
triple ends label-checked, grounding via the pending_facts join). Fact
projection is never hand-rolled here. Context fields (contextual_prefix,
title, section) are default-on, part of the envelope — the `bare`
context-stripping knob was DROPPED as speculative; add one only if the
usage logs (S1 instrumentation) show a measured payload/latency need.

THE RERANKER IS A SEAM, NOT A COMPONENT. The path always calls
`Reranker.rerank()` between the ANN candidates and the caller; the shipped
implementation is a pass-through no-op. When BGE-reranker-v2 is built
(Decision 2b), it implements the same seam and is benchmarked with/without
through the harness — callers never restructure. Ranks are stamped AFTER
the seam, so a future reranker's ordering is what `signal.rank` reports.

Evidence carries retrieval signal (score/rank/mode/query) + provenance,
never a confidence-of-truth field — S1 type discipline, structurally
enforced by the envelope itself.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from knowledge_hub.choke_point import PostgresChokePoint
from knowledge_hub.factstore_pg import vector_literal
from knowledge_hub.interfaces import Embedder
from knowledge_hub.operations import (
    DENSE_RETRIEVE_SQL,
    InProcessOperationCatalog,
    evidence_envelope_from_row,
)
from knowledge_hub.serving import (
    EvidenceEnvelope,
    Principal,
    RetrievalQuery,
    RetrievalService,
)

# Axis C, decided: bge-m3 served prefix-free. The query text is embedded
# verbatim — this constant IS the "prefix-free" decision in greppable form;
# it must stay empty until a benchmark run says otherwise.
QUERY_PREFIX = ""

# The modes this service knows how to serve. 'dense' is the Axis-C default;
# 'hybrid' is DORMANT — constructible for benchmarking, never the default.
RETRIEVAL_MODES = ("dense", "hybrid")

# Retrieval depth bounds (mirrors the S3 op's k<=50 ParamSpec).
MAX_K = 50

# Standard reciprocal-rank-fusion constant (dormant hybrid path only).
RRF_K = 60

# Dormant hybrid fusion: dense ANN + native tsvector keyword ranks, fused
# with RRF. Same gate discipline as the dense template — every branch that
# touches a label-bearing table carries {sec:d}, every chunks touch carries
# {tenant:c}; a candidate is filtered before it can be fused, let alone
# served. NOT the default path (Axis C round 3: hybrid was worse on SOP);
# it exists so a future benchmark flip is a config change, not a build.
HYBRID_RETRIEVE_SQL = """WITH dense AS (
    SELECT c.id AS chunk_id,
           ROW_NUMBER() OVER (
               ORDER BY c.embedding <=> %(query)s::vector) AS r
    FROM chunks c
    JOIN documents d ON d.id = c.document_id AND {sec:d} AND {cur:d}
    WHERE {tenant:c}
      AND c.level = 'child'
      AND c.embedding IS NOT NULL
    ORDER BY c.embedding <=> %(query)s::vector
    LIMIT %(pool)s
), sparse AS (
    SELECT c.id AS chunk_id,
           ROW_NUMBER() OVER (
               ORDER BY ts_rank(c.content_tsv,
                                plainto_tsquery('english', %(query_text)s))
                        DESC, c.id) AS r
    FROM chunks c
    JOIN documents d ON d.id = c.document_id AND {sec:d} AND {cur:d}
    WHERE {tenant:c}
      AND c.level = 'child'
      AND c.content_tsv @@ plainto_tsquery('english', %(query_text)s)
    LIMIT %(pool)s
), fused AS (
    SELECT chunk_id,
           COALESCE(1.0 / (%(rrf_k)s + dense.r), 0)
         + COALESCE(1.0 / (%(rrf_k)s + sparse.r), 0) AS score
    FROM dense FULL OUTER JOIN sparse USING (chunk_id)
)
SELECT
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
    fused.score
FROM fused
JOIN chunks c ON c.id = fused.chunk_id
JOIN documents d ON d.id = c.document_id AND {sec:d} AND {cur:d}
LEFT JOIN security_labels sl ON sl.id = d.security_label_id
WHERE {tenant:c}
ORDER BY fused.score DESC, c.id
LIMIT %(k)s"""


# ------------------------------------------------------------- rerank seam --
class Reranker(ABC):
    """The rerank seam (Decision 2b): sits between the ANN candidate list
    and the caller, always called. A real reranker (BGE-reranker-v2) drops
    in by implementing THIS and being handed to the service constructor —
    no caller or service restructuring. It may reorder and truncate; it must
    never add candidates (everything it sees already transited the gate)."""

    @abstractmethod
    def rerank(self, query: str,
               candidates: list[EvidenceEnvelope]) -> list[EvidenceEnvelope]:
        """Candidates in, candidates out, best first."""


class PassThroughReranker(Reranker):
    """The shipped no-op: ANN order is served order. Exists so the path
    always calls the seam — with/without benchmarking (Decision 2b) then
    compares rerankers, never code paths."""

    def rerank(self, query: str,
               candidates: list[EvidenceEnvelope]) -> list[EvidenceEnvelope]:
        return list(candidates)


# -------------------------------------------------------------- the service --
class DenseRetrievalService(RetrievalService):
    """The S4 evidence surface: embed (bge-m3, prefix-free) -> gated dense
    ANN -> rerank seam -> ranked EvidenceEnvelopes; grounded facts opt-in
    via S3's facts_citing. Holds a choke point, an embedder, and the op
    catalog — never a connection."""

    def __init__(self, choke: PostgresChokePoint, embedder: Embedder,
                 catalog: InProcessOperationCatalog, *,
                 reranker: Optional[Reranker] = None,
                 retrieval_mode: str = "dense"):
        if retrieval_mode not in RETRIEVAL_MODES:
            raise ValueError(
                f"retrieval_mode must be one of {RETRIEVAL_MODES}, "
                f"got {retrieval_mode!r}")
        self._choke = choke
        self._embedder = embedder
        self._catalog = catalog
        self._reranker = reranker or PassThroughReranker()
        self._mode = retrieval_mode

    @property
    def retrieval_mode(self) -> str:
        """The served mode — 'dense' unless explicitly constructed otherwise
        (and nothing in the default path constructs otherwise)."""
        return self._mode

    # ------------------------------------------------------------- S1 seam
    def retrieve(self, query: RetrievalQuery, principal: Principal, *,
                 enrich: bool = False) -> list[EvidenceEnvelope]:
        if not isinstance(query, RetrievalQuery):
            raise TypeError(
                f"retrieve() takes a RetrievalQuery, got {type(query).__name__}")
        if not 1 <= query.k <= MAX_K:
            raise ValueError(f"k must be between 1 and {MAX_K}, got {query.k}")

        # Enforce FIRST: the search runs only under the minted FilteredQuery.
        fq = self._choke.enforce(query, principal)

        # Axis C: the query text is embedded VERBATIM (prefix-free).
        text = QUERY_PREFIX + query.text
        vec = vector_literal(self._embedder.embed([text])[0])

        if self._mode == "dense":
            rows = self._choke.read(fq, DENSE_RETRIEVE_SQL,
                                    {"query": vec, "k": query.k})
        else:  # dormant hybrid — reachable only by explicit construction
            rows = self._choke.read(fq, HYBRID_RETRIEVE_SQL, {
                "query": vec, "query_text": query.text,
                "pool": max(query.k * 4, 20), "rrf_k": RRF_K, "k": query.k})

        candidates = [
            evidence_envelope_from_row("retrieve", row, rank=ix + 1,
                                       query_text=query.text, mode=self._mode)
            for ix, row in enumerate(rows)]

        # The seam is ALWAYS called; ranks are stamped after it, so a real
        # reranker's ordering is what signal.rank reports.
        ranked = self._reranker.rerank(query.text, candidates)
        for ix, env in enumerate(ranked):
            env.signal.rank = ix + 1

        if enrich:
            # Decision 2c opt-in, via S3: facts_citing is a registered op
            # transiting the gate, so each attached FactEnvelope inherits
            # referential filtering (both triple ends label-checked) and the
            # grounding join. Fact projection is never hand-rolled here.
            # The query's temporal audit scope is forwarded so an
            # include_retracted evidence read grounds against the same
            # temporal window it was served from.
            for env in ranked:
                env.grounded_facts = self._catalog.execute(
                    "facts_citing",
                    {"chunk_id": env.spine.chunk_id,
                     "include_retracted": query.include_retracted},
                    principal)
        return ranked
