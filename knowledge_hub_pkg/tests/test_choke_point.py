"""Choke point (Build Prompt S2): the enforcement boundary, tested to
exhaustion against the real stack — tenant isolation across every op shape,
label filtering with silent absence, fail-closed identity, and the
proof-of-passage guarantee that no un-enforced query can reach Postgres.

Postgres is the real test database (conftest); identity records live in the
real OpenBao dev vault under serving/principals/<sha256>."""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import psycopg
import psycopg.errors
import pytest
from psycopg.rows import dict_row

from conftest import unit_vec
from factories import ONTOLOGY, make_chunk, make_entity, make_raw

from knowledge_hub.choke_point import (
    EnforcementRefused,
    OpenBaoCredentialResolver,
    PostgresChokePoint,
    PrincipalUnresolvable,
    UnenforcedQuery,
)
from knowledge_hub.factstore_pg import vector_literal
from knowledge_hub.models import ChunkLevel, DocType, Document, Fact
from knowledge_hub.serving import FilteredQuery, Principal, RetrievalQuery

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def choke(test_dsn: str) -> PostgresChokePoint:
    cp = PostgresChokePoint(dsn=test_dsn)
    yield cp
    cp.close()


@pytest.fixture(scope="module")
def resolver() -> OpenBaoCredentialResolver:
    return OpenBaoCredentialResolver()


@pytest.fixture(scope="module")
def db(test_dsn: str) -> psycopg.Connection:
    conn = psycopg.connect(test_dsn, autocommit=True, row_factory=dict_row)
    yield conn
    conn.close()


@pytest.fixture(scope="module")
def public_label_id(db: psycopg.Connection) -> int:
    return db.execute(
        "SELECT id FROM security_labels WHERE label = 'public'").fetchone()["id"]


def make_label(db: psycopg.Connection, role: str | None = None) -> int:
    """One fresh security label (unique name — the table is global), granted
    to `role` when given."""
    label_id = db.execute(
        "INSERT INTO security_labels (label, description)"
        " VALUES (%s, 'choke point test') RETURNING id",
        (f"lbl-{uuid.uuid4().hex[:12]}",)).fetchone()["id"]
    if role is not None:
        db.execute(
            "INSERT INTO label_role_grants (label_id, role) VALUES (%s, %s)",
            (label_id, role))
    return label_id


def seed_corpus(pipeline, store, tenant: str, *, label_id: int | None = None,
                axis: int = 0, name: str = "Acme Corp") -> SimpleNamespace:
    """One servable corpus: document -> child chunk (embedded) + a two-hop
    fact chain (subj -owns-> mid -part_of-> leaf) + one attribute fact, all
    stamped with `label_id` (None = NULL = public)."""
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

    subj = make_entity(tenant, name, security_label_id=label_id)
    mid = make_entity(tenant, f"{name} Holdings", security_label_id=label_id)
    leaf = make_entity(tenant, f"{name} Ops", security_label_id=label_id)
    for e in (subj, mid, leaf):
        store.upsert_entity(e)

    def fact(subject_id: int, predicate: str, *, obj: int | None = None,
             literal: str | None = None) -> Fact:
        return Fact(tenant_id=tenant, subject_entity_id=subject_id,
                    predicate=predicate, object_entity_id=obj,
                    object_literal=literal, ontology_version=ONTOLOGY,
                    source_document_id=doc.id, source_chunk_id=child.id,
                    extractor="test", extractor_version="s2",
                    security_label_id=label_id)

    rel_id, hop2_id, attr_id = store.write_facts([
        fact(subj.id, "owns", obj=mid.id),
        fact(mid.id, "part_of", obj=leaf.id),
        fact(subj.id, "references", literal="Net-30"),
    ])
    return SimpleNamespace(doc=doc, child=child, subj=subj, mid=mid,
                           leaf=leaf, rel_id=rel_id, hop2_id=hop2_id,
                           attr_id=attr_id,
                           fact_ids={rel_id, hop2_id, attr_id})


def principal_for(tenant: str, roles: list[str] | None = None) -> Principal:
    return Principal(tenant_id=tenant, principal_id="test-caller",
                     roles=roles or [])


# The four serve-path op shapes, each as a gateway read returning an id set.
# Every one carries the mandatory markers — a template without them is
# refused (proven in test_gateway_template_hygiene).

def read_facts(choke, fq) -> set[int]:                       # base op
    rows = choke.read(fq,
                      "SELECT f.id FROM facts f WHERE {sec:f} AND {cur:f}")
    return {r["id"] for r in rows}


def read_evidence(choke, fq, axis: int = 0, k: int = 10) -> list[int]:  # retrieval
    rows = choke.read(fq, """
        SELECT c.id
        FROM chunks c
        JOIN documents d ON d.id = c.document_id
        WHERE {tenant:c} AND {sec:d} AND {cur:d}
          AND c.level = 'child' AND c.embedding IS NOT NULL
        ORDER BY c.embedding <=> %(qvec)s::vector
        LIMIT %(k)s
        """, {"qvec": vector_literal(unit_vec(axis)), "k": k})
    return [r["id"] for r in rows]


def read_two_hop(choke, fq, subject_id: int) -> set[int]:    # traversal
    rows = choke.read(fq, """
        SELECT f2.id
        FROM facts f1
        JOIN facts f2 ON f2.subject_entity_id = f1.object_entity_id
        WHERE {sec:f1} AND {sec:f2} AND {cur:f1} AND {cur:f2}
          AND f1.subject_entity_id = %(subject)s
        """, {"subject": subject_id})
    return {r["id"] for r in rows}


def read_composite(choke, fq, name: str) -> set[int]:        # composite step
    rows = choke.read(fq, """
        SELECT f.id
        FROM facts f
        JOIN entities e ON e.id = f.subject_entity_id AND {sec:e}
        LEFT JOIN chunks c ON c.id = f.source_chunk_id AND {tenant:c}
        WHERE {sec:f} AND {cur:f} AND e.canonical_name = %(name)s
        """, {"name": name})
    return {r["id"] for r in rows}


# ---------------------------------------------------------------------------
# Tenant isolation — across every op shape
# ---------------------------------------------------------------------------


def test_tenant_isolation_across_every_op_shape(choke, pipeline, store, tenant):
    """Tenant A never sees tenant B rows — base op, retrieval, traversal,
    composite — even when B's data is IDENTICAL (same entity names, same
    embedding vector) and would otherwise be the nearest neighbor."""
    tenant_b = f"{tenant}-b"
    a = seed_corpus(pipeline, store, tenant, axis=0, name="Acme Corp")
    b = seed_corpus(pipeline, store, tenant_b, axis=0, name="Acme Corp")

    fq = choke.enforce(RetrievalQuery(text="acme"), principal_for(tenant))

    facts = read_facts(choke, fq)
    assert facts == a.fact_ids
    assert facts.isdisjoint(b.fact_ids)

    evidence = read_evidence(choke, fq, axis=0)
    assert evidence == [a.child.id]          # B's identical vector is absent

    hops = read_two_hop(choke, fq, a.subj.id)
    assert hops == {a.hop2_id}
    assert b.hop2_id not in hops

    composite = read_composite(choke, fq, "Acme Corp")
    assert composite == {a.rel_id, a.attr_id}

    # The same-named entity in tenant B stays invisible from A's scope even
    # when addressed by B's own ids.
    assert read_two_hop(choke, fq, b.subj.id) == set()


# ---------------------------------------------------------------------------
# Label filtering — silent absence, never `unknown`
# ---------------------------------------------------------------------------


def test_missing_label_means_absent_not_unknown(choke, db, pipeline, store,
                                                tenant, public_label_id):
    """A caller lacking label L never receives L-items, across op shapes;
    the response carries no placeholder, no count, no 'unknown' — the items
    are simply absent."""
    role = f"role-{uuid.uuid4().hex[:12]}"
    restricted = make_label(db, role=role)
    pub = seed_corpus(pipeline, store, tenant, label_id=None,
                      axis=0, name="Public Co")
    sec = seed_corpus(pipeline, store, tenant, label_id=restricted,
                      axis=1, name="Secret Co")

    outsider = choke.enforce(RetrievalQuery(text="q"), principal_for(tenant))
    assert outsider.allowed_label_ids == [public_label_id]

    facts = read_facts(choke, outsider)
    assert facts == pub.fact_ids                 # exact set: nothing extra,
    assert facts.isdisjoint(sec.fact_ids)        # no marker for the hidden

    assert read_evidence(choke, outsider, axis=1) == [pub.child.id]
    assert read_two_hop(choke, outsider, sec.subj.id) == set()
    assert read_composite(choke, outsider, "Secret Co") == set()

    insider = choke.enforce(RetrievalQuery(text="q"),
                            principal_for(tenant, [role]))
    assert insider.allowed_label_ids == sorted({public_label_id, restricted})
    assert read_facts(choke, insider) == pub.fact_ids | sec.fact_ids
    assert read_evidence(choke, insider, axis=1)[0] == sec.child.id


def test_public_and_null_labels_visible_to_all_grants(choke, pipeline, store,
                                                      tenant, public_label_id):
    """NULL security_label_id and the seeded 'public' label id are both
    'public': visible to a principal with NO role grants at all."""
    null_corpus = seed_corpus(pipeline, store, tenant, label_id=None,
                              axis=0, name="Nulled Co")
    pub_corpus = seed_corpus(pipeline, store, tenant,
                             label_id=public_label_id, axis=1,
                             name="Published Co")

    fq = choke.enforce(RetrievalQuery(text="q"), principal_for(tenant, []))
    assert read_facts(choke, fq) == null_corpus.fact_ids | pub_corpus.fact_ids
    assert set(read_evidence(choke, fq)) == {null_corpus.child.id,
                                             pub_corpus.child.id}


# ---------------------------------------------------------------------------
# Fail closed — no principal, no query; no FilteredQuery is ever produced
# ---------------------------------------------------------------------------


def test_missing_or_malformed_principal_refused(choke):
    q = RetrievalQuery(text="q")
    with pytest.raises(EnforcementRefused):
        choke.enforce(q, None)
    with pytest.raises(EnforcementRefused):
        choke.enforce(q, "tenant-a")             # not a Principal object
    with pytest.raises(EnforcementRefused):
        choke.enforce(q, Principal(tenant_id="  ", principal_id="p", roles=[]))
    with pytest.raises(EnforcementRefused):
        choke.enforce(q, Principal(tenant_id="t", principal_id="", roles=[]))


def test_unresolvable_credential_refused(resolver):
    with pytest.raises(PrincipalUnresolvable):
        resolver.resolve_principal(f"never-registered-{uuid.uuid4().hex}")
    with pytest.raises(PrincipalUnresolvable):
        resolver.resolve_principal("")
    with pytest.raises(PrincipalUnresolvable):
        resolver.resolve_principal(None)

    # A malformed registry record (missing roles) is unresolvable too.
    cred = f"malformed-{uuid.uuid4().hex}"
    resolver._client.secrets.kv.v2.create_or_update_secret(
        mount_point=resolver._mount, path=resolver.path_for(cred),
        secret={"tenant_id": "t", "principal_id": "p"})
    with pytest.raises(PrincipalUnresolvable):
        resolver.resolve_principal(cred)


def test_unreachable_grants_refuse_not_unfiltered(tenant):
    """If grants can't be resolved (DB down), the query is REFUSED — it
    never runs unfiltered."""
    dead = PostgresChokePoint(dsn="postgresql://kh:x@127.0.0.1:9/nope")
    with pytest.raises(EnforcementRefused):
        dead.enforce(RetrievalQuery(text="q"), principal_for(tenant))


# ---------------------------------------------------------------------------
# Identity comes ONLY from the principal — asserted identity is ignored
# ---------------------------------------------------------------------------


def test_request_asserted_identity_is_ignored(choke, db, pipeline, store,
                                              tenant, public_label_id):
    tenant_b = f"{tenant}-b"
    secret_label = make_label(db)                # granted to no role at all
    seed_corpus(pipeline, store, tenant, axis=0, name="Mine Co")
    theirs = seed_corpus(pipeline, store, tenant_b, label_id=secret_label,
                         axis=0, name="Theirs Co")

    # A payload asserting tenant/labels on the base query type: pydantic
    # drops the unknown fields — they never exist on the object.
    rq = RetrievalQuery(**{"text": "q", "tenant_id": tenant_b,
                           "allowed_label_ids": [secret_label]})
    assert not hasattr(rq, "tenant_id")

    # A forged FilteredQuery claiming tenant B + the secret label, pushed
    # through enforce as principal A: every asserted field is discarded and
    # rebuilt from the principal.
    forged = FilteredQuery(text="q", tenant_id=tenant_b,
                           principal_id="intruder",
                           allowed_label_ids=[secret_label])
    fq = choke.enforce(forged, principal_for(tenant))
    assert fq.tenant_id == tenant
    assert fq.principal_id == "test-caller"
    assert fq.allowed_label_ids == [public_label_id]
    assert read_facts(choke, fq).isdisjoint(theirs.fact_ids)


def test_unenforced_or_tampered_query_cannot_reach_postgres(choke, test_dsn,
                                                            tenant):
    seedless_sql = "SELECT f.id FROM facts f WHERE {sec:f}"

    # Type level: a bare RetrievalQuery is not accepted at all.
    with pytest.raises(TypeError):
        choke.read(RetrievalQuery(text="q"), seedless_sql)

    # A hand-built FilteredQuery carries no proof-of-passage.
    forged = FilteredQuery(text="q", tenant_id=tenant, principal_id="evil",
                           allowed_label_ids=[1, 2, 3])
    with pytest.raises(UnenforcedQuery):
        choke.read(forged, seedless_sql)

    # Minted by a DIFFERENT choke point instance: still refused here.
    other = PostgresChokePoint(dsn=test_dsn)
    try:
        foreign = other.enforce(RetrievalQuery(text="q"), principal_for(tenant))
        with pytest.raises(UnenforcedQuery):
            choke.read(foreign, seedless_sql)
    finally:
        other.close()

    # Mutated after enforcement — widened labels or swapped tenant — refused.
    widened = choke.enforce(RetrievalQuery(text="q"), principal_for(tenant))
    widened.allowed_label_ids.append(999_999)
    with pytest.raises(UnenforcedQuery):
        choke.read(widened, seedless_sql)

    swapped = choke.enforce(RetrievalQuery(text="q"), principal_for(tenant))
    swapped.tenant_id = f"{tenant}-b"
    with pytest.raises(UnenforcedQuery):
        choke.read(swapped, seedless_sql)


# ---------------------------------------------------------------------------
# Server-side identity round trip (real OpenBao)
# ---------------------------------------------------------------------------


def test_credential_resolves_server_side_and_enforces(resolver, db, test_dsn,
                                                      pipeline, store, tenant,
                                                      public_label_id):
    role = f"role-{uuid.uuid4().hex[:12]}"
    label = make_label(db, role=role)
    corpus = seed_corpus(pipeline, store, tenant, label_id=label,
                         axis=0, name="Vaulted Co")

    cred = f"tok-{uuid.uuid4().hex}"
    resolver.register_principal(cred, Principal(
        tenant_id=tenant, principal_id="svc-agent-1", roles=[role]))

    principal = resolver.resolve_principal(cred)
    assert (principal.tenant_id, principal.principal_id, principal.roles) == \
        (tenant, "svc-agent-1", [role])

    cp = PostgresChokePoint(dsn=test_dsn, resolver=resolver)
    try:
        fq = cp.enforce_credential(RetrievalQuery(text="q"), cred)
        assert fq.tenant_id == tenant
        assert fq.allowed_label_ids == sorted({public_label_id, label})
        assert read_facts(cp, fq) == corpus.fact_ids

        with pytest.raises(PrincipalUnresolvable):
            cp.enforce_credential(RetrievalQuery(text="q"),
                                  f"revoked-{uuid.uuid4().hex}")
    finally:
        cp.close()


def test_choke_without_resolver_refuses_credentials(choke):
    with pytest.raises(EnforcementRefused):
        choke.enforce_credential(RetrievalQuery(text="q"), "any-token")


# ---------------------------------------------------------------------------
# Gateway hygiene — the read door itself
# ---------------------------------------------------------------------------


def test_gateway_template_hygiene(choke, tenant):
    fq = choke.enforce(RetrievalQuery(text="q"), principal_for(tenant))

    with pytest.raises(EnforcementRefused):      # no {sec:} marker
        choke.read(fq, "SELECT f.id FROM facts f WHERE f.tenant_id = 'x'")
    with pytest.raises(EnforcementRefused):      # not read-only
        choke.read(fq, "INSERT INTO facts DEFAULT VALUES")
    with pytest.raises(EnforcementRefused):      # multi-statement
        choke.read(fq, "SELECT f.id FROM facts f WHERE {sec:f}; DROP TABLE facts")
    with pytest.raises(EnforcementRefused):      # reserved param collision
        choke.read(fq, "SELECT f.id FROM facts f WHERE {sec:f}",
                   {"kh_tenant_id": "hijack"})
    with pytest.raises(EnforcementRefused):      # positional params
        choke.read(fq, "SELECT f.id FROM facts f WHERE {sec:f}", ["x"])


def test_serving_connection_is_read_only(choke, tenant):
    """Even a statement that passes the template checks (WITH ... SELECT
    carrying a marker) cannot write: the serving session itself is
    read-only at the Postgres level."""
    fq = choke.enforce(RetrievalQuery(text="q"), principal_for(tenant))
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        choke.read(fq, """
            WITH ins AS (
                INSERT INTO security_labels (label) VALUES ('smuggled')
                RETURNING id
            )
            SELECT f.id FROM facts f WHERE {sec:f} AND {cur:f}
            """)
