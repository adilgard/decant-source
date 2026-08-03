"""API service — the enforcement boundary made physical (Build Prompt S5),
tested against the REAL stack over REAL HTTP: live Postgres, live OpenBao
credential resolution, live bge-m3 embeddings, a real threading server on an
ephemeral loopback port.

What must hold:

* every registered op/composite (+ retrieval) is reachable as an endpoint
  returning the correct S1 envelope JSON, and NO other query surface exists
  — no ad-hoc query endpoint, no raw-SQL endpoint, no path around the gate;
* identity is resolved at the boundary, fail-closed: unauthenticated, wrong,
  and revoked credentials are refused with a generic 401; a request cannot
  assert its own tenant;
* a wrong-tenant caller never sees another tenant's data end-to-end through
  HTTP, even when both tenants hold IDENTICAL data (the S2 worst case);
* a composite endpoint returns per-step-tagged mixed envelopes + the
  execution trace;
* the connection count stays bounded under many-tenant load (the S4
  DSN-cache posture holds at the service layer — no per-tenant connections);
* latency percentiles are recorded per request against the §4 budget;
* endpoints track registry changes: add a spec -> the endpoint appears on
  rebuild, and NOTHING else changes;
* check_stack's version-integrity logic fails on installed != pyproject.
"""
from __future__ import annotations

import importlib.util
import threading
import uuid

import httpx
import psycopg
import pytest
from psycopg.rows import dict_row

from conftest import INFRA_DIR
from test_retrieval import make_label, seed_passage

from knowledge_hub.choke_point import OpenBaoCredentialResolver, PostgresChokePoint
from knowledge_hub.operations import (
    InProcessOperationCatalog,
    OperationSpec,
    fact_template,
    register_serving_defaults,
)
from knowledge_hub.retrieval import DenseRetrievalService
from knowledge_hub.serving import (
    EvidenceEnvelope,
    FactEnvelope,
    InMemoryUsageRecorder,
    Principal,
)
from knowledge_hub.service_http import (
    LATENCY_BUDGET_P95_MS,
    KnowledgeHubServingService,
    ServingApp,
    installed_version,
    make_server,
)

TEST_DB = "kh_factstore_test"

# ---------------------------------------------------------------------------
# Fixtures: ONE shared service instance for the whole module — the deployment
# shape (Decision 7) — with per-test tenants registered onto its catalog the
# way build-time authoring would.
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
def catalog(choke, embedder) -> InProcessOperationCatalog:
    return InProcessOperationCatalog(choke, embedder)


@pytest.fixture(scope="module")
def resolver() -> OpenBaoCredentialResolver:
    return OpenBaoCredentialResolver()


@pytest.fixture(scope="module")
def recorder() -> InMemoryUsageRecorder:
    return InMemoryUsageRecorder()


@pytest.fixture(scope="module")
def service(choke, embedder, catalog, recorder) -> KnowledgeHubServingService:
    retrieval = DenseRetrievalService(choke, embedder, catalog)
    svc = KnowledgeHubServingService(choke, catalog, retrieval, recorder)
    assert svc.warm()
    return svc


@pytest.fixture(scope="module")
def app(service, resolver) -> ServingApp:
    return ServingApp(service, resolver)


@pytest.fixture(scope="module")
def client(app) -> httpx.Client:
    """A real server on an ephemeral loopback port — requests below transit
    actual sockets, headers, and the threading handler, not a shortcut."""
    server = make_server(app, "127.0.0.1", 0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with httpx.Client(base_url=f"http://127.0.0.1:{port}",
                      timeout=60.0) as c:
        yield c
    server.shutdown()


@pytest.fixture()
def surface(catalog, tenant) -> str:
    """This test's tenant with the default op surface registered (the
    build-time bootstrap)."""
    register_serving_defaults(catalog, tenant)
    return tenant


def grant(resolver: OpenBaoCredentialResolver, tenant: str,
          roles: list[str] | None = None) -> str:
    """Provision one opaque serving credential in the REAL vault registry
    and return the token the caller would hold."""
    token = f"tok-{uuid.uuid4().hex}"
    resolver.register_principal(token, Principal(
        tenant_id=tenant, principal_id=f"caller-{uuid.uuid4().hex[:8]}",
        roles=roles or []))
    return token


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Deliverable 1 — every registered op is an endpoint; nothing else exists
# ---------------------------------------------------------------------------


def test_every_registered_op_and_retrieval_reachable_with_correct_envelopes(
        client, resolver, surface, pipeline, store, db, embedder):
    corpus = seed_passage(
        pipeline, store, db, embedder, surface,
        title="Fermentation SOP", section="Temperature",
        text="Hold the fermentation tank at 22C and vent CO2 twice daily "
             "until the gravity reading stabilizes.")
    token = grant(resolver, surface)

    listed = client.get("/v1/ops", headers=bearer(token))
    assert listed.status_code == 200
    names = {op["name"] for op in listed.json()["operations"]}
    assert names == {"get_facts", "get_by_key", "get_entity", "neighbors",
                     "facts_citing", "retrieve", "entity_dossier"}

    # Every fact op serves validated S1 FactEnvelopes as-is.
    for name, params in [
            ("get_facts", {"entity_id": corpus.subj.id}),
            ("get_by_key", {"identifier": corpus.asset_id}),
            ("get_entity", {"entity_id": corpus.subj.id}),
            ("neighbors", {"entity_id": corpus.subj.id, "depth": 2}),
            ("facts_citing", {"chunk_id": corpus.child.id})]:
        r = client.post(f"/v1/ops/{name}", json=params, headers=bearer(token))
        assert r.status_code == 200, (name, r.text)
        body = r.json()
        assert body["tenant_id"] == surface
        assert body["operation"] == name
        assert body["returns"] == "facts"
        assert body["evidence"] == []
        assert body["facts"], name
        for payload in body["facts"]:
            env = FactEnvelope.model_validate(payload)  # extra=forbid: exact
            assert env.spine.tenant_id == surface
        assert body["request_id"] and body["wall_ms"] >= 0

    # The registered evidence op and the S4 retrieval endpoint both serve
    # validated EvidenceEnvelopes; enrich is a caller param on the latter.
    for path, params in [
            ("/v1/ops/retrieve", {"query": "fermentation tank temperature",
                                  "k": 5}),
            ("/v1/retrieve", {"query": "fermentation tank temperature",
                              "k": 5, "enrich": True})]:
        r = client.post(path, json=params, headers=bearer(token))
        assert r.status_code == 200, (path, r.text)
        body = r.json()
        assert body["returns"] == "evidence" and body["facts"] == []
        envs = [EvidenceEnvelope.model_validate(p) for p in body["evidence"]]
        assert corpus.child.id in {e.spine.chunk_id for e in envs}
    # enrich=True attached gated grounded facts over HTTP.
    top = next(p for p in body["evidence"]
               if p["spine"]["chunk_id"] == corpus.child.id)
    assert {f["fact_id"] for f in top["grounded_facts"]} == corpus.fact_ids


def test_no_unregistered_or_raw_query_surface_exists(client, resolver,
                                                     surface):
    token = grant(resolver, surface)
    # An unregistered op name fails closed — indistinguishable from absent.
    r = client.post("/v1/ops/select_star", json={}, headers=bearer(token))
    assert r.status_code == 404 and r.json()["error"] == "unknown_operation"

    # There is no ad-hoc query surface of any shape.
    for method, path in [("POST", "/v1/sql"), ("POST", "/v1/query"),
                         ("POST", "/v1/ask"), ("POST", "/v1/ops"),
                         ("GET", "/v1/ops/get_facts"), ("POST", "/"),
                         ("POST", "/v1/ops/get_facts/raw")]:
        r = client.request(method, path, headers=bearer(token))
        assert r.status_code in (404, 501), (method, path, r.status_code)

    # And the route table IS the registry, spelled as URLs.
    app_routes = client.get("/v1/ops", headers=bearer(token))
    assert app_routes.status_code == 200


def test_endpoints_are_generated_from_the_registry(app, catalog, resolver,
                                                   client, surface):
    """Add a spec -> the endpoint appears on rebuild; nothing else changes."""
    before = set(app.endpoints(surface))
    catalog.register(surface, OperationSpec(
        name="current_facts",
        description="every current fact of the tenant (test-authored spec)",
        returns="facts",
        sql=fact_template(where="f.valid_to IS NULL"),
    ))
    rebuilt = ServingApp(app.service, resolver)
    after = set(rebuilt.endpoints(surface))
    assert after - before == {"POST /v1/ops/current_facts"}
    assert before - after == set()

    # The registry is the single source of truth, so the running server
    # serves the newly registered op too — and still nothing else.
    token = grant(resolver, surface)
    r = client.post("/v1/ops/current_facts", json={}, headers=bearer(token))
    assert r.status_code == 200
    assert r.json()["returns"] == "facts"


# ---------------------------------------------------------------------------
# Deliverable 2 — fail-closed boundary auth; identity is never asserted
# ---------------------------------------------------------------------------


def test_unauthenticated_wrong_and_revoked_callers_are_refused(
        client, resolver, surface, tenant):
    token = grant(resolver, surface)

    # No credential at all.
    for path in ("/v1/ops", "/v1/ops/get_facts", "/v1/retrieve"):
        r = (client.get(path) if path == "/v1/ops"
             else client.post(path, json={}))
        assert r.status_code == 401
        assert r.json() == {"error": "unauthorized"}  # generic, no detail

    # A token nobody ever registered.
    r = client.get("/v1/ops", headers=bearer(f"tok-{uuid.uuid4().hex}"))
    assert r.status_code == 401

    # Malformed auth schemes.
    for header in ({"Authorization": token},          # no Bearer prefix
                   {"Authorization": "Basic dXNlcg=="}):
        assert client.get("/v1/ops", headers=header).status_code == 401

    # A REVOKED credential: delete the vault registry record and the same
    # token that worked a moment ago is refused (fail-closed, server-side).
    assert client.get("/v1/ops", headers=bearer(token)).status_code == 200
    import hvac

    from knowledge_hub.config import settings
    hvac.Client(url=settings.bao_addr, token=settings.bao_root_token) \
        .secrets.kv.v2.delete_metadata_and_all_versions(
            mount_point=settings.bao_kv_mount,
            path=OpenBaoCredentialResolver.path_for(token))
    assert client.get("/v1/ops", headers=bearer(token)).status_code == 401


def test_the_request_can_never_assert_its_own_identity(client, resolver,
                                                       surface):
    """Identity rides ONLY in the resolved credential: a body field claiming
    a tenant is an unknown param, refused before anything runs."""
    token = grant(resolver, surface)
    r = client.post("/v1/ops/get_facts",
                    json={"entity_id": 1, "tenant_id": "someone-else"},
                    headers=bearer(token))
    assert r.status_code == 400
    assert "unknown param" in r.json()["detail"]

    r = client.post("/v1/retrieve",
                    json={"query": "x", "tenant_id": "someone-else"},
                    headers=bearer(token))
    assert r.status_code == 400


def test_wrong_tenant_never_sees_anothers_data_end_to_end(
        client, catalog, resolver, surface, pipeline, store, db, embedder):
    """The S2 worst case, proven through the whole HTTP stack: two tenants
    hold IDENTICAL text, names, and identifier values; each caller sees
    exactly its own copy on every surface — ops, key lookup, retrieval,
    composite."""
    tenant_b = f"{surface}-b"
    register_serving_defaults(catalog, tenant_b)
    shared_key = f"EQ-{uuid.uuid4().hex[:8]}"
    text = ("Centrifuge the extract at 4000 rpm for ten minutes before "
            "transferring the supernatant to the clean room.")

    mine = seed_passage(pipeline, store, db, embedder, surface,
                        title="Extraction SOP", text=text,
                        name="Shared Extractor", asset_id=shared_key)
    theirs = seed_passage(pipeline, store, db, embedder, tenant_b,
                          title="Extraction SOP", text=text,
                          name="Shared Extractor", asset_id=shared_key)

    tok_a, tok_b = grant(resolver, surface), grant(resolver, tenant_b)

    for token, own, other in [(tok_a, mine, theirs), (tok_b, theirs, mine)]:
        # Exact-identifier path: the SAME key resolves per-tenant.
        r = client.post("/v1/ops/get_by_key",
                        json={"identifier": shared_key},
                        headers=bearer(token)).json()
        subjects = {f["subject"]["entity_id"] for f in r["facts"]}
        assert own.subj.id in subjects and other.subj.id not in subjects
        assert {f["spine"]["tenant_id"] for f in r["facts"]} == {r["tenant_id"]}

        # Retrieval: identical text, only the caller's chunk surfaces.
        r = client.post("/v1/retrieve",
                        json={"query": "centrifuge the extract"},
                        headers=bearer(token)).json()
        chunks = {e["spine"]["chunk_id"] for e in r["evidence"]}
        assert own.child.id in chunks and other.child.id not in chunks

        # Direct probe at the other tenant's entity id: silently empty.
        r = client.post("/v1/ops/get_facts",
                        json={"entity_id": other.subj.id},
                        headers=bearer(token)).json()
        assert r["facts"] == []

        # Composite: every step stays inside the caller's slice.
        r = client.post("/v1/ops/entity_dossier",
                        json={"identifier": shared_key},
                        headers=bearer(token)).json()
        for step in r["steps"]:
            for f in step["facts"]:
                assert f["spine"]["tenant_id"] != (
                    tenant_b if token == tok_a else surface)
            for e in step["evidence"]:
                assert e["spine"]["chunk_id"] != other.child.id


def test_above_grant_labels_stay_invisible_over_http(
        client, resolver, surface, pipeline, store, db, embedder):
    """Label enforcement transits the HTTP boundary intact: the same tenant,
    one restricted passage — absent for the ungranted caller, served to the
    role-granted one (absence, never 'forbidden')."""
    role = f"role-{uuid.uuid4().hex[:12]}"
    restricted = make_label(db, role=role)
    secret = seed_passage(pipeline, store, db, embedder, surface,
                          title="Restricted Recipe", label_id=restricted,
                          name="Secret Blend",
                          text="The proprietary terpene blend ratio is "
                               "documented for licensed staff only.")

    outsider, insider = grant(resolver, surface), grant(resolver, surface,
                                                        roles=[role])
    q = {"query": "proprietary terpene blend ratio"}
    seen = {e["spine"]["chunk_id"] for e in client.post(
        "/v1/retrieve", json=q, headers=bearer(outsider)).json()["evidence"]}
    assert secret.child.id not in seen
    seen = {e["spine"]["chunk_id"] for e in client.post(
        "/v1/retrieve", json=q, headers=bearer(insider)).json()["evidence"]}
    assert secret.child.id in seen


# ---------------------------------------------------------------------------
# Deliverable 3 — composite endpoint: per-step tags + execution trace
# ---------------------------------------------------------------------------


def test_composite_endpoint_returns_per_step_tagged_envelopes_and_trace(
        client, resolver, surface, pipeline, store, db, embedder):
    corpus = seed_passage(
        pipeline, store, db, embedder, surface,
        title="Vendor Dossier Source", name="Granite Botanicals",
        text="Granite Botanicals operates the northern cultivation site "
             "and references Net-30 terms in its supply contracts.")
    token = grant(resolver, surface)

    r = client.post("/v1/ops/entity_dossier",
                    json={"identifier": corpus.asset_id},
                    headers=bearer(token))
    assert r.status_code == 200
    body = r.json()
    assert body["returns"] == "composite"
    # The flattened top-level lists stay empty — envelopes ride per-step,
    # tagged, never mixed.
    assert body["facts"] == [] and body["evidence"] == []

    steps = {s["step"]: s for s in body["steps"]}
    assert list(steps) == ["resolve", "facts", "evidence"]
    for label in ("resolve", "facts"):
        assert steps[label]["returns"] == "facts"
        assert steps[label]["facts"] and steps[label]["evidence"] == []
        for payload in steps[label]["facts"]:
            FactEnvelope.model_validate(payload)
    assert steps["evidence"]["returns"] == "evidence"
    assert steps["evidence"]["facts"] == []
    for payload in steps["evidence"]["evidence"]:
        env = EvidenceEnvelope.model_validate(payload)
        assert env.grounded_facts == []      # the minimal op never enriches

    # The execution trace rode along: op, status, caller-visible params
    # (raw text, never vectors), envelope counts, wall time.
    trace = {t["step"]: t for t in body["trace"]}
    assert set(trace) == {"resolve", "facts", "evidence"}
    assert all(t["status"] == "ok" for t in trace.values())
    assert trace["evidence"]["params"]["query"] == "Granite Botanicals"
    assert trace["facts"]["envelopes"] == len(steps["facts"]["facts"])
    assert all(t["wall_ms"] >= 0 for t in trace.values())


# ---------------------------------------------------------------------------
# Deliverable 4 — usage instrumentation: serialization is the read
# ---------------------------------------------------------------------------


def test_served_field_reads_and_state_branches_are_logged(
        client, resolver, recorder, surface, pipeline, store, db, embedder):
    corpus = seed_passage(
        pipeline, store, db, embedder, surface, title="Usage Doc",
        text="Log retention policy: keep raw capture logs for ten years.")
    token = grant(resolver, surface)
    client.post("/v1/ops/get_facts", json={"entity_id": corpus.subj.id},
                headers=bearer(token))
    client.post("/v1/retrieve", json={"query": "log retention policy"},
                headers=bearer(token))

    mine = [u for u in recorder.records if u.tenant_id == surface]
    facts_usage = [u for u in mine if u.envelope_kind == "fact"]
    evidence_usage = [u for u in mine if u.envelope_kind == "evidence"]
    assert facts_usage and evidence_usage

    # Serving is maximal today, and the log PROVES it per envelope: every
    # model field was serialized, and the state VALUE served was recorded
    # (the strip/fold evidence Decision 4a/4b needs).
    assert facts_usage[0].fields_read == sorted(FactEnvelope.model_fields)
    assert facts_usage[0].states_branched  # e.g. ['known_confident']
    assert evidence_usage[0].fields_read == sorted(
        EvidenceEnvelope.model_fields)
    # One request_id spans one request's envelopes; keys are addressable.
    assert all(u.envelope_key.startswith(("fact:", "chunk:")) for u in mine)


# ---------------------------------------------------------------------------
# Deliverable 5 — bounded connections under many-tenant load
# ---------------------------------------------------------------------------


def test_connection_count_stays_bounded_under_many_tenant_load(
        client, catalog, resolver, db, choke, service, app):
    """Ten tenants, several requests each, through the ONE shared service:
    the backend count for the test DB never grows — row-level tenancy rides
    the single serving connection (Decision 7 / the S4 fix, held at the
    service layer). And nothing in the stack exposes a raw connection."""
    def backends() -> int:
        return db.execute(
            "SELECT count(*) AS n FROM pg_stat_activity"
            " WHERE datname = %s", (TEST_DB,)).fetchone()["n"]

    tenants = [f"load-{uuid.uuid4().hex[:8]}-{i}" for i in range(10)]
    tokens = {}
    for t in tenants:
        register_serving_defaults(catalog, t)
        tokens[t] = grant(resolver, t)

    before = backends()
    for t in tenants:
        for _ in range(3):
            r = client.post("/v1/ops/get_by_key",
                            json={"identifier": "none-such"},
                            headers=bearer(tokens[t]))
            assert r.status_code == 200
    after = backends()
    assert after <= before, (before, after)   # zero growth across 30 calls

    # No raw connection accessor anywhere in the served stack.
    for component in (service, app, catalog):
        assert not any(isinstance(v, psycopg.Connection)
                       for v in vars(component).values()), component


# ---------------------------------------------------------------------------
# Deliverable 6 — latency percentiles per request, §4 budget observable
# ---------------------------------------------------------------------------


def test_latency_percentiles_recorded_per_request(client, resolver, catalog,
                                                  surface):
    token = grant(resolver, surface)
    for _ in range(5):
        assert client.post("/v1/ops/get_by_key",
                           json={"identifier": "latency-probe"},
                           headers=bearer(token)).status_code == 200

    metrics = client.get("/v1/metrics").json()
    assert metrics["budget_p95_ms"] == LATENCY_BUDGET_P95_MS == 300
    stats = metrics["endpoints"]["op:get_by_key"]
    assert stats["count"] >= 5
    assert 0 < stats["p50_ms"] <= stats["p95_ms"] <= stats["p99_ms"]
    assert isinstance(stats["within_budget"], bool)  # the §4 gate, observable

    # Errors are counted where they happened, not folded into latency lies.
    client.post("/v1/ops/get_by_key", json={"bogus": 1},
                headers=bearer(token))
    metrics = client.get("/v1/metrics").json()
    assert metrics["endpoints"]["op:get_by_key"]["errors"] >= 1


def test_health_reports_components_and_installed_version(client):
    r = client.get("/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["postgres"] is True and body["vault"] is True
    assert body["version"] == installed_version() != "unknown"


# ---------------------------------------------------------------------------
# Deliverable 7 — check_stack version integrity
# ---------------------------------------------------------------------------


def test_check_stack_fails_when_installed_version_drifts():
    spec = importlib.util.spec_from_file_location(
        "check_stack", INFRA_DIR / "check_stack.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    installed, declared, source = mod._version_triple()
    # A correct editable install is green — this ALSO asserts the real
    # environment right now (the drift that bit twice can't hide here).
    mod._assert_versions(installed, declared, source)
    assert installed == declared == source

    with pytest.raises(RuntimeError, match="version drift"):
        mod._assert_versions("0.0.1", declared, source)
    with pytest.raises(RuntimeError, match="version drift"):
        mod._assert_versions(installed, declared, "9.9.9")
