"""Operation registry + base ops + composites (Build Prompt S3), tested
against the real stack.

What must hold: an op is a declarative spec the generator makes executable;
an unmarked template is UNWRITABLE at registration; every generated op can
reach Postgres only through the S2 gate (verified, not assumed); tenant and
label filtering are inherited from S2 on every op shape; composites are
fixed enumerable plans — unregistered refs, content-dependent shape, and
cycles are all rejected — and entity_dossier serves facts as facts,
evidence as evidence, per-step, with an execution trace."""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import psycopg
import pytest
from psycopg.rows import dict_row
from pydantic import ValidationError

from conftest import unit_vec
from factories import ONTOLOGY, make_chunk, make_entity, make_raw

from knowledge_hub.choke_point import PostgresChokePoint
from knowledge_hub.models import ChunkLevel, DocType, Document, Fact
from knowledge_hub.operations import (
    CompositeResult,
    CompositeSpec,
    CompositeStep,
    InProcessOperationCatalog,
    OperationCallError,
    OperationRejected,
    OperationSpec,
    ParamBinding,
    ParamSpec,
    fact_template,
    register_serving_defaults,
)
from knowledge_hub.serving import (
    EvidenceEnvelope,
    FactEnvelope,
    Operation,
    Principal,
    UncertaintyState,
    UnknownOperation,
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


@pytest.fixture(scope="module")
def public_label_id(db: psycopg.Connection) -> int:
    return db.execute(
        "SELECT id FROM security_labels WHERE label = 'public'").fetchone()["id"]


@pytest.fixture()
def catalog(choke, embedder, tenant) -> InProcessOperationCatalog:
    """A per-test catalog with the standard surface registered for the
    test's tenant (build-time bootstrap, exercised for real)."""
    cat = InProcessOperationCatalog(choke, embedder)
    register_serving_defaults(cat, tenant)
    return cat


def principal_for(tenant: str, roles: list[str] | None = None) -> Principal:
    return Principal(tenant_id=tenant, principal_id="test-caller",
                     roles=roles or [])


def make_label(db: psycopg.Connection, role: str | None = None) -> int:
    label_id = db.execute(
        "INSERT INTO security_labels (label, description)"
        " VALUES (%s, 's3 ops test') RETURNING id",
        (f"lbl-{uuid.uuid4().hex[:12]}",)).fetchone()["id"]
    if role is not None:
        db.execute(
            "INSERT INTO label_role_grants (label_id, role) VALUES (%s, %s)",
            (label_id, role))
    return label_id


def seed_corpus(pipeline, store, db, tenant: str, *, label_id: int | None = None,
                axis: int = 0, name: str = "Granite Botanicals",
                asset_id: str | None = None,
                grounding: str | None = "pass") -> SimpleNamespace:
    """One servable corpus: document -> embedded child chunk, a three-node
    fact chain (subj -operates-> mid -located_in-> leaf) + one attribute
    fact, a strong key on the subject, and a promoted pending_facts row
    carrying the grounding verdict for the chain's first edge."""
    raw = make_raw(tenant)
    pipeline.ingest_raw(raw)
    doc = Document(tenant_id=tenant, raw_document_id=raw.id,
                   doc_type=DocType.prose, title=f"{name} dossier",
                   security_label_id=label_id)
    store.insert_document(doc)
    parent = make_chunk(tenant, doc.id)
    store.insert_chunks([parent])
    child = make_chunk(tenant, doc.id, level=ChunkLevel.child,
                       parent_chunk_id=parent.id, embedding=unit_vec(axis),
                       embedding_model="bge-m3")
    store.insert_chunks([child])

    key = asset_id or f"EQ-{uuid.uuid4().hex[:8]}"
    subj = make_entity(tenant, name, security_label_id=label_id,
                       attributes={"asset_id": key})
    mid = make_entity(tenant, f"{name} Site", security_label_id=label_id)
    leaf = make_entity(tenant, f"{name} Region", security_label_id=label_id)
    for e in (subj, mid, leaf):
        store.upsert_entity(e)

    def fact(subject_id: int, predicate: str, *, obj: int | None = None,
             literal: str | None = None) -> Fact:
        return Fact(tenant_id=tenant, subject_entity_id=subject_id,
                    predicate=predicate, object_entity_id=obj,
                    object_literal=literal, ontology_version=ONTOLOGY,
                    source_document_id=doc.id, source_chunk_id=child.id,
                    extractor="test", extractor_version="s3",
                    confidence=0.9, security_label_id=label_id)

    rel_id, hop2_id, attr_id = store.write_facts([
        fact(subj.id, "operates", obj=mid.id),
        fact(mid.id, "located_in", obj=leaf.id),
        fact(subj.id, "references", literal="Net-30"),
    ])

    # The grounding verdict lives on pending_facts and survives promotion
    # via promoted_fact_id — there is no grounding column on facts.
    if grounding is not None:
        db.execute(
            """
            INSERT INTO pending_facts
              (tenant_id, subject_ref, predicate, object_ref,
               ontology_version, source_document_id, source_chunk_id,
               extractor, extractor_version, security_label_id,
               resolution_status, promoted_fact_id, grounding)
            VALUES (%s, %s, 'operates', %s, %s, %s, %s, 'test', 's3', %s,
                    'promoted', %s, %s)
            """,
            (tenant, f"entity:{subj.id}", f"entity:{mid.id}", ONTOLOGY,
             doc.id, child.id, label_id, rel_id, grounding))

    return SimpleNamespace(doc=doc, child=child, subj=subj, mid=mid,
                           leaf=leaf, rel_id=rel_id, hop2_id=hop2_id,
                           attr_id=attr_id, asset_id=key,
                           fact_ids={rel_id, hop2_id, attr_id})


def fact_ids(envelopes: list[FactEnvelope]) -> set[int]:
    return {e.fact_id for e in envelopes}


# ---------------------------------------------------------------------------
# Deliverable 1 — the generator rejects everything outside the grammar
# ---------------------------------------------------------------------------


def test_unmarked_template_is_unwritable(catalog, tenant):
    """The S3 face of 'unfiltered op is unwritable': no {sec:<alias>}
    marker, no registration — the op never exists to be called."""
    with pytest.raises(OperationRejected, match="sec:"):
        catalog.register(tenant, OperationSpec(
            name="leaky", description="", returns="facts",
            sql="SELECT f.id FROM facts f WHERE f.tenant_id = 'oops'"))
    with pytest.raises(UnknownOperation):
        catalog.get(tenant, "leaky")


def test_generator_rejects_templates_outside_the_grammar(catalog, tenant):
    good_where = "f.subject_entity_id = %(entity_id)s"
    params = {"entity_id": ParamSpec(type="int", required=True)}

    with pytest.raises(OperationRejected, match="multi-statement"):
        catalog.register(tenant, OperationSpec(
            name="two", description="", returns="facts", params=params,
            sql=fact_template(where=good_where) + "; SELECT 1"))
    with pytest.raises(OperationRejected, match="SELECT/WITH"):
        catalog.register(tenant, OperationSpec(
            name="writer", description="", returns="facts", params=params,
            sql="DELETE FROM facts WHERE {sec:f}"))
    with pytest.raises(OperationRejected, match="undeclared param"):
        catalog.register(tenant, OperationSpec(
            name="undeclared", description="", returns="facts",
            sql=fact_template(where="f.id = %(mystery)s")))
    with pytest.raises(OperationRejected, match="outside the.*grammar"):
        catalog.register(tenant, OperationSpec(
            name="stray", description="", returns="facts",
            sql="SELECT f.id FROM facts f WHERE {sec:f} AND x = '{braces}'"))
    with pytest.raises(OperationRejected, match="reserved"):
        catalog.register(tenant, OperationSpec(
            name="reserved", description="", returns="facts",
            params={"kh_tenant_id": ParamSpec(type="str")},
            sql=fact_template(where="TRUE")))

    # A bare S1 Operation (data with no template) is unregistrable.
    with pytest.raises(OperationRejected, match="bare Operation"):
        catalog.register(tenant, Operation(
            name="bare", description="", returns="facts"))


def test_registration_is_the_only_mint_and_reads_are_gated(
        catalog, pipeline, store, db, tenant, monkeypatch):
    """A generated op reaches Postgres exactly once per run, through
    PostgresChokePoint.read — never around it. The compiled op holds no
    connection to leak."""
    corpus = seed_corpus(pipeline, store, db, tenant)
    compiled = catalog.compiled(tenant, "get_facts")
    assert not any(isinstance(v, psycopg.Connection)
                   for v in vars(compiled).values())

    calls: list[str] = []
    original = PostgresChokePoint.read

    def spying_read(self, query, sql, params=None):
        calls.append(sql)
        return original(self, query, sql, params)

    monkeypatch.setattr(PostgresChokePoint, "read", spying_read)
    envs = catalog.execute("get_facts", {"entity_id": corpus.subj.id},
                           principal_for(tenant))
    assert len(calls) == 1 and "{sec:f}" in calls[0]
    assert fact_ids(envs) == {corpus.rel_id, corpus.attr_id}

    # Unregistered ask fails closed — and never touches the gate.
    calls.clear()
    with pytest.raises(UnknownOperation):
        catalog.execute("never_registered", {}, principal_for(tenant))
    assert calls == []


# ---------------------------------------------------------------------------
# Deliverable 2 — base ops return correct envelopes (real rows, real gate)
# ---------------------------------------------------------------------------


def test_get_facts_envelopes_and_grounding_join(catalog, pipeline, store, db,
                                                tenant):
    corpus = seed_corpus(pipeline, store, db, tenant, grounding="pass")
    envs = catalog.execute("get_facts", {"entity_id": corpus.subj.id},
                           principal_for(tenant))
    assert fact_ids(envs) == {corpus.rel_id, corpus.attr_id}
    by_id = {e.fact_id: e for e in envs}

    rel = by_id[corpus.rel_id]
    assert rel.subject.entity_id == corpus.subj.id
    assert rel.subject.canonical_name == corpus.subj.canonical_name
    assert rel.object_entity.entity_id == corpus.mid.id
    assert rel.predicate == "operates"
    assert rel.grounding == "pass"                    # via pending_facts join
    assert rel.state is UncertaintyState.known_confident
    assert rel.spine.document_id == corpus.doc.id
    assert rel.spine.chunk_id == corpus.child.id
    assert rel.spine.security_label == "public"       # NULL label serves as public
    assert rel.spine.tenant_id == tenant
    assert rel.ontology_version == ONTOLOGY

    attr = by_id[corpus.attr_id]
    assert attr.object_entity is None
    assert attr.object_literal == "Net-30"
    assert attr.grounding is None                     # never span-grounded

    # predicate filter + role filter
    only = catalog.execute("get_facts", {"entity_id": corpus.subj.id,
                                         "predicate": "references"},
                           principal_for(tenant))
    assert fact_ids(only) == {corpus.attr_id}
    object_side = catalog.execute("get_facts", {"entity_id": corpus.mid.id,
                                                "role": "object"},
                                  principal_for(tenant))
    assert fact_ids(object_side) == {corpus.rel_id}


def test_flagged_grounding_lowers_state(catalog, pipeline, store, db, tenant):
    corpus = seed_corpus(pipeline, store, db, tenant,
                         grounding="span_missing")
    envs = catalog.execute("get_facts", {"entity_id": corpus.subj.id,
                                         "predicate": "operates"},
                           principal_for(tenant))
    (env,) = envs
    assert env.grounding == "span_missing"
    assert env.state is UncertaintyState.known_low_confidence


def test_get_by_key_resolves_strong_identifier(catalog, pipeline, store, db,
                                               tenant):
    """The exact-identifier path: a verbatim equipment/batch ID resolves
    against the registry's attributes, never through retrieval."""
    corpus = seed_corpus(pipeline, store, db, tenant, name="Dryer Unit 7")
    other = seed_corpus(pipeline, store, db, tenant, name="Dryer Unit 9")

    envs = catalog.execute("get_by_key", {"identifier": corpus.asset_id},
                           principal_for(tenant))
    assert fact_ids(envs) == {corpus.rel_id, corpus.attr_id}
    assert all(e.subject.entity_id == corpus.subj.id for e in envs)
    assert not fact_ids(envs) & other.fact_ids

    # Named-key match and a miss.
    named = catalog.execute("get_by_key", {"identifier": corpus.asset_id,
                                           "key": "asset_id"},
                            principal_for(tenant))
    assert fact_ids(named) == fact_ids(envs)
    wrong_key = catalog.execute("get_by_key", {"identifier": corpus.asset_id,
                                               "key": "email"},
                                principal_for(tenant))
    assert wrong_key == []
    miss = catalog.execute("get_by_key", {"identifier": "EQ-nope"},
                           principal_for(tenant))
    assert miss == []


def test_get_entity_by_id_or_key(catalog, pipeline, store, db, tenant):
    corpus = seed_corpus(pipeline, store, db, tenant)
    by_id = catalog.execute("get_entity", {"entity_id": corpus.subj.id},
                            principal_for(tenant))
    by_key = catalog.execute("get_entity", {"identifier": corpus.asset_id},
                             principal_for(tenant))
    assert fact_ids(by_id) == fact_ids(by_key) == {corpus.rel_id,
                                                   corpus.attr_id}
    with pytest.raises(OperationCallError, match="at least one"):
        catalog.execute("get_entity", {}, principal_for(tenant))


def test_neighbors_recursive_cte_is_depth_bounded(catalog, pipeline, store,
                                                  db, tenant):
    corpus = seed_corpus(pipeline, store, db, tenant)
    p = principal_for(tenant)

    one = catalog.execute("neighbors", {"entity_id": corpus.subj.id}, p)
    assert fact_ids(one) == {corpus.rel_id}           # depth defaults to 1

    two = catalog.execute("neighbors", {"entity_id": corpus.subj.id,
                                        "depth": 2}, p)
    assert fact_ids(two) == {corpus.rel_id, corpus.hop2_id}

    pred = catalog.execute("neighbors", {"entity_id": corpus.subj.id,
                                         "predicate": "located_in",
                                         "depth": 2}, p)
    assert pred == []            # chain broken: first hop isn't located_in

    with pytest.raises(OperationCallError, match="<= 5"):
        catalog.execute("neighbors", {"entity_id": corpus.subj.id,
                                      "depth": 99}, p)


def test_facts_citing_links_chunk_to_facts(catalog, pipeline, store, db,
                                           tenant):
    corpus = seed_corpus(pipeline, store, db, tenant)
    envs = catalog.execute("facts_citing", {"chunk_id": corpus.child.id},
                           principal_for(tenant))
    assert fact_ids(envs) == corpus.fact_ids
    assert all(e.spine.chunk_id == corpus.child.id for e in envs)


def test_retrieve_returns_evidence_envelopes(catalog, pipeline, store, db,
                                             tenant):
    corpus = seed_corpus(pipeline, store, db, tenant)
    envs = catalog.execute("retrieve", {"query": "granite botanicals"},
                           principal_for(tenant))
    assert envs and all(isinstance(e, EvidenceEnvelope) for e in envs)
    top = envs[0]
    assert top.spine.chunk_id == corpus.child.id
    assert top.signal.mode == "dense"
    assert top.signal.query == "granite botanicals"
    assert top.signal.rank == 1
    assert top.grounded_facts == []                   # S3 retrieve is bare
    # Relevance is a statement about the query — the envelope carries no
    # truth-confidence field at all (S1 forbids it structurally).
    assert "confidence" not in EvidenceEnvelope.model_fields


# ---------------------------------------------------------------------------
# Tenant/label filtering holds on every op — inherited from S2, verified here
# ---------------------------------------------------------------------------


def test_tenant_and_label_filtering_inherited_on_every_op(
        catalog, pipeline, store, db, tenant):
    """Identical corpora in a sibling tenant and under a restricted label:
    every registered op serves exactly the caller's permitted slice, with
    hidden items absent — not marked, not counted."""
    tenant_b = f"{tenant}-b"
    role = f"role-{uuid.uuid4().hex[:12]}"
    restricted = make_label(db, role=role)
    shared_key = f"EQ-{uuid.uuid4().hex[:8]}"
    mine = seed_corpus(pipeline, store, db, tenant, axis=0,
                       name="Acme Corp", asset_id=shared_key)
    theirs = seed_corpus(pipeline, store, db, tenant_b, axis=0,
                         name="Acme Corp", asset_id=shared_key)
    secret = seed_corpus(pipeline, store, db, tenant, axis=1,
                         name="Secret Co", label_id=restricted)

    outsider = principal_for(tenant)
    for op, params in [
        ("get_facts", {"entity_id": mine.subj.id}),
        ("get_by_key", {"identifier": shared_key}),
        ("get_entity", {"entity_id": mine.subj.id}),
        ("neighbors", {"entity_id": mine.subj.id, "depth": 5}),
        ("facts_citing", {"chunk_id": mine.child.id}),
    ]:
        served = fact_ids(catalog.execute(op, params, outsider))
        assert served <= mine.fact_ids, op
        assert not served & theirs.fact_ids, op       # tenant isolation
        assert not served & secret.fact_ids, op       # label absence

    # Cross-tenant address by THEIR ids: silent emptiness, never an error.
    assert catalog.execute("get_facts", {"entity_id": theirs.subj.id},
                           outsider) == []
    # The label-granted insider sees the restricted slice too.
    insider_served = fact_ids(catalog.execute(
        "get_by_key", {"identifier": secret.asset_id},
        principal_for(tenant, [role])))
    assert insider_served == {secret.rel_id, secret.attr_id}
    assert catalog.execute("get_by_key", {"identifier": secret.asset_id},
                           outsider) == []

    # Evidence: B's identical vector never surfaces in A's results.
    evs = catalog.execute("retrieve", {"query": "acme corp"}, outsider)
    assert theirs.child.id not in {e.spine.chunk_id for e in evs}


def test_traversal_never_extends_through_a_hidden_node(
        catalog, pipeline, store, db, tenant):
    """Restricting the middle entity of subj -> mid -> leaf hides BOTH edges
    from an ungranted caller: nothing served names the hidden node, and the
    walk does not silently bridge across it."""
    restricted = make_label(db)                       # granted to nobody
    corpus = seed_corpus(pipeline, store, db, tenant)
    db.execute("UPDATE entities SET security_label_id = %s WHERE id = %s",
               (restricted, corpus.mid.id))

    served = catalog.execute("neighbors", {"entity_id": corpus.subj.id,
                                           "depth": 5},
                             principal_for(tenant))
    assert served == []


def test_scoped_op_is_invisible_not_forbidden(catalog, tenant):
    role = f"role-{uuid.uuid4().hex[:12]}"
    catalog.register(tenant, OperationSpec(
        name="restricted_op", description="", returns="facts",
        scope=[role],
        params={"entity_id": ParamSpec(type="int", required=True)},
        sql=fact_template(where="f.subject_entity_id = %(entity_id)s")))

    with pytest.raises(UnknownOperation):             # absent, not 403
        catalog.execute("restricted_op", {"entity_id": 1},
                        principal_for(tenant))
    names = {o.name for o in catalog.list_for(principal_for(tenant))}
    assert "restricted_op" not in names
    names_in = {o.name for o in catalog.list_for(principal_for(tenant, [role]))}
    assert "restricted_op" in names_in


# ---------------------------------------------------------------------------
# Deliverable 3 — composites: fixed plans, rejected outside the grammar
# ---------------------------------------------------------------------------


def test_composite_referencing_unregistered_op_rejected(catalog, tenant):
    with pytest.raises(OperationRejected, match="unregistered op"):
        catalog.register(tenant, CompositeSpec(
            name="ghost_plan", description="",
            params={"identifier": ParamSpec(type="str", required=True)},
            steps=[CompositeStep(step="a", op="never_registered", bind={
                "identifier": ParamBinding(source="param",
                                           name="identifier")})]))


def test_content_dependent_plan_shape_is_unexpressible(catalog, tenant):
    """The step grammar has no field to hang control flow on: a 'when'
    condition, a loop, or a free extractor all fail validation — a spec
    whose ops can't be enumerated is a router, and routers are rejected."""
    with pytest.raises(ValidationError):
        CompositeStep(step="a", op="get_facts", when="len(results) > 3",
                      bind={})
    with pytest.raises(ValidationError):
        CompositeStep(step="a", op="get_facts", foreach="results",
                      bind={})
    with pytest.raises(OperationRejected, match="unknown extractor"):
        catalog.register(tenant, CompositeSpec(
            name="freeform", description="",
            params={"identifier": ParamSpec(type="str", required=True)},
            steps=[
                CompositeStep(step="a", op="get_by_key", bind={
                    "identifier": ParamBinding(source="param",
                                               name="identifier")}),
                CompositeStep(step="b", op="get_facts", bind={
                    "entity_id": ParamBinding(
                        source="step", step="a",
                        extract="eval:pick_whatever")}),
            ]))
    # Data flows strictly downward — a forward reference is rejected.
    with pytest.raises(OperationRejected, match="EARLIER"):
        catalog.register(tenant, CompositeSpec(
            name="forward_ref", description="",
            params={"identifier": ParamSpec(type="str", required=True)},
            steps=[
                CompositeStep(step="a", op="get_facts", bind={
                    "entity_id": ParamBinding(source="step", step="b",
                                              extract="first_subject_id")}),
                CompositeStep(step="b", op="get_by_key", bind={
                    "identifier": ParamBinding(source="param",
                                               name="identifier")}),
            ]))


def test_cyclic_composite_rejected(catalog, tenant):
    """Downward-acyclic is checked against the LIVE catalog on every
    registration: replacing a base op with a composite that (transitively)
    depends on its own dependents is refused."""
    catalog.register(tenant, CompositeSpec(
        name="wrapper", description="",
        params={"entity_id": ParamSpec(type="int", required=True)},
        steps=[CompositeStep(step="inner", op="get_facts", bind={
            "entity_id": ParamBinding(source="param", name="entity_id")})]))

    with pytest.raises(OperationRejected, match="cyclic"):
        catalog.register(tenant, CompositeSpec(
            name="get_facts",              # replaces the base op by name...
            description="",
            params={"entity_id": ParamSpec(type="int", required=True)},
            steps=[CompositeStep(step="loop", op="wrapper", bind={
                "entity_id": ParamBinding(source="param",
                                          name="entity_id")})]))
    # The refused registration must not have clobbered the base op.
    assert catalog.get(tenant, "get_facts").returns == "facts"


def test_composite_fallback_chain_is_fixed_and_traced(catalog, pipeline,
                                                      store, db, tenant):
    """try-A-else-B: the declared fallback runs only when the primary
    returns zero envelopes, and the trace records which op actually ran."""
    corpus = seed_corpus(pipeline, store, db, tenant)
    catalog.register(tenant, CompositeSpec(
        name="keyed_or_direct", description="",
        params={"identifier": ParamSpec(type="str", required=True),
                "entity_id": ParamSpec(type="int", required=True)},
        steps=[CompositeStep(
            step="resolve", op="get_by_key",
            bind={"identifier": ParamBinding(source="param",
                                             name="identifier")},
            fallback_op="get_facts",
            fallback_bind={"entity_id": ParamBinding(source="param",
                                                     name="entity_id")})]))

    hit = catalog.execute("keyed_or_direct",
                          {"identifier": corpus.asset_id,
                           "entity_id": corpus.subj.id},
                          principal_for(tenant))
    assert hit.trace[0].op == "get_by_key"
    assert hit.trace[0].status == "ok"

    missed = catalog.execute("keyed_or_direct",
                             {"identifier": "EQ-does-not-exist",
                              "entity_id": corpus.subj.id},
                             principal_for(tenant))
    assert missed.trace[0].op == "get_facts"
    assert missed.trace[0].status == "fallback_used"
    assert fact_ids(missed.steps[0].facts) == {corpus.rel_id, corpus.attr_id}


def test_entity_dossier_end_to_end(catalog, pipeline, store, db, tenant,
                                   monkeypatch):
    """The first composite, against the real stack: strong key in; facts as
    facts, evidence as evidence, tagged by step, never flattened; an
    execution trace; and every step transiting the S2 gate."""
    corpus = seed_corpus(pipeline, store, db, tenant, name="Mill Press 3")

    gate_calls: list[str] = []
    original = PostgresChokePoint.read

    def spying_read(self, query, sql, params=None):
        gate_calls.append(sql)
        return original(self, query, sql, params)

    monkeypatch.setattr(PostgresChokePoint, "read", spying_read)
    result = catalog.execute("entity_dossier",
                             {"identifier": corpus.asset_id},
                             principal_for(tenant))

    assert isinstance(result, CompositeResult)
    by_step = {s.step: s for s in result.steps}
    assert list(by_step) == ["resolve", "facts", "evidence"]

    # Facts as facts — full S1 envelopes with states and grounding.
    resolve = by_step["resolve"]
    assert resolve.returns == "facts" and resolve.evidence == []
    assert all(isinstance(e, FactEnvelope) for e in resolve.facts)
    assert all(e.subject.entity_id == corpus.subj.id for e in resolve.facts)
    facts = by_step["facts"]
    assert fact_ids(facts.facts) == {corpus.rel_id, corpus.attr_id}
    assert {e.state for e in facts.facts} == {UncertaintyState.known_confident}
    assert {e.fact_id: e.grounding for e in facts.facts}[corpus.rel_id] == "pass"

    # Evidence as evidence — retrieval signal, no truth-confidence.
    evidence = by_step["evidence"]
    assert evidence.returns == "evidence" and evidence.facts == []
    assert all(isinstance(e, EvidenceEnvelope) for e in evidence.evidence)
    assert evidence.evidence[0].signal.query == "Mill Press 3"

    # Execution trace: one entry per executed step, each gated (three gate
    # transits — one per step; there is no other door).
    assert [t.step for t in result.trace] == ["resolve", "facts", "evidence"]
    assert all(t.status == "ok" and t.gated for t in result.trace)
    assert len(gate_calls) == 3
    assert all("{sec:" in sql for sql in gate_calls)
    # The trace shows raw caller-visible params, never vectors.
    assert result.trace[2].params["query"] == "Mill Press 3"

    # An unresolvable identifier: dependent steps skip, nothing fabricates.
    empty = catalog.execute("entity_dossier", {"identifier": "EQ-nothing"},
                            principal_for(tenant))
    assert [t.status for t in empty.trace] == [
        "ok", "skipped_empty_input", "skipped_empty_input"]
    assert empty.steps[0].facts == []


def test_dossier_inherits_permission_filtering(catalog, pipeline, store, db,
                                               tenant):
    """A composite inherits its steps' label filtering automatically: the
    whole dossier of a restricted entity is silently empty for an ungranted
    caller — and served whole to a granted one."""
    role = f"role-{uuid.uuid4().hex[:12]}"
    restricted = make_label(db, role=role)
    corpus = seed_corpus(pipeline, store, db, tenant, label_id=restricted,
                         name="Hidden Plant")

    blind = catalog.execute("entity_dossier",
                            {"identifier": corpus.asset_id},
                            principal_for(tenant))
    assert blind.steps[0].facts == []
    assert [t.status for t in blind.trace] == [
        "ok", "skipped_empty_input", "skipped_empty_input"]

    granted = catalog.execute("entity_dossier",
                              {"identifier": corpus.asset_id},
                              principal_for(tenant, [role]))
    assert fact_ids(granted.steps[0].facts) == {corpus.rel_id,
                                                corpus.attr_id}
