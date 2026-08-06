"""Operator UI + operator READ endpoints (Build Prompt 20), tested against
the REAL stack over REAL HTTP — with the READ serving service beside the
operator service, so the UI's wiring contract (reads via the operator reads,
decisions via the BP19 writes, agreement visible through serving) is proven
end to end.

What must hold:

* GET /v1/monitor renders live counters that MATCH the database — landed,
  facts, per-stage counts, per-source progress, review counts, throughput
  series (no sine fakes anywhere in the shipped app.js);
* GET /v1/monitor/activity feeds real pipeline events, newest first, in the
  mock's `stage · detail` copy voice (no POOL);
* GET /v1/reviews lists real items with matching counts; /v1/reviews/<id>
  loads the candidate-pair evidence panel (both candidates, evidence
  for/against from recorded features, the source passage + provenance);
* the UI's decide() contract: resolve_merge via the item id from the
  listing → recorded + the queue count drops + THE MERGE IS VISIBLE THROUGH
  THE READ SERVING LAYER; split_merge undoes it; a skip writes nothing;
* reads are tenant + role enforced and read-only: an agent principal gets
  403, another tenant's items are absent, and no read leaves an audit row
  or mutates anything;
* the UI ships in the package and serves from the operator service itself
  (offline, single origin): /ui/ answers with the console shell, app.js
  carries no fakes, and the shell renders no data unauthenticated.
"""
from __future__ import annotations

import threading
import uuid

import httpx
import psycopg
import pytest
from psycopg.rows import dict_row

from factories import ONTOLOGY, make_raw
from test_retrieval import seed_passage

from knowledge_hub.capture import SourceRegistry
from knowledge_hub.choke_point import OpenBaoCredentialResolver, PostgresChokePoint
from knowledge_hub.models import MatchCandidate
from knowledge_hub.operations import (
    InProcessOperationCatalog,
    register_serving_defaults,
)
from knowledge_hub.operator_http import (
    OperatorApp,
    OperatorGate,
    OperatorService,
    register_operator_defaults,
)
from knowledge_hub.operator_reads import OperatorReadService
from knowledge_hub.resolution import ResolutionService
from knowledge_hub.retrieval import DenseRetrievalService
from knowledge_hub.serving import Principal
from knowledge_hub.service_http import (
    KnowledgeHubServingService,
    ServingApp,
    make_server,
)

# ---------------------------------------------------------------------------
# Fixtures — operator service (with reads + UI) and read serving, live HTTP.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def db(test_dsn: str) -> psycopg.Connection:
    conn = psycopg.connect(test_dsn, autocommit=True, row_factory=dict_row, connect_timeout=10)
    yield conn
    conn.close()


@pytest.fixture(scope="module")
def resolver() -> OpenBaoCredentialResolver:
    return OpenBaoCredentialResolver()


@pytest.fixture(scope="module")
def operator_app(store, pipeline, scorer, embedder, secrets,
                 resolver) -> OperatorApp:
    resolution = ResolutionService(pipeline, scorer, embedder)
    service = OperatorService(store, resolution, SourceRegistry(store),
                              secrets)
    gate = OperatorGate(store)
    register_operator_defaults(gate, service)
    return OperatorApp(gate, service, resolver,
                       reads=OperatorReadService(store))


@pytest.fixture(scope="module")
def op_client(operator_app) -> httpx.Client:
    server = make_server(operator_app, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with httpx.Client(base_url=f"http://127.0.0.1:{server.server_address[1]}",
                      timeout=60.0) as c:
        yield c
    server.shutdown()


@pytest.fixture(scope="module")
def read_stack(test_dsn, embedder, resolver):
    choke = PostgresChokePoint(dsn=test_dsn)
    catalog = InProcessOperationCatalog(choke, embedder)
    service = KnowledgeHubServingService(
        choke, catalog, DenseRetrievalService(choke, embedder, catalog))
    assert service.warm()
    server = make_server(ServingApp(service, resolver), "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    client = httpx.Client(
        base_url=f"http://127.0.0.1:{server.server_address[1]}", timeout=60.0)
    yield catalog, client
    client.close()
    server.shutdown()
    choke.close()


def grant(resolver, tenant: str, roles: list[str]) -> str:
    token = f"tok-{uuid.uuid4().hex}"
    resolver.register_principal(token, Principal(
        tenant_id=tenant, principal_id=f"ui-{uuid.uuid4().hex[:8]}",
        roles=roles))
    return token


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def seed_review_pair(store, pipeline, db, embedder, tenant, *,
                     features=None):
    """Two entities with facts + one entity-entity review pair — what the
    UI's review queue acts on."""
    main = seed_passage(pipeline, store, db, embedder, tenant,
                        title="Vendor master", name="Granite Botanicals",
                        text="Granite Botanicals operates the northern "
                             "cultivation site under Net-30 terms.")
    dup = seed_passage(pipeline, store, db, embedder, tenant,
                       title="Vendor dupe", name="Granite Botanicals Inc",
                       text="Granite Botanicals Inc appears in the ledger "
                            "as the same supplier.")
    candidate_id = store.insert_match_candidate(MatchCandidate(
        tenant_id=tenant, left_type="entity", left_id=dup.subj.id,
        right_type="entity", right_id=main.subj.id, match_score=0.62,
        match_method="embedding", band="gray", decision="review",
        features=features or {"name_sim": 0.94, "cosine": 0.87,
                              "key_overlap": False, "corroboration": 0}))
    return main, dup, candidate_id


# ---------------------------------------------------------------------------
# Part A — the monitor read
# ---------------------------------------------------------------------------


def test_monitor_snapshot_matches_the_database(
        op_client, resolver, store, db, pipeline, dispatcher, embedder,
        tenant):
    corpus = seed_passage(pipeline, store, db, embedder, tenant,
                          title="Live doc", name="Monitor Org",
                          text="A landed document with promoted facts.")
    SourceRegistry(store).register(tenant, "fs-mon", "filesystem")
    raw2 = make_raw(tenant)
    pipeline.ingest_raw(raw2)
    dispatcher.dispatch(tenant, raw2.id)
    tok = grant(resolver, tenant, ["reviewer"])

    r = op_client.get("/v1/monitor", headers=bearer(tok))
    assert r.status_code == 200, r.text
    m = r.json()
    assert m["tenant_id"] == tenant
    # Counters match the DB (seed_passage lands 1 raw doc + raw2 = 2).
    assert m["landed"] == 2
    assert m["facts"] == len(corpus.fact_ids)
    assert m["facts_confident"] + m["facts_low_confidence"] == m["facts"]
    assert m["stages"]["capture"]["count"] == 2
    assert m["stages"]["capture"]["in_flight"] == 1     # raw2 still queued
    assert m["stages"]["process"]["queue_depth"] == 1
    assert m["stages"]["facts"]["count"] == m["facts"]
    assert m["stages"]["resolve"]["held_for_review"] == 0
    # Per-source progress from the registry.
    (src,) = m["sources"]
    assert src["source_ref"] == "fs-mon" and src["status"] == "active"
    assert src["landed"] == 0            # counted by source_system, honest 0
    # Throughput series covers the window and counts this minute's landings.
    assert len(m["throughput"]["series"]) == m["throughput"]["window_min"]
    assert sum(m["throughput"]["series"]) == 2
    assert m["throughput"]["per_min"] == m["throughput"]["series"][-1]
    # The §4 budget frame is present even when the serving process is away,
    # and the component status is one of the three honest states (BP32).
    assert m["p95_budget_ms"] == 300 and "p95_ms" in m
    assert m["serving_status"] in ("ok", "warming", "down")
    assert m["uptime_s"] >= 0
    # Review counts mirror the review_queue feeders.
    assert m["review"] == {"merges": 0, "quarantined": 0, "flagged": 0,
                           "total": 0}


def test_activity_feed_speaks_the_pipeline_voice(
        op_client, resolver, store, db, pipeline, dispatcher, tenant):
    from factories import land_document

    doc = land_document(pipeline, store, tenant)
    item = dispatcher.dispatch(tenant, doc.raw_document_id)
    db.execute("UPDATE dispatch_queue SET status = 'done', acked_at = now()"
               " WHERE id = %s", (item,))
    db.execute(
        """
        INSERT INTO extraction_runs
            (tenant_id, document_id, unit_hash, strategy, extractor,
             extractor_version, ontology_version, facts_staged,
             mentions_staged, quarantined)
        VALUES (%s, %s, %s, 'llm_joint', 'qwen3.6', 'test', %s, 5, 3, 1)
        """, (tenant, doc.id, f"h{uuid.uuid4().hex}", ONTOLOGY))
    tok = grant(resolver, tenant, ["reviewer"])

    events = op_client.get("/v1/monitor/activity",
                           headers=bearer(tok)).json()["events"]
    assert events, "no events for real pipeline state"
    # Newest first, and every line speaks `stage · detail`.
    ats = [e["at"] for e in events]
    assert ats == sorted(ats, reverse=True)
    assert all("·" in e["text"] for e in events)
    assert all(e["text"].split("·")[0].strip() in
               ("capture", "process", "extract", "resolve", "operator")
               for e in events)
    assert any("parsed · chunked · embedded" in e["text"] for e in events)


# ---------------------------------------------------------------------------
# Part A — the review reads
# ---------------------------------------------------------------------------


def test_reviews_listing_and_candidate_pair_evidence(
        op_client, resolver, store, db, pipeline, embedder, tenant):
    main, dup, candidate_id = seed_review_pair(store, pipeline, db, embedder,
                                               tenant)
    qid = db.execute(
        "INSERT INTO quarantined_extractions (tenant_id, reason, detail,"
        " extractor, extractor_version, ontology_version)"
        " VALUES (%s, 'unbound_predicate', 'retained_for', 't', 'v', %s)"
        " RETURNING id", (tenant, ONTOLOGY)).fetchone()["id"]
    tok = grant(resolver, tenant, ["reviewer"])

    listing = op_client.get("/v1/reviews", headers=bearer(tok)).json()
    assert listing["counts"] == {"merges": 1, "quarantined": 1, "flagged": 0,
                                 "total": 2}
    by_id = {i["id"]: i for i in listing["items"]}
    merge_item = by_id[f"match:{candidate_id}"]
    assert merge_item["kind"] == "merge"
    assert "Granite Botanicals" in merge_item["title"]
    assert merge_item["subtitle"] == "merge · 0.62 gray band"
    assert by_id[f"quarantine:{qid}"]["subtitle"] \
        == "quarantine · unbound_predicate"

    # The candidate-pair evidence panel.
    d = op_client.get(f"/v1/reviews/match:{candidate_id}",
                      headers=bearer(tok)).json()
    assert d["question"] == "Are these the same organization?"
    assert d["score"] == pytest.approx(0.62)
    assert d["thresholds"]["t_high"] == pytest.approx(0.95)   # policy row
    assert d["candidate_a"]["name"] == "Granite Botanicals"
    assert d["candidate_b"]["name"] == "Granite Botanicals Inc"
    assert d["candidate_a"]["fact_count"] == len(main.fact_ids)
    assert "asset_id" in d["candidate_a"]["identifiers"]
    # Evidence panels derive from the RECORDED features only.
    assert any("Names agree" in e for e in d["evidence_for"])
    assert any("Context embeddings are close" in e for e in d["evidence_for"])
    assert "No identifier overlap" in d["evidence_against"]
    assert "No corroborating graph edge" in d["evidence_against"]
    # The UI posts exactly these BP19 actions.
    assert d["actions"]["merge"]["params"] == {"candidate_id": candidate_id,
                                               "same": True}

    # A mention-left pair carries the source passage + provenance.
    mention_id = db.execute(
        "INSERT INTO entity_mentions (tenant_id, surface_text, entity_type,"
        " source_system, source_document_id, source_chunk_id,"
        " resolution_status) VALUES (%s, 'Granite Botanicals Inc',"
        " 'Organization', 'test', %s, %s, 'review') RETURNING id",
        (tenant, dup.doc.id, dup.child.id)).fetchone()["id"]
    mc2 = store.insert_match_candidate(MatchCandidate(
        tenant_id=tenant, left_type="mention", left_id=mention_id,
        right_type="entity", right_id=main.subj.id, match_score=0.58,
        match_method="embedding", band="gray", decision="review"))
    d2 = op_client.get(f"/v1/reviews/match:{mc2}",
                       headers=bearer(tok)).json()
    assert d2["candidate_b"]["role"] == "mention"
    assert d2["passage"]["document_title"] == "Vendor dupe"
    assert "Granite Botanicals Inc" in d2["passage"]["text"]
    assert d2["passage"]["chunk_id"] == dup.child.id


def test_reads_are_role_gated_tenant_scoped_and_write_free(
        op_client, resolver, store, db, pipeline, embedder, tenant):
    _, _, candidate_id = seed_review_pair(store, pipeline, db, embedder,
                                          tenant)
    # An agent read-principal gets 403 on every operator read.
    agent = grant(resolver, tenant, [])
    for path in ("/v1/monitor", "/v1/monitor/activity", "/v1/reviews",
                 f"/v1/reviews/match:{candidate_id}"):
        assert op_client.get(path, headers=bearer(agent)).status_code == 403
    # Unauthenticated: 401, no data.
    assert op_client.get("/v1/monitor").status_code == 401

    # Another tenant's reviewer sees an EMPTY world, and the item is absent.
    other = grant(resolver, f"{tenant}-other", ["reviewer"])
    m = op_client.get("/v1/monitor", headers=bearer(other)).json()
    assert m["landed"] == 0 and m["review"]["total"] == 0
    assert op_client.get(f"/v1/reviews/match:{candidate_id}",
                         headers=bearer(other)).status_code == 404

    # Reads audit nothing and mutate nothing.
    reviewer = grant(resolver, tenant, ["reviewer"])
    op_client.get("/v1/reviews", headers=bearer(reviewer))
    op_client.get("/v1/monitor", headers=bearer(reviewer))
    assert db.execute("SELECT count(*) AS n FROM operator_audit"
                      " WHERE tenant_id = %s", (tenant,)).fetchone()["n"] == 0
    assert store.get_match_candidate(tenant, candidate_id).decision \
        == "review"


# ---------------------------------------------------------------------------
# Part B — the UI's decide() contract, agreement proven through serving
# ---------------------------------------------------------------------------


def test_ui_decide_flow_merge_split_and_skip(
        op_client, read_stack, resolver, store, db, pipeline, embedder,
        tenant):
    """Exactly the calls app.js makes, in order: list → detail → POST the
    action from the detail's own `actions` block → counts drop → the change
    is visible through the READ serving layer. Skip is proven write-free."""
    catalog, read_client = read_stack
    register_serving_defaults(catalog, tenant)
    main, dup, candidate_id = seed_review_pair(store, pipeline, db, embedder,
                                               tenant)
    reviewer = grant(resolver, tenant, ["reviewer"])
    reader = grant(resolver, tenant, [])

    # The UI lists the queue and loads the top item.
    listing = op_client.get("/v1/reviews", headers=bearer(reviewer)).json()
    assert listing["counts"]["merges"] == 1
    item = listing["items"][0]
    detail = op_client.get(f"/v1/reviews/{item['id']}",
                           headers=bearer(reviewer)).json()

    # A "skip" performs NO request — prove nothing changed since listing.
    assert db.execute("SELECT count(*) AS n FROM operator_audit"
                      " WHERE tenant_id = %s", (tenant,)).fetchone()["n"] == 0

    # 'A' — merge: POST the action the detail itself declared.
    act = detail["actions"]["merge"]
    r = op_client.post(f"/v1/actions/{act['action']}", json=act["params"],
                       headers=bearer(reviewer))
    assert r.status_code == 200, r.text
    merge_id = r.json()["result"]["merge_id"]
    assert merge_id

    # The queue count drops — the UI's counter refresh sees it.
    listing = op_client.get("/v1/reviews", headers=bearer(reviewer)).json()
    assert listing["counts"]["merges"] == 0

    # THE AGREEMENT, via the UI's exact wiring: the merge shows through the
    # READ serving boundary.
    facts = read_client.post("/v1/ops/get_facts",
                             json={"entity_id": main.subj.id, "role": "any"},
                             headers=bearer(reader)).json()["facts"]
    assert dup.fact_ids <= {f["fact_id"] for f in facts}

    # 'S' — split the merge just made (the session's undo).
    r = op_client.post("/v1/actions/split_merge",
                       json={"merge_id": merge_id},
                       headers=bearer(reviewer))
    assert r.status_code == 200
    facts = read_client.post("/v1/ops/get_facts",
                             json={"entity_id": dup.subj.id, "role": "any"},
                             headers=bearer(reader)).json()["facts"]
    assert {f["fact_id"] for f in facts} == dup.fact_ids

    # The monitor's activity feed narrates what the operator just did.
    events = op_client.get("/v1/monitor/activity",
                           headers=bearer(reviewer)).json()["events"]
    assert any("operator · resolve_merge applied" in e["text"]
               for e in events)
    assert any("operator · split_merge applied" in e["text"]
               for e in events)


def test_concurrent_ui_polling_never_strands_the_store_transaction(
        op_client, resolver, store, db, pipeline, embedder, tenant):
    """Regression for a bug the live browser session caught: the UI polls
    monitor + activity + reviews CONCURRENTLY; unserialized, those
    transactions interleaved on the one store connection and stranded it
    idle-in-transaction — every later 'commit' became a savepoint inside a
    transaction that never ended, invisible to the rest of the world. Now
    all store-touching requests share one lock: hammer the API from many
    threads, then prove a write is visible from a SEPARATE connection and
    no backend is left idle-in-transaction."""
    _, _, candidate_id = seed_review_pair(store, pipeline, db, embedder,
                                          tenant)
    tok = grant(resolver, tenant, ["operator"])

    paths = ["/v1/monitor", "/v1/monitor/activity", "/v1/reviews",
             f"/v1/reviews/match:{candidate_id}", "/v1/alerts",
             "/v1/health"] * 5
    errors: list[str] = []

    def hit(path):
        try:
            r = op_client.get(path, headers=bearer(tok))
            if r.status_code != 200:
                errors.append(f"{path} -> {r.status_code}")
        except Exception as e:                      # pragma: no cover
            errors.append(f"{path} -> {type(e).__name__}")

    threads = [threading.Thread(target=hit, args=(p,)) for p in paths]
    for t in threads:
        t.start()
    # A write races the read burst — exactly the UI's decide-while-polling.
    r = op_client.post("/v1/actions/resolve_merge",
                       json={"candidate_id": candidate_id, "same": False},
                       headers=bearer(tok))
    assert r.status_code == 200, r.text
    for t in threads:
        t.join()
    assert not errors, errors

    # The write is visible from a SEPARATE connection — it truly committed.
    # (Under the bug, this read 'review' forever: the decision sat in a
    # savepoint inside a transaction that never ended.)
    assert db.execute(
        "SELECT decision FROM match_candidates WHERE id = %s",
        (candidate_id,)).fetchone()["decision"] == "auto_separate"
    # And no backend is left STRANDED mid-transaction. The stranded state
    # persists forever; legitimate transactions clear in milliseconds — the
    # age filter separates the bug from a passing blip on the shared stack.
    import time as _time
    _time.sleep(1.2)
    stuck = db.execute(
        "SELECT pid, application_name, query, xact_start, backend_start"
        " FROM pg_stat_activity"
        " WHERE datname = current_database()"
        "   AND state = 'idle in transaction'"
        "   AND now() - xact_start > interval '1 second'").fetchall()
    assert not stuck, stuck


def test_provisioned_credentials_log_into_the_console(op_client, tenant):
    """BP23 end to end: a credential minted by the PROVISIONING path (what
    deploy bootstrap / provision-operator print once) passes the console's
    login check and lands role-scoped — operator sees everything, reviewer
    sees review actions only, exactly what the UI unlock renders."""
    import hvac

    from knowledge_hub.config import settings
    from knowledge_hub.deploy_apply import provision_operator_credential

    client = hvac.Client(url=settings.bao_addr,
                         token=settings.bao_root_token)
    op_tok, _ = provision_operator_credential(
        client, settings.bao_kv_mount, tenant, "operator", "test-deploy")
    rev_tok, _ = provision_operator_credential(
        client, settings.bao_kv_mount, tenant, "reviewer", "test-deploy")

    # The exact call app.js makes at unlock.
    r = op_client.get("/v1/actions", headers=bearer(op_tok))
    assert r.status_code == 200
    operator_actions = {a["name"] for a in r.json()["actions"]}
    assert "pause_source" in operator_actions          # operator scope

    r = op_client.get("/v1/actions", headers=bearer(rev_tok))
    assert r.status_code == 200
    reviewer_actions = {a["name"] for a in r.json()["actions"]}
    assert "resolve_merge" in reviewer_actions
    assert "pause_source" not in reviewer_actions      # reviewer scope ONLY
    # And reviewer reads work (the monitor is review-role visible).
    assert op_client.get("/v1/monitor",
                         headers=bearer(rev_tok)).status_code == 200


# ---------------------------------------------------------------------------
# Part B — the UI ships in the package and serves offline, no fakes
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# BP25 on-site hardening — the console-side fixes, over real HTTP
# ---------------------------------------------------------------------------


def test_agent_token_is_rejected_at_the_console_login(op_client, resolver,
                                                      tenant):
    """F3: the exact login call app.js makes (GET /v1/actions). An AGENT
    serving credential resolves but has no console role — it used to get a
    200 + empty catalog and unlock a console where every read 403s
    silently: a blank dashboard under SYSTEM : NOMINAL. Now it is refused
    at the door, with the right words."""
    agent = grant(resolver, tenant, [])
    r = op_client.get("/v1/actions", headers=bearer(agent))
    assert r.status_code == 403
    detail = r.json()["detail"]
    assert "AGENT serving credential" in detail
    assert "OPERATOR CONSOLE credential" in detail
    # The real console credentials still pass, both roles.
    for roles in (["operator"], ["reviewer"]):
        tok = grant(resolver, tenant, roles)
        assert op_client.get("/v1/actions",
                             headers=bearer(tok)).status_code == 200


def test_health_reports_vault_status_distinctly(op_client):
    """F1: /v1/health now carries vault_status (ok|sealed|unreachable) so
    the lock screen can branch 'unseal it' vs 'bad credential'. Against the
    live (unsealed) dev vault that is 'ok'; the sealed path is proven in
    test_onsite_hardening with a sealed-vault fake."""
    body = op_client.get("/v1/health").json()
    assert body["vault"] is True
    assert body["vault_status"] == "ok"


def test_passage_lookup_answers_the_trust_question(
        op_client, read_stack, resolver, store, db, pipeline, embedder,
        tenant):
    """F18: 'where did this fact come from?' — a fact served through the
    READ boundary carries document_id/chunk_id; GET /v1/passages/<chunk_id>
    dereferences that to the passage text + document title through a
    proper, role-gated door. No psql."""
    catalog, read_client = read_stack
    register_serving_defaults(catalog, tenant)
    corpus = seed_passage(pipeline, store, db, embedder, tenant,
                          title="Trace doc", name="Traceable Org",
                          text="Traceable Org retains records for seven "
                               "years under SOP-90.")
    reader = grant(resolver, tenant, [])
    facts = read_client.post("/v1/ops/get_facts",
                             json={"entity_id": corpus.subj.id,
                                   "role": "any"},
                             headers=bearer(reader)).json()["facts"]
    chunk_id = next(f["spine"]["chunk_id"] for f in facts
                    if f["spine"]["chunk_id"])

    reviewer = grant(resolver, tenant, ["reviewer"])
    r = op_client.get(f"/v1/passages/{chunk_id}", headers=bearer(reviewer))
    assert r.status_code == 200, r.text
    passage = r.json()
    assert passage["document_title"] == "Trace doc"
    assert "Traceable Org" in passage["text"]
    assert passage["chunk_id"] == chunk_id

    # Scoped exactly like every other operator read: another tenant sees
    # absence, an agent principal is refused, garbage ids are absent.
    other = grant(resolver, f"{tenant}-other", ["reviewer"])
    assert op_client.get(f"/v1/passages/{chunk_id}",
                         headers=bearer(other)).status_code == 404
    agent = grant(resolver, tenant, [])
    assert op_client.get(f"/v1/passages/{chunk_id}",
                         headers=bearer(agent)).status_code == 403
    assert op_client.get("/v1/passages/nope",
                         headers=bearer(reviewer)).status_code == 404


def test_failed_document_surfaces_in_the_badge_count_and_the_alerts_cli(
        op_client, resolver, store, db, pipeline, dispatcher, tenant,
        monkeypatch, capsys):
    """F5: a failed queue item is VISIBLE — monitor.alerts_open feeds the
    UI badge (the old status='error' count was structurally 0), and
    `khctl alerts` lists it; --retry and --ack act on it through the
    existing endpoints (their first consumer)."""
    from knowledge_hub import deploy_cli

    raw = make_raw(tenant)
    pipeline.ingest_raw(raw)
    item_id = dispatcher.dispatch(tenant, raw.id)
    db.execute("UPDATE dispatch_queue SET last_error = 'docling exploded'"
               " WHERE id = %s", (item_id,))
    tok = grant(resolver, tenant, ["operator"])

    monitor = op_client.get("/v1/monitor", headers=bearer(tok)).json()
    assert monitor["alerts_open"] == 1

    base = str(op_client.base_url)
    monkeypatch.setenv("KH_OPERATOR_TOKEN", tok)

    rc = deploy_cli.main(["alerts", "--url", base])
    out = capsys.readouterr().out
    assert rc == 0
    assert "docling exploded" in out and f"#{item_id}" in out

    rc = deploy_cli.main(["alerts", "--url", base,
                          "--retry", f"dispatch:{item_id}"])
    out = capsys.readouterr().out
    assert rc == 0 and "requeued" in out
    row = db.execute("SELECT status, acknowledged_at FROM dispatch_queue"
                     " WHERE id = %s", (item_id,)).fetchone()
    assert row["status"] == "queued" and row["acknowledged_at"] is None

    rc = deploy_cli.main(["alerts", "--url", base,
                          "--ack", f"dispatch:{item_id}"])
    out = capsys.readouterr().out
    assert rc == 0 and "acknowledged" in out
    row = db.execute("SELECT acknowledged_at, acknowledged_by"
                     " FROM dispatch_queue WHERE id = %s",
                     (item_id,)).fetchone()
    assert row["acknowledged_at"] is not None

    # Acknowledged -> the badge count drops with it.
    monitor = op_client.get("/v1/monitor", headers=bearer(tok)).json()
    assert monitor["alerts_open"] == 0


def test_alerts_cli_unreachable_service_is_an_actionable_fail(monkeypatch,
                                                              capsys):
    from knowledge_hub import deploy_cli

    monkeypatch.setenv("KH_OPERATOR_TOKEN", "tok-x")
    rc = deploy_cli.main(["alerts", "--url", "http://127.0.0.1:9"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "[FAIL]" in out and "khctl console" in out


def test_console_shell_carries_the_bp25_honesty_fixes(op_client):
    html = op_client.get("/ui/").text
    # L6: the dev note is gone from the lock screen.
    assert "screen styling pending Design" not in html
    # L7: the lock screen names the lost-token recovery.
    assert "provision-operator" in html
    # L5, superseded by Stage 2: the dead search went from a lying field to
    # an honest NOT-YET-WIRED chip to GONE — a disabled control still
    # spends attention. Neither form may return until search works.
    assert "Look up an entity, a document, a source" not in html
    assert "LOOKUP" not in html

    js = op_client.get("/ui/app.js").text
    # F1: login failures consult /v1/health and can name a SEALED vault.
    assert "vault_status" in js and "SEALED" in js
    assert "credentialFailureMessage" in js
    # F3: a no-console-role token is surfaced, not swallowed.
    assert "no console role" in js
    # F5: the badge counts the real alert view.
    assert "alerts_open" in js


# ---------------------------------------------------------------------------
# BP32 — the BP28 console-honesty fixes (findings 12/13/14 → tasks #22–#24)
# ---------------------------------------------------------------------------


def test_monitor_serving_status_is_honest_about_the_third_component(
        store, tenant):
    """BP28 #22, server truth: STACK:HEALTH's third component is the serving
    PROCESS, not 'a p95 sample exists'. Reachable with traffic → ok (the p95
    rides along); reachable but QUIET (endpoints == {}) → warming — a
    healthy deploy with no traffic yet is NOT a failure; unreachable → down.
    The old payload carried only p95_ms, which conflated healthy-but-quiet
    with dead — the 2/3 (67%) tile on a fully healthy deploy."""
    import http.server
    import json as _json

    class Metrics(http.server.BaseHTTPRequestHandler):
        payload: dict = {}

        def do_GET(self):
            body = _json.dumps(type(self).payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Metrics)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/v1/metrics"
    try:
        # Healthy but quiet: the metrics door answers, no samples yet.
        Metrics.payload = {"budget_p95_ms": 300, "endpoints": {}}
        m = OperatorReadService(store, serving_metrics_url=url) \
            .monitor(tenant)
        assert m["serving_status"] == "warming" and m["p95_ms"] is None
        # Traffic: ok, and the retrieve p95 rides along unchanged.
        Metrics.payload = {"budget_p95_ms": 300, "endpoints": {
            "retrieve": {"count": 4, "errors": 0, "p50_ms": 10.0,
                         "p95_ms": 42.0, "p99_ms": 60.0,
                         "within_budget": True}}}
        m = OperatorReadService(store, serving_metrics_url=url) \
            .monitor(tenant)
        assert m["serving_status"] == "ok" and m["p95_ms"] == 42.0
    finally:
        server.shutdown()
        server.server_close()
    # Genuine degradation: nothing listens there any more.
    m = OperatorReadService(store, serving_metrics_url=url).monitor(tenant)
    assert m["serving_status"] == "down" and m["p95_ms"] is None


def test_health_tile_renders_real_component_health(op_client):
    """BP28 #22, shipped console: the tile counts serving_status — warming
    counts GREEN (a healthy deploy reads fully healthy, consistent with
    check_stack and the SYSTEM : NOMINAL footer), a down component is NAMED,
    a sealed vault says SEALED — and the 'a p95 sample exists' component is
    dead."""
    js = op_client.get("/ui/app.js").text
    assert "m.serving_status" in js
    assert 'm.serving_status !== "down"' in js       # down degrades the tile
    assert 'm.serving_status === "warming"' in js    # warming is neutral
    assert 'h.vault_status === "sealed"' in js       # a sealed vault is named
    assert "DEGRADED" in js
    # The old third component — health-by-latency-sample — is gone.
    assert "m.p95_ms !== null" not in js


def test_review_action_labels_follow_the_item_type(op_client):
    """BP28 #23: the DECIDE buttons say what the keys DO for the item on
    screen — merge → merge/keep-separate, quarantine → resolve/dismiss,
    flagged → resolve only (its R posts nothing, so no R button) — instead
    of merge labels on everything."""
    js = op_client.get("/ui/app.js").text
    assert "REVIEW_KINDS" in js
    for label in ("Merge them", "Keep separate",
                  "Resolve — record the correction", "Dismiss",
                  "Resolve — tag stands corrected"):
        assert label in js, f"missing per-type label {label!r}"
    # The key legends match the instruction line per type.
    assert "A merge · R keep separate · S split · space skip" in js
    assert "A resolve · R dismiss · space skip" in js
    assert "A resolve · space skip" in js
    # flagged HIDES the R button rather than mislabeling it.
    assert 'classList.toggle("kh-hide", !kind.r)' in js
    # And the static shell no longer claims merge keys for every item.
    html = op_client.get("/ui/").text
    assert "A merge · R keep separate" not in html


def test_review_queue_explains_itself(op_client):
    """BP28 #24: the queue frames itself — what the screen is, why a human
    is needed, what A/R/S/space do — and the key legend re-renders per
    item type beside the per-type button labels."""
    html = op_client.get("/ui/").text
    assert "REVIEW QUEUE : WHY A HUMAN" in html
    assert 'id="rv-keys"' in html
    assert "calls the pipeline refuses to make alone" in html
    assert "audit trail" in html
    js = op_client.get("/ui/app.js").text
    assert '$("rv-keys").textContent = kind.keys' in js


def test_ui_serves_from_the_operator_service_with_no_fakes(op_client):
    # The shell serves without auth (it is static; it renders nothing until
    # a credential resolves) — and / redirects into it.
    index = op_client.get("/ui/")
    assert index.status_code == 200
    assert index.headers["content-type"].startswith("text/html")
    html = index.text
    assert "OPERATOR : CONSOLE" in html
    assert "OPERATOR : LOCKED" in html          # the locked state exists
    assert "QUEUE : CLEAR" in html              # the empty-queue state exists
    assert "Can’t reach the system" in html     # the offline state exists
    assert "fonts.googleapis.com" not in html   # air-gapped: no CDN anywhere
    assert 'src="./app.js"' in html

    js = op_client.get("/ui/app.js")
    assert js.status_code == 200
    assert js.headers["content-type"].startswith("text/javascript")
    # Every fake from the design-tool mock is dead.
    for fake in ("wobble", "POOL", "Math.random", "Math.sin"):
        assert fake not in js.text, f"design-tool fake {fake!r} shipped"
    # And the real bindings are present.
    for real in ("/v1/monitor", "/v1/monitor/activity", "/v1/reviews",
                 "/v1/actions/resolve_merge", "split_merge",
                 "sessionStorage"):
        assert real in js.text

    # No traversal, no surprise files (a client-normalized "/pyproject.toml"
    # lands outside /ui and hits the auth wall instead — either way, never
    # a file).
    assert op_client.get("/ui/../pyproject.toml").status_code in (400, 401,
                                                                  404)
    assert op_client.get("/ui/nope.js").status_code == 404
    # The root points a browser at the console.
    root = op_client.get("/")
    assert root.status_code == 302 and "/ui/" in root.text


# ---------------------------------------------------------------------------
# d.s operator-console pass, Stage 1 — tab disposition + the two new wires
# ---------------------------------------------------------------------------


class _FakeOllamaReport:
    def __init__(self, reachable=True, models=(), version="0.9.9",
                 error=None):
        self.reachable = reachable
        self.models = list(models)
        self.version = version
        self.error = error


def test_inference_read_reports_the_boxes_own_list(store, pipeline, scorer,
                                                   embedder, secrets):
    """GET /v1/inference's service half: the served-model list comes from
    the BOX (probe_ollama's /api/tags read), never a hardcoded list, and
    the configured roles are matched by phase_models' tag rule (name == tag
    or tag is name:variant) so 'configured but not served' is visible."""
    from knowledge_hub.config import settings

    calls: list[str] = []

    def probe(host):
        calls.append(host)
        return _FakeOllamaReport(models=[f"{settings.embedding_model}:latest",
                                         f"{settings.extraction_model}:q8",
                                         "mistral:7b"])

    service = OperatorService(store, ResolutionService(pipeline, scorer,
                                                       embedder),
                              SourceRegistry(store), secrets,
                              inference_probe=probe)
    status = service.inference_status()
    assert calls == [settings.ollama_host]      # asked the configured box
    assert status["target"] == settings.ollama_host
    assert status["reachable"] is True
    assert "mistral:7b" in status["models"]     # everything served is shown
    assert status["embedding"]["served"] is True     # bge-m3:latest matches
    assert status["extraction"]["served"] is True    # qwen3.6:q8 matches
    # Cached: two console surfaces read this; one probe answers both.
    service.inference_status()
    assert len(calls) == 1

    # Unreachable box: honest error, roles UNKNOWN rather than fabricated.
    down = OperatorService(store, ResolutionService(pipeline, scorer,
                                                    embedder),
                           SourceRegistry(store), secrets,
                           inference_probe=lambda host: _FakeOllamaReport(
                               reachable=False, version=None,
                               error="ConnectionError: refused"))
    status = down.inference_status()
    assert status["reachable"] is False
    assert status["models"] == []
    assert "refused" in status["error"]
    assert status["embedding"]["served"] is False

    # A model the box does NOT serve reads served=False even when reachable.
    partial = OperatorService(store, ResolutionService(pipeline, scorer,
                                                       embedder),
                              SourceRegistry(store), secrets,
                              inference_probe=lambda host: _FakeOllamaReport(
                                  models=[f"{settings.embedding_model}:latest"]))
    status = partial.inference_status()
    assert status["embedding"]["served"] is True
    assert status["extraction"]["served"] is False


def test_inference_read_is_role_gated(op_client, resolver, tenant):
    """The route half: role-gated exactly like /v1/components — an agent
    serving credential gets 403 without a probe ever firing; an operator
    gets the payload shape the tab renders."""
    from knowledge_hub.config import settings

    agent = grant(resolver, tenant, [])
    assert op_client.get("/v1/inference",
                         headers=bearer(agent)).status_code == 403
    assert op_client.get("/v1/inference").status_code == 401

    operator = grant(resolver, tenant, ["operator"])
    resp = op_client.get("/v1/inference", headers=bearer(operator))
    assert resp.status_code == 200
    body = resp.json()
    assert body["target"] == settings.ollama_host
    for key in ("reachable", "models", "embedding", "extraction"):
        assert key in body
    assert body["embedding"]["model"] == settings.embedding_model
    assert body["extraction"]["model"] == settings.extraction_model


def test_console_tab_disposition_wire_two_hide_two_defer_one(op_client):
    """d.s Stage 1: every VISIBLE tab is fully wired — the placeholder
    surface is gone, the three unwired tabs are out of the visible set
    (Sources & access: future build; System connections: folded into
    Inference; Facts & entities: next pass, designed from the real
    corpus), and the two newly-wired tabs carry their real bindings."""
    html = op_client.get("/ui/").text
    # Hidden tabs are not offered.
    for gone in ('data-tab="sources"', 'data-tab="topology"',
                 'data-tab="facts"'):
        assert gone not in html, f"hidden tab still visible: {gone}"
    # The lying placeholder surface no longer exists at all.
    assert "SURFACE : NOT YET WIRED" not in html
    assert "follow-on build" not in html
    # Errors & health: wired panel with the retry/acknowledge contract and
    # an empty state DISTINCT from not-loaded.
    assert 'id="panel-health"' in html
    assert "ALERTS : NONE" in html                     # honest empty state
    assert "checking for open alerts" in html          # distinct not-loaded
    assert "No errors — nothing needs attention." in html
    # Inference: the thin honest tab — target + reachability + served list.
    assert 'id="panel-inference"' in html
    assert "INFERENCE : WHERE MODEL WORK RUNS" in html
    assert "no document text ever leaves your network" in html

    js = op_client.get("/ui/app.js").text
    # The wires are real: alerts read + the two write actions, and the
    # inference read. No orphaned placeholder logic remains.
    for real in ("/v1/alerts", "/v1/actions/", "retry_failed_item",
                 "acknowledge_alert", "/v1/inference"):
        assert real in js
    assert "OTHER_TITLES" not in js
    assert "panel-other" not in js
    # A degraded SOURCE alert offers no retry button — its remedy is named
    # instead (retry_failed_item knows only dispatch/extraction queues).
    assert "Resume it " in js or "Resume it" in js


def test_stage2_no_lying_copy_or_fabricated_status(op_client):
    """d.s Stage 2, item by item: no dead surfaces advertised, no
    design-tool residue, no numbers or names frozen into the shell that
    real state can drift away from, and no fabricated progress."""
    html = op_client.get("/ui/").text
    js = op_client.get("/ui/app.js").text

    # The Ontology header no longer denies the re-extract box that ships
    # on the same tab.
    assert "a later build" not in html
    assert "re-extract box at the bottom of this tab" in html
    # Design-tool residue is gone.
    assert "CONCEPT 04" not in html and "VIRTUAL SELF" not in html
    # Model names + embedding dim come from /v1/inference, never the shell.
    for frozen in ("bge-m3", "qwen3.6", "1024-dim"):
        assert frozen not in html, f"model claim frozen into the shell: {frozen}"
        assert frozen not in js, f"model claim frozen into the JS: {frozen}"
    assert "embedding_dim" in js and "state.inference" in js
    # The p95 budget renders from the server's number.
    assert "budget 300 ms" not in html
    assert "p95_budget_ms" in js
    # The footer states only what it can source: the page's own address and
    # the health-reported posture — the static box claim is gone.
    assert "appliance · single box" not in html
    assert "127.0.0.1:8081" not in html
    assert "location.host" in js and "footer-posture" in js
    # No fabricated progress: the hardwired 60%-full source bar is dead;
    # in-progress claims activity (animated stripe), not extent — and the
    # served-but-never-rendered last_run_at is finally on screen.
    assert "backfill_done ? 0 : 40" not in js
    assert "last_run_at" in js
    # Pre-data gauges/bars start EMPTY — no percentage exists before the
    # first poll answers.
    for fake in ("conic-gradient(#7be0c8 100%", "conic-gradient(#9fc0ff 30%",
                 "conic-gradient(#eadf9a 97%", "conic-gradient(#c9b8ff 61%",
                 "inset:0 26% 0 0", "inset:0 32% 0 0", "inset:0 42% 0 0",
                 "inset:0 55% 0 0", "inset:0 18% 0 0"):
        assert fake not in html, f"pre-data fake survived: {fake}"
    # A 5xx is not NOMINAL: erroring and unreachable are distinct states.
    assert "SYSTEM : ERRORING" in js
    assert "resp.status >= 500" in js
    # The uptime label says what the number is.
    assert "SERVICE UPTIME" in html
