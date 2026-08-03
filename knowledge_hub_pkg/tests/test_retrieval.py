"""Retrieval path — the semantic evidence surface (Build Prompt S4), tested
against the REAL pilot stack with live bge-m3 embeddings.

What must hold:

* a query returns relevant chunks as EvidenceEnvelopes with citations
  (provenance spine) and a dense retrieval signal;
* every candidate is gated (tenant + label) BEFORE it becomes evidence —
  cross-tenant and above-grant chunks never surface, and enrichment never
  attaches a fact naming a hidden entity;
* `enrich=True` attaches the correct grounded facts VIA S3's facts_citing
  (each itself referentially filtered); `enrich=False` attaches none;
* the rerank seam is always called and is a clean no-op — a future reranker
  slots in without touching callers;
* served config is exactly the Axis-C decision (bge-m3, prefix-free, dense);
  hybrid stays off by default.

Embeddings are LIVE: chunks are embedded with real bge-m3 at seed time and
the query is embedded by the same model at retrieve time, so relevance is
real cosine similarity — not synthetic unit vectors.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import psycopg
import pytest
from psycopg.rows import dict_row

from factories import ONTOLOGY, make_chunk, make_entity, make_raw, sha

from knowledge_hub.choke_point import PostgresChokePoint
from knowledge_hub.models import ChunkLevel, DocType, Document, Fact
from knowledge_hub.operations import (
    InProcessOperationCatalog,
    register_serving_defaults,
)
from knowledge_hub.retrieval import (
    QUERY_PREFIX,
    Reranker,
    DenseRetrievalService,
    PassThroughReranker,
)
from knowledge_hub.serving import (
    EvidenceEnvelope,
    FactEnvelope,
    Principal,
    RetrievalQuery,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def choke(test_dsn: str) -> PostgresChokePoint:
    cp = PostgresChokePoint(dsn=test_dsn)
    yield cp
    cp.close()


@pytest.fixture(scope="module")
def db(test_dsn: str) -> psycopg.Connection:
    conn = psycopg.connect(test_dsn, autocommit=True, row_factory=dict_row)
    yield conn
    conn.close()


@pytest.fixture()
def catalog(choke, embedder, tenant) -> InProcessOperationCatalog:
    cat = InProcessOperationCatalog(choke, embedder)
    register_serving_defaults(cat, tenant)
    return cat


@pytest.fixture()
def service(choke, embedder, catalog) -> DenseRetrievalService:
    return DenseRetrievalService(choke, embedder, catalog)


def principal_for(tenant: str, roles: list[str] | None = None) -> Principal:
    return Principal(tenant_id=tenant, principal_id="test-caller",
                     roles=roles or [])


def make_label(db: psycopg.Connection, role: str | None = None) -> int:
    label_id = db.execute(
        "INSERT INTO security_labels (label, description)"
        " VALUES (%s, 's4 retrieval test') RETURNING id",
        (f"lbl-{uuid.uuid4().hex[:12]}",)).fetchone()["id"]
    if role is not None:
        db.execute(
            "INSERT INTO label_role_grants (label_id, role) VALUES (%s, %s)",
            (label_id, role))
    return label_id


def seed_passage(pipeline, store, db, embedder, tenant: str, *, text: str,
                 title: str, section: str | None = None,
                 label_id: int | None = None, name: str = "Granite Botanicals",
                 asset_id: str | None = None,
                 grounding: str | None = "pass") -> SimpleNamespace:
    """One servable passage embedded with LIVE bge-m3: a document -> one
    embedded child chunk carrying `text`, plus a small fact chain grounded
    to that chunk (so enrich has real facts to attach and the grounding
    verdict rides pending_facts, same shape as the S3 tests)."""
    raw = make_raw(tenant)
    pipeline.ingest_raw(raw)
    doc = Document(tenant_id=tenant, raw_document_id=raw.id,
                   doc_type=DocType.prose, title=title,
                   security_label_id=label_id)
    store.insert_document(doc)
    parent = make_chunk(tenant, doc.id)
    store.insert_chunks([parent])

    locator = {"section": 0, "heading": section,
               "heading_path": section} if section else None
    vec = embedder.embed([text])[0]
    # content_hash is explicitly randomized: filter tests seed IDENTICAL
    # text across tenants, and ux_chunk_hash is a global unique index.
    child = make_chunk(tenant, doc.id, level=ChunkLevel.child,
                       parent_chunk_id=parent.id, content=text,
                       content_hash=sha(),
                       contextual_prefix=f"[{title}] {text[:24]}",
                       locator=locator, embedding=vec,
                       embedding_model="bge-m3")
    store.insert_chunks([child])

    key = asset_id or f"EQ-{uuid.uuid4().hex[:8]}"
    subj = make_entity(tenant, name, security_label_id=label_id,
                       attributes={"asset_id": key})
    mid = make_entity(tenant, f"{name} Site", security_label_id=label_id)
    for e in (subj, mid):
        store.upsert_entity(e)

    def fact(subject_id, predicate, *, obj=None, literal=None) -> Fact:
        return Fact(tenant_id=tenant, subject_entity_id=subject_id,
                    predicate=predicate, object_entity_id=obj,
                    object_literal=literal, ontology_version=ONTOLOGY,
                    source_document_id=doc.id, source_chunk_id=child.id,
                    extractor="test", extractor_version="s4",
                    confidence=0.9, security_label_id=label_id)

    rel_id, attr_id = store.write_facts([
        fact(subj.id, "operates", obj=mid.id),
        fact(subj.id, "references", literal="Net-30"),
    ])

    if grounding is not None:
        db.execute(
            """
            INSERT INTO pending_facts
              (tenant_id, subject_ref, predicate, object_ref,
               ontology_version, source_document_id, source_chunk_id,
               extractor, extractor_version, security_label_id,
               resolution_status, promoted_fact_id, grounding)
            VALUES (%s, %s, 'operates', %s, %s, %s, %s, 'test', 's4', %s,
                    'promoted', %s, %s)
            """,
            (tenant, f"entity:{subj.id}", f"entity:{mid.id}", ONTOLOGY,
             doc.id, child.id, label_id, rel_id, grounding))

    return SimpleNamespace(doc=doc, child=child, subj=subj, mid=mid,
                           rel_id=rel_id, attr_id=attr_id, asset_id=key,
                           fact_ids={rel_id, attr_id})


# ---------------------------------------------------------------------------
# Deliverable 1 — a query returns relevant evidence with citations
# ---------------------------------------------------------------------------


def test_query_returns_relevant_evidence_with_citations(
        service, pipeline, store, db, embedder, tenant):
    """Real bge-m3: the on-topic passage outranks the off-topic one, and
    every result is a fully-cited EvidenceEnvelope carrying a dense signal
    (score/rank/mode/query) and NO truth-confidence field."""
    on = seed_passage(
        pipeline, store, db, embedder, tenant,
        title="Substrate Handling SOP", section="Sterilization",
        text="Sterilize the coco coir substrate in the autoclave at 121C "
             "for 90 minutes before inoculation to prevent contamination.")
    off = seed_passage(
        pipeline, store, db, embedder, tenant,
        title="HR Onboarding", name="Payroll Dept",
        text="New employees submit their direct-deposit banking details "
             "through the payroll portal during their first week.")

    evs = service.retrieve(
        RetrievalQuery(text="how do I sterilize growing substrate?"),
        principal_for(tenant))

    assert evs and all(isinstance(e, EvidenceEnvelope) for e in evs)
    top = evs[0]
    assert top.spine.chunk_id == on.child.id            # relevant one wins
    assert off.child.id in {e.spine.chunk_id for e in evs}  # both visible

    # Citation: the provenance spine points back to the exact bytes.
    assert top.spine.document_id == on.doc.id
    assert top.spine.tenant_id == tenant
    assert top.spine.chunk_id is not None

    # Dense retrieval signal — a statement about the QUERY.
    assert top.signal.mode == "dense"
    assert top.signal.rank == 1
    assert top.signal.query == "how do I sterilize growing substrate?"
    assert top.signal.score > evs[1].signal.score       # ranked, best first
    assert 0.0 <= top.signal.score <= 1.0

    # Context fields are default-on (the dropped `bare` knob).
    assert top.document_title == "Substrate Handling SOP"
    assert top.section == "Sterilization"
    assert top.contextual_prefix is not None

    # No confidence-of-truth field exists on evidence at all (S1 discipline).
    assert "confidence" not in EvidenceEnvelope.model_fields
    assert top.grounded_facts == []                     # default is bare-fast


def test_k_bounds_the_result_set(service, pipeline, store, db, embedder,
                                 tenant):
    for i in range(4):
        seed_passage(pipeline, store, db, embedder, tenant,
                     title=f"Doc {i}", name=f"Org {i}",
                     text=f"Batch {i}: mycelium colonization notes and "
                          f"substrate moisture readings for the grow room.")
    evs = service.retrieve(
        RetrievalQuery(text="substrate moisture in the grow room", k=2),
        principal_for(tenant))
    assert len(evs) == 2
    assert [e.signal.rank for e in evs] == [1, 2]

    with pytest.raises(ValueError, match="k must be between"):
        service.retrieve(RetrievalQuery(text="x", k=999),
                         principal_for(tenant))


# ---------------------------------------------------------------------------
# Deliverable 2 — every candidate is gated BEFORE it becomes evidence
# ---------------------------------------------------------------------------


def test_retrieval_respects_tenant_and_label_filters(
        service, choke, embedder, pipeline, store, db, tenant):
    """A cross-tenant chunk and an above-grant chunk carrying the SAME text
    never surface: they are filtered out of the candidate set before becoming
    evidence (permission-invisibility, inherited from S2)."""
    tenant_b = f"{tenant}-b"
    role = f"role-{uuid.uuid4().hex[:12]}"
    restricted = make_label(db, role=role)
    text = ("Harvest the fruiting bodies once the caps begin to flatten, "
            "then dry them at 45C until cracker-dry for storage.")

    mine = seed_passage(pipeline, store, db, embedder, tenant,
                        title="Harvest SOP (mine)", text=text)
    theirs = seed_passage(pipeline, store, db, embedder, tenant_b,
                          title="Harvest SOP (theirs)", text=text)
    secret = seed_passage(pipeline, store, db, embedder, tenant,
                          title="Harvest SOP (secret)", text=text,
                          label_id=restricted, name="Secret Farm")

    # Sibling tenant B needs its own registered surface to retrieve at all.
    cat_b = InProcessOperationCatalog(choke, embedder)
    register_serving_defaults(cat_b, tenant_b)
    service_b = DenseRetrievalService(choke, embedder, cat_b)

    query = RetrievalQuery(text="how to harvest and dry the mushrooms")
    outsider = principal_for(tenant)
    seen = {e.spine.chunk_id for e in service.retrieve(query, outsider)}

    assert mine.child.id in seen
    assert theirs.child.id not in seen                  # tenant isolation
    assert secret.child.id not in seen                  # label absence

    # The label-granted insider DOES see the restricted chunk.
    insider = {e.spine.chunk_id for e in
               service.retrieve(query, principal_for(tenant, [role]))}
    assert secret.child.id in insider

    # And tenant B sees only its own copy — never A's.
    theirs_seen = {e.spine.chunk_id for e in
                   service_b.retrieve(query, principal_for(tenant_b))}
    assert theirs_seen == {theirs.child.id}


def test_every_candidate_transits_the_gate(service, choke, pipeline, store,
                                            db, embedder, tenant, monkeypatch):
    """The ANN query is just another gated read: retrieval reaches Postgres
    exactly once, through PostgresChokePoint.read, with the {sec:} marker —
    never around it. The service holds no connection to leak."""
    seed_passage(pipeline, store, db, embedder, tenant, title="Doc",
                 text="Airflow and CO2 exchange in the fruiting chamber.")
    assert not any(isinstance(v, psycopg.Connection)
                   for v in vars(service).values())

    calls: list[str] = []
    original = PostgresChokePoint.read

    def spying_read(self, query, sql, params=None):
        calls.append(sql)
        return original(self, query, sql, params)

    monkeypatch.setattr(PostgresChokePoint, "read", spying_read)
    service.retrieve(RetrievalQuery(text="fruiting chamber airflow"),
                     principal_for(tenant))
    assert len(calls) == 1
    assert "{sec:d}" in calls[0] and "{tenant:c}" in calls[0]


# ---------------------------------------------------------------------------
# Deliverable 3 — enrich opt-in routes through S3's facts_citing
# ---------------------------------------------------------------------------


def test_enrich_attaches_grounded_facts_via_facts_citing(
        service, pipeline, store, db, embedder, tenant):
    """enrich=True attaches the facts grounded to each chunk, and it routes
    through the registered facts_citing op — so the attached FactEnvelopes
    are full S1 facts with states and grounding. enrich=False attaches
    none (bare-fast default)."""
    corpus = seed_passage(
        pipeline, store, db, embedder, tenant, title="Batch Record",
        text="Batch GB-42 was inoculated on the sterilized substrate and "
             "moved to the incubation room the same day.")
    query = RetrievalQuery(text="what happened to the inoculated batch")

    bare = service.retrieve(query, principal_for(tenant))
    assert all(e.grounded_facts == [] for e in bare)

    enriched = service.retrieve(query, principal_for(tenant), enrich=True)
    top = next(e for e in enriched if e.spine.chunk_id == corpus.child.id)
    assert all(isinstance(f, FactEnvelope) for f in top.grounded_facts)
    assert {f.fact_id for f in top.grounded_facts} == corpus.fact_ids
    # The grounded fact carries its verdict via the pending_facts join.
    rel = next(f for f in top.grounded_facts if f.fact_id == corpus.rel_id)
    assert rel.grounding == "pass"
    assert rel.subject.entity_id == corpus.subj.id


def test_enrich_routes_through_registered_facts_citing_op(
        service, catalog, pipeline, store, db, embedder, tenant, monkeypatch):
    """Enrichment is not hand-rolled fact projection: it calls the catalog's
    facts_citing op (which transits the gate) once per surfaced chunk."""
    seed_passage(pipeline, store, db, embedder, tenant, title="Doc",
                 text="Substrate pasteurization log for the grow cycle.")

    seen: list[tuple[str, dict]] = []
    original = InProcessOperationCatalog.execute

    def spying_execute(self, name, params, principal):
        seen.append((name, dict(params)))
        return original(self, name, params, principal)

    monkeypatch.setattr(InProcessOperationCatalog, "execute", spying_execute)
    evs = service.retrieve(RetrievalQuery(text="substrate pasteurization"),
                           principal_for(tenant), enrich=True)
    assert seen and all(name == "facts_citing" for name, _ in seen)
    assert {p["chunk_id"] for _, p in seen} == {e.spine.chunk_id for e in evs}


def test_enrich_never_attaches_a_fact_naming_a_hidden_entity(
        service, pipeline, store, db, embedder, tenant):
    """Enrichment inherits S3's referential filtering: if the object entity
    of a grounded fact is behind a label the caller lacks, that fact is
    absent from grounded_facts — retrieval must not leak a hidden entity via
    enrichment (both triple ends are label-checked by facts_citing)."""
    restricted = make_label(db)                          # granted to nobody
    corpus = seed_passage(
        pipeline, store, db, embedder, tenant, title="Mixed Record",
        text="The site operates under a confidential parent organization.")
    # Hide the object end of the 'operates' relation.
    db.execute("UPDATE entities SET security_label_id = %s WHERE id = %s",
               (restricted, corpus.mid.id))

    enriched = service.retrieve(
        RetrievalQuery(text="confidential parent organization site"),
        principal_for(tenant), enrich=True)
    top = next(e for e in enriched if e.spine.chunk_id == corpus.child.id)
    got = {f.fact_id for f in top.grounded_facts}
    assert corpus.rel_id not in got                      # object hidden -> absent
    assert corpus.attr_id in got                         # literal-object fact stays


# ---------------------------------------------------------------------------
# Deliverable 4 — the rerank seam is called and is a clean no-op
# ---------------------------------------------------------------------------


def test_rerank_seam_is_called_and_noop_by_default(
        service, pipeline, store, db, embedder, tenant, monkeypatch):
    """The default reranker is a pass-through: the path calls the seam, and
    ANN order is served order. Ranks are re-stamped after the seam."""
    for i in range(3):
        seed_passage(pipeline, store, db, embedder, tenant, title=f"D{i}",
                     name=f"Org {i}",
                     text=f"Cold-chain step {i}: keep the harvest at 4C "
                          f"during transport to preserve potency.")

    calls: list[int] = []
    original = PassThroughReranker.rerank

    def spying(self, query, candidates):
        calls.append(len(candidates))
        return original(self, query, candidates)

    monkeypatch.setattr(PassThroughReranker, "rerank", spying)
    evs = service.retrieve(RetrievalQuery(text="cold chain harvest transport"),
                           principal_for(tenant))
    assert len(calls) == 1                               # seam always called
    assert [e.signal.rank for e in evs] == list(range(1, len(evs) + 1))
    # No-op: scores stay monotonically non-increasing (pure ANN order).
    scores = [e.signal.score for e in evs]
    assert scores == sorted(scores, reverse=True)


def test_a_future_reranker_slots_in_without_touching_callers(
        choke, catalog, pipeline, store, db, embedder, tenant):
    """A real reranker drops in by implementing the seam and being handed to
    the constructor — the caller's retrieve() call is unchanged. Here a
    reversing reranker proves the path honors the seam's ordering."""
    for i in range(3):
        seed_passage(pipeline, store, db, embedder, tenant, title=f"D{i}",
                     name=f"Org {i}",
                     text=f"Humidity note {i}: maintain 90% RH in the "
                          f"fruiting chamber for pinning.")

    class ReversingReranker(Reranker):
        def rerank(self, query, candidates):
            return list(reversed(candidates))

    service = DenseRetrievalService(choke, embedder, catalog,
                                    reranker=ReversingReranker())
    dense = DenseRetrievalService(choke, embedder, catalog)

    q = RetrievalQuery(text="fruiting chamber humidity for pinning")
    ann = [e.spine.chunk_id for e in dense.retrieve(q, principal_for(tenant))]
    reranked = [e.spine.chunk_id
                for e in service.retrieve(q, principal_for(tenant))]
    assert reranked == list(reversed(ann))
    # Ranks reflect the seam's ordering, not the ANN's.
    reranked_evs = service.retrieve(q, principal_for(tenant))
    assert [e.signal.rank for e in reranked_evs] == list(
        range(1, len(reranked_evs) + 1))


# ---------------------------------------------------------------------------
# Deliverable 5 — served config is the Axis-C decision; hybrid stays off
# ---------------------------------------------------------------------------


def test_served_config_matches_axis_c_decision(service):
    """bge-m3, prefix-free, dense. The default service serves dense; the
    query prefix is empty (prefix-free); the model is bge-m3."""
    assert service.retrieval_mode == "dense"
    assert QUERY_PREFIX == ""                            # prefix-free
    assert service._embedder.model == "bge-m3"


def test_query_is_embedded_prefix_free(service, embedder, tenant,
                                       pipeline, store, db, monkeypatch):
    """The query text reaches the embedder VERBATIM — no instruction prefix,
    no task template. This is the Axis-C 'prefix-free' decision, asserted at
    the boundary where a prefix would be prepended."""
    seed_passage(pipeline, store, db, embedder, tenant, title="Doc",
                 text="Inoculation technique for grain spawn jars.")

    embedded: list[str] = []
    original = type(embedder).embed

    def spying_embed(self, texts):
        embedded.extend(texts)
        return original(self, texts)

    monkeypatch.setattr(type(embedder), "embed", spying_embed)
    service.retrieve(RetrievalQuery(text="grain spawn inoculation"),
                     principal_for(tenant))
    assert embedded == ["grain spawn inoculation"]       # verbatim, no prefix


def test_hybrid_is_dormant_not_the_default(choke, embedder, catalog):
    """Hybrid dense+sparse fusion is constructible (for a future benchmark
    flip) but never the served default. The default path is dense; asking
    for an unknown mode is a construction error."""
    default = DenseRetrievalService(choke, embedder, catalog)
    assert default.retrieval_mode == "dense"
    assert default._mode != "hybrid"

    # Dormant mode is reachable ONLY by explicit construction.
    dormant = DenseRetrievalService(choke, embedder, catalog,
                                    retrieval_mode="hybrid")
    assert dormant.retrieval_mode == "hybrid"

    with pytest.raises(ValueError, match="retrieval_mode must be one of"):
        DenseRetrievalService(choke, embedder, catalog,
                              retrieval_mode="sparse")


def test_dormant_hybrid_still_gates_every_candidate(
        choke, embedder, catalog, pipeline, store, db, tenant):
    """Even the dormant hybrid path is gated: constructed explicitly, it
    still filters tenant + label before fusing, so it can never surface a
    chunk the dense default couldn't. (Proves the seam's SQL carries the
    markers — not that we serve it.)"""
    tenant_b = f"{tenant}-b"
    text = "Standardized dosing chart for the tincture production line."
    mine = seed_passage(pipeline, store, db, embedder, tenant,
                        title="Dosing SOP", text=text)
    theirs = seed_passage(pipeline, store, db, embedder, tenant_b,
                          title="Dosing SOP", text=text)

    hybrid = DenseRetrievalService(choke, embedder, catalog,
                                   retrieval_mode="hybrid")
    evs = hybrid.retrieve(RetrievalQuery(text="tincture dosing chart"),
                          principal_for(tenant))
    seen = {e.spine.chunk_id for e in evs}
    assert mine.child.id in seen
    assert theirs.child.id not in seen                   # tenant isolation
    assert all(e.signal.mode == "hybrid" for e in evs)
