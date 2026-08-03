"""Operator/admin write API (Build Prompt 19), tested against the REAL stack
over REAL HTTP — with the READ serving service (S5) running beside it, so
the central agreement is proven live: the UI reads through serving and acts
through the operator API, and the two see the same world.

What must hold:

* resolve_merge -> entity_merges records it (reversible), the labels store
  gets the human decision, the audit trail records it, and the change is
  VISIBLE through the read serving layer;
* split_merge reverses a prior merge and re-resolves the dependent facts —
  again visible through serving;
* a cross-tenant write is REFUSED (absence, nothing mutated);
* an agent read-principal attempting ANY write is REFUSED (deny-by-default
  role gate) — and the refusal is itself audited;
* unknown / revoked / malformed principals are REFUSED fail-closed;
* pause_source ACTUALLY pauses capture; resume_source resumes it;
* triage_quarantine moves the item out of the review queue and records the
  decision as a flywheel correction label;
* acknowledge_alert clears the alert; retry_failed_item requeues;
* the write choke point cannot be bypassed: identity-shaped params are
  unregistrable, unscoped write specs are unconstructable, and the tenant
  is always injected from the principal.
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
    WriteOperation,
    WriteOperationRejected,
    WriteParamSpec,
    register_operator_defaults,
)
from knowledge_hub.resolution import ResolutionService
from knowledge_hub.retrieval import DenseRetrievalService
from knowledge_hub.serving import Principal
from knowledge_hub.service_http import (
    KnowledgeHubServingService,
    ServingApp,
    make_server,
)
from knowledge_hub.sources_fs import FilesystemSourceAdapter

# ---------------------------------------------------------------------------
# Fixtures: ONE operator stack + ONE read-serving stack, both over real HTTP.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def db(test_dsn: str) -> psycopg.Connection:
    conn = psycopg.connect(test_dsn, autocommit=True, row_factory=dict_row)
    yield conn
    conn.close()


@pytest.fixture(scope="module")
def resolver() -> OpenBaoCredentialResolver:
    return OpenBaoCredentialResolver()


@pytest.fixture(scope="module")
def resolution(pipeline, scorer, embedder) -> ResolutionService:
    return ResolutionService(pipeline, scorer, embedder)


@pytest.fixture(scope="module")
def operator_app(store, resolution, secrets, resolver) -> OperatorApp:
    service = OperatorService(store, resolution, SourceRegistry(store),
                              secrets)
    gate = OperatorGate(store)
    register_operator_defaults(gate, service)
    return OperatorApp(gate, service, resolver)


@pytest.fixture(scope="module")
def op_client(operator_app) -> httpx.Client:
    server = make_server(operator_app, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with httpx.Client(base_url=f"http://127.0.0.1:{server.server_address[1]}",
                      timeout=60.0) as c:
        yield c
    server.shutdown()


@pytest.fixture(scope="module")
def read_catalog(test_dsn, embedder):
    choke = PostgresChokePoint(dsn=test_dsn)
    catalog = InProcessOperationCatalog(choke, embedder)
    yield choke, catalog
    choke.close()


@pytest.fixture(scope="module")
def read_client(read_catalog, embedder, resolver) -> httpx.Client:
    """The S5 READ boundary, running live beside the operator API — the
    'reads via serving, acts via operator' agreement is asserted through
    real sockets on both sides."""
    choke, catalog = read_catalog
    service = KnowledgeHubServingService(
        choke, catalog, DenseRetrievalService(choke, embedder, catalog))
    assert service.warm()
    server = make_server(ServingApp(service, resolver), "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with httpx.Client(base_url=f"http://127.0.0.1:{server.server_address[1]}",
                      timeout=60.0) as c:
        yield c
    server.shutdown()


@pytest.fixture()
def surface(read_catalog, tenant) -> str:
    """This test's tenant, registered on the READ catalog (so serving can
    answer for it)."""
    _, catalog = read_catalog
    register_serving_defaults(catalog, tenant)
    return tenant


def grant(resolver, tenant: str, roles: list[str]) -> str:
    token = f"tok-{uuid.uuid4().hex}"
    resolver.register_principal(token, Principal(
        tenant_id=tenant, principal_id=f"op-{uuid.uuid4().hex[:8]}",
        roles=roles))
    return token


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def review_candidate(store, tenant: str, left_id: int, right_id: int) -> int:
    """One entity-entity pair awaiting human review — the review queue's
    'match' feeder, exactly what the UI's resolve button acts on."""
    return store.insert_match_candidate(MatchCandidate(
        tenant_id=tenant, left_type="entity", left_id=left_id,
        right_type="entity", right_id=right_id, match_score=0.91,
        match_method="probabilistic", band="gray", decision="review"))


def read_fact_ids(read_client, token: str, entity_id: int) -> set[int]:
    r = read_client.post("/v1/ops/get_facts",
                         json={"entity_id": entity_id, "role": "any"},
                         headers=bearer(token))
    assert r.status_code == 200, r.text
    return {f["fact_id"] for f in r.json()["facts"]}


def audit_rows(db, tenant: str) -> list[dict]:
    return db.execute(
        "SELECT * FROM operator_audit WHERE tenant_id = %s ORDER BY id",
        (tenant,)).fetchall()


# ---------------------------------------------------------------------------
# Review resolution: merge -> audited + labeled + reversible + READ-VISIBLE
# ---------------------------------------------------------------------------


def test_resolve_merge_records_everything_and_read_serving_agrees(
        op_client, read_client, resolver, store, db, pipeline, embedder,
        surface):
    main = seed_passage(pipeline, store, db, embedder, surface,
                        title="Vendor master", name="Granite Botanicals",
                        text="Granite Botanicals operates the northern "
                             "cultivation site under Net-30 terms.")
    dup = seed_passage(pipeline, store, db, embedder, surface,
                       title="Vendor dupe", name="Granite Botanicals Inc",
                       text="Granite Botanicals Inc appears in the vendor "
                            "ledger as the same supplier.")
    candidate_id = review_candidate(store, surface,
                                    left_id=dup.subj.id,
                                    right_id=main.subj.id)
    reviewer_tok = grant(resolver, surface, ["reviewer"])
    reader_tok = grant(resolver, surface, [])

    # Before: each entity serves its own facts through the READ layer.
    assert read_fact_ids(read_client, reader_tok, dup.subj.id) == dup.fact_ids

    r = op_client.post("/v1/actions/resolve_merge",
                       json={"candidate_id": candidate_id, "same": True},
                       headers=bearer(reviewer_tok))
    assert r.status_code == 200, r.text
    body = r.json()
    merge_id = body["result"]["merge_id"]
    assert body["result"]["decision"] == "merged" and merge_id
    assert body["snapshot_ref"] == f"entity_merges:{merge_id}"
    assert body["tenant_id"] == surface

    # 1. The domain recorded it REVERSIBLY (snapshot present, not reversed).
    merge = store.get_entity_merge(surface, merge_id)
    assert merge.surviving_entity_id == main.subj.id
    assert merge.merged_entity_id == dup.subj.id
    assert merge.reversed_at is None
    assert merge.merged_snapshot["fact_sides"]     # the undo data is real

    # 2. The flywheel got the human decision (one action, two records).
    (label,) = db.execute(
        "SELECT * FROM labels WHERE tenant_id = %s", (surface,)).fetchall()
    assert label["label_type"] == "er_match"
    assert label["source"] == "human_review"
    assert label["payload"]["candidate_id"] == candidate_id

    # 3. The audit trail recorded who/what/when + the snapshot ref.
    (audit,) = audit_rows(db, surface)
    assert audit["action"] == "resolve_merge"
    assert audit["outcome"] == "applied"
    assert audit["target"] == f"match_candidate:{candidate_id}"
    assert audit["snapshot_ref"] == f"entity_merges:{merge_id}"
    assert audit["principal_id"].startswith("op-")
    assert audit["id"] == body["audit_id"]

    # 4. THE AGREEMENT: the change is visible through the READ serving
    #    layer — the absorbed entity's facts now serve under the survivor,
    #    and the absorbed entity serves nothing (absence).
    survivor_facts = read_fact_ids(read_client, reader_tok, main.subj.id)
    assert dup.fact_ids <= survivor_facts
    assert main.fact_ids <= survivor_facts
    assert read_fact_ids(read_client, reader_tok, dup.subj.id) == set()


def test_split_merge_reverses_and_read_serving_agrees(
        op_client, read_client, resolver, store, db, pipeline, embedder,
        surface):
    main = seed_passage(pipeline, store, db, embedder, surface,
                        title="Split master", name="Zenith Widgets",
                        text="Zenith Widgets operates the packaging line.")
    dup = seed_passage(pipeline, store, db, embedder, surface,
                       title="Split dupe", name="Zenith Widgets LLC",
                       text="Zenith Widgets LLC references Net-30 terms.")
    candidate_id = review_candidate(store, surface, left_id=dup.subj.id,
                                    right_id=main.subj.id)
    reviewer_tok = grant(resolver, surface, ["reviewer"])
    reader_tok = grant(resolver, surface, [])

    merged = op_client.post("/v1/actions/resolve_merge",
                            json={"candidate_id": candidate_id, "same": True},
                            headers=bearer(reviewer_tok)).json()
    merge_id = merged["result"]["merge_id"]
    assert read_fact_ids(read_client, reader_tok, dup.subj.id) == set()

    r = op_client.post("/v1/actions/split_merge",
                       json={"merge_id": merge_id},
                       headers=bearer(reviewer_tok))
    assert r.status_code == 200, r.text
    assert r.json()["result"]["restored_entity_id"] == dup.subj.id

    # The merge row is reversed, not deleted (history stays).
    merge = store.get_entity_merge(surface, merge_id)
    assert merge.reversed_at is not None

    # The reversal is the flywheel's hard negative.
    nonmatch = [l for l in db.execute(
        "SELECT * FROM labels WHERE tenant_id = %s", (surface,)).fetchall()
        if l["label_type"] == "er_nonmatch" and l["source"] == "reversal"]
    assert len(nonmatch) == 1
    assert nonmatch[0]["payload"]["merge_id"] == merge_id

    # READ-VISIBLE both ways: the dependent facts re-resolved back apart.
    assert read_fact_ids(read_client, reader_tok, dup.subj.id) == dup.fact_ids
    survivor_facts = read_fact_ids(read_client, reader_tok, main.subj.id)
    assert survivor_facts == main.fact_ids
    # Audit shows both actions in order.
    actions = [a["action"] for a in audit_rows(db, surface)]
    assert actions == ["resolve_merge", "split_merge"]


def test_keep_separate_labels_the_nonmatch_and_merges_nothing(
        op_client, resolver, store, db, pipeline, embedder, surface):
    a = seed_passage(pipeline, store, db, embedder, surface,
                     title="Homonym A", name="Apex Labs",
                     text="Apex Labs operates the QA bench.")
    b = seed_passage(pipeline, store, db, embedder, surface,
                     title="Homonym B", name="Apex Labs",
                     text="A different Apex Labs references Net-30 terms.")
    candidate_id = review_candidate(store, surface, left_id=b.subj.id,
                                    right_id=a.subj.id)
    tok = grant(resolver, surface, ["reviewer"])

    r = op_client.post("/v1/actions/resolve_merge",
                       json={"candidate_id": candidate_id, "same": False},
                       headers=bearer(tok))
    assert r.status_code == 200
    assert r.json()["result"] == {"decision": "kept_separate",
                                  "merge_id": None}
    assert store.get_match_candidate(surface, candidate_id).decision \
        == "auto_separate"
    (label,) = db.execute("SELECT * FROM labels WHERE tenant_id = %s",
                          (surface,)).fetchall()
    assert label["label_type"] == "er_nonmatch"
    # Both homonyms still exist — the registry tolerates them.
    assert store.get_entity(surface, a.subj.id) is not None
    assert store.get_entity(surface, b.subj.id) is not None


# ---------------------------------------------------------------------------
# The gate: cross-tenant, read-principal, fail-closed, unmintable bypass
# ---------------------------------------------------------------------------


def test_cross_tenant_write_is_refused_and_nothing_mutates(
        op_client, resolver, store, db, pipeline, embedder, surface):
    victim = seed_passage(pipeline, store, db, embedder, surface,
                          title="Victim", name="Target Org",
                          text="Target Org operates the cold room.")
    other = seed_passage(pipeline, store, db, embedder, surface,
                         title="Other", name="Other Org",
                         text="Other Org references Net-30.")
    candidate_id = review_candidate(store, surface, left_id=other.subj.id,
                                    right_id=victim.subj.id)

    intruder_tenant = f"{surface}-intruder"
    intruder = grant(resolver, intruder_tenant, ["reviewer", "operator"])

    # A fully-privileged principal of ANOTHER tenant: the target simply
    # does not exist in its scope — 404, and nothing changed.
    r = op_client.post("/v1/actions/resolve_merge",
                       json={"candidate_id": candidate_id, "same": True},
                       headers=bearer(intruder))
    assert r.status_code == 404
    assert store.get_match_candidate(surface, candidate_id).decision \
        == "review"
    assert store.get_entity(surface, other.subj.id) is not None

    # Same for ingestion control against another tenant's source.
    SourceRegistry(store).register(surface, "fs-victim", "filesystem")
    r = op_client.post("/v1/actions/pause_source",
                       json={"source_ref": "fs-victim"},
                       headers=bearer(intruder))
    assert r.status_code == 404
    assert SourceRegistry(store).get(surface, "fs-victim").status == "active"

    # The attempts were audited — in the INTRUDER's tenant, as failures.
    attempts = audit_rows(db, intruder_tenant)
    assert [a["outcome"] for a in attempts] == ["failed", "failed"]
    assert audit_rows(db, surface) == []      # victim tenant: no audit noise


def test_agent_read_principal_may_do_none_of_this(op_client, resolver, db,
                                                  surface):
    for roles in ([], [f"label-role-{uuid.uuid4().hex[:8]}"]):
        tok = grant(resolver, surface, roles)
        for action, body in [
                ("resolve_merge", {"candidate_id": 1, "same": True}),
                ("split_merge", {"merge_id": 1}),
                ("triage_quarantine", {"quarantine_id": 1,
                                       "decision": "dismissed"}),
                ("pause_source", {"source_ref": "x"}),
                ("add_source", {"source_ref": "x",
                                "source_system": "filesystem"}),
                ("acknowledge_alert", {"kind": "dispatch", "item_id": 1})]:
            r = op_client.post(f"/v1/actions/{action}", json=body,
                               headers=bearer(tok))
            assert r.status_code == 403, (roles, action, r.status_code)
            assert r.json() == {"error": "forbidden"}
        # The action catalog REFUSES it outright (BP25/F3: the console's
        # login check — an agent token must not unlock a blank console),
        # and alerts are off-limits.
        catalog = op_client.get("/v1/actions", headers=bearer(tok))
        assert catalog.status_code == 403
        assert "AGENT serving credential" in catalog.json()["detail"]
        assert op_client.get("/v1/alerts",
                             headers=bearer(tok)).status_code == 403
    # Every refused attempt is an audit row (a security signal, kept).
    refused = [a for a in audit_rows(db, surface) if a["outcome"] == "refused"]
    assert len(refused) == 12
    assert all(a["error"] == "insufficient role" for a in refused)


def test_unknown_revoked_malformed_principals_fail_closed(op_client,
                                                          resolver, surface):
    import hvac

    from knowledge_hub.config import settings

    assert op_client.post("/v1/actions/resolve_merge", json={}) \
        .status_code == 401
    assert op_client.post(
        "/v1/actions/resolve_merge", json={},
        headers=bearer(f"tok-{uuid.uuid4().hex}")).status_code == 401
    assert op_client.get(
        "/v1/actions",
        headers={"Authorization": "Basic dXNlcg=="}).status_code == 401

    token = grant(resolver, surface, ["operator"])
    assert op_client.get("/v1/actions",
                         headers=bearer(token)).status_code == 200
    hvac.Client(url=settings.bao_addr, token=settings.bao_root_token) \
        .secrets.kv.v2.delete_metadata_and_all_versions(
            mount_point=settings.bao_kv_mount,
            path=OpenBaoCredentialResolver.path_for(token))
    assert op_client.get("/v1/actions",
                         headers=bearer(token)).status_code == 401


def test_no_op_can_mint_an_unscoped_or_identity_asserting_write(
        op_client, operator_app, resolver, surface):
    gate: OperatorGate = operator_app.gate

    # Identity-shaped params are unregistrable — tenant comes ONLY from the
    # resolved principal, structurally.
    with pytest.raises(WriteOperationRejected, match="identity"):
        gate.register(WriteOperation(
            name="evil_op", description="x", scope=["operator"],
            params={"tenant_id": WriteParamSpec(type="str")}),
            lambda principal, p: None)

    # An unscoped (everyone-may-write) op is unconstructable.
    with pytest.raises(Exception, match="at least 1"):
        WriteOperation(name="open_op", description="x", scope=[], params={})

    # And over HTTP, a body asserting a tenant is an unknown param.
    tok = grant(resolver, surface, ["reviewer"])
    r = op_client.post("/v1/actions/resolve_merge",
                       json={"candidate_id": 1, "same": True,
                             "tenant_id": "someone-else"},
                       headers=bearer(tok))
    assert r.status_code == 400
    assert "unknown param" in r.json()["detail"]

    # No raw-mutation surface exists anywhere in the URL space.
    for method, path in [("POST", "/v1/sql"), ("POST", "/v1/actions"),
                         ("POST", "/v1/actions/update_facts"),
                         ("GET", "/v1/actions/resolve_merge"),
                         ("POST", "/v1/actions/resolve_merge/raw")]:
        assert op_client.request(method, path, headers=bearer(tok)) \
            .status_code == 404, (method, path)


# ---------------------------------------------------------------------------
# Ingestion control + alerts + quarantine + flagged documents
# ---------------------------------------------------------------------------


def test_pause_source_actually_pauses_capture_and_resume_resumes(
        op_client, resolver, store, db, capture, tenant, tmp_path):
    (tmp_path / "sop.txt").write_text("Standard operating procedure text.",
                                      encoding="utf-8")
    ref = f"fs-{uuid.uuid4().hex[:8]}"
    tok = grant(resolver, tenant, ["operator"])

    r = op_client.post("/v1/actions/add_source",
                       json={"source_ref": ref,
                             "source_system": "filesystem"},
                       headers=bearer(tok))
    assert r.status_code == 200
    cred = r.json()["result"]["credential"]
    assert cred["vault_path"] == f"tenants/{tenant}/sources/{ref}"
    assert cred["present"] is False       # pointer + presence, NEVER a value

    adapter = FilesystemSourceAdapter(ref, root=tmp_path)

    # Paused -> capture SKIPS the source; nothing lands.
    assert op_client.post("/v1/actions/pause_source",
                          json={"source_ref": ref, "reason": "maintenance"},
                          headers=bearer(tok)).status_code == 200
    result = capture.run_source(tenant, adapter)
    assert result.status == "skipped"
    landed = db.execute(
        "SELECT count(*) AS n FROM raw_documents WHERE tenant_id = %s",
        (tenant,)).fetchone()["n"]
    assert landed == 0

    # Resumed -> the same run lands the file.
    assert op_client.post("/v1/actions/resume_source",
                          json={"source_ref": ref},
                          headers=bearer(tok)).status_code == 200
    result = capture.run_source(tenant, adapter)
    assert result.status == "ok"
    landed = db.execute(
        "SELECT count(*) AS n FROM raw_documents WHERE tenant_id = %s",
        (tenant,)).fetchone()["n"]
    assert landed == 1

    # Config guard: credential-shaped keys never enter the registry.
    r = op_client.post("/v1/actions/edit_scope",
                       json={"source_ref": ref,
                             "config": {"root": "/data", "api_token": "x"}},
                       headers=bearer(tok))
    assert r.status_code == 400
    assert "vault flow" in r.json()["detail"]


def test_acknowledge_alert_clears_it_and_retry_requeues(
        op_client, resolver, store, db, dispatcher, pipeline, tenant):
    raw = make_raw(tenant)
    pipeline.ingest_raw(raw)
    item_id = dispatcher.dispatch(tenant, raw.id)
    db.execute("UPDATE dispatch_queue SET status = 'error',"
               " last_error = 'poison document' WHERE id = %s", (item_id,))
    tok = grant(resolver, tenant, ["operator"])

    def alerts():
        return op_client.get("/v1/alerts",
                             headers=bearer(tok)).json()["alerts"]

    assert [(a["kind"], a["ref_id"]) for a in alerts()] == \
        [("dispatch", item_id)]

    r = op_client.post("/v1/actions/acknowledge_alert",
                       json={"kind": "dispatch", "item_id": item_id,
                             "note": "known poison, vendor ticket open"},
                       headers=bearer(tok))
    assert r.status_code == 200
    assert alerts() == []                                 # cleared
    row = db.execute("SELECT * FROM dispatch_queue WHERE id = %s",
                     (item_id,)).fetchone()
    assert row["acknowledged_by"].startswith("op-")

    # Double-ack is a conflict, not a silent overwrite.
    assert op_client.post("/v1/actions/acknowledge_alert",
                          json={"kind": "dispatch", "item_id": item_id},
                          headers=bearer(tok)).status_code == 409

    # Retry requeues and clears the ack (a fresh failure re-alerts).
    r = op_client.post("/v1/actions/retry_failed_item",
                       json={"queue": "dispatch", "item_id": item_id},
                       headers=bearer(tok))
    assert r.status_code == 200
    row = db.execute("SELECT * FROM dispatch_queue WHERE id = %s",
                     (item_id,)).fetchone()
    assert row["status"] == "queued" and row["acknowledged_at"] is None

    # A nonexistent target fails closed.
    assert op_client.post("/v1/actions/acknowledge_alert",
                          json={"kind": "dispatch", "item_id": 999999999},
                          headers=bearer(tok)).status_code == 404


def test_triage_quarantine_clears_queue_and_records_the_decision(
        op_client, resolver, db, tenant):
    qid = db.execute(
        """
        INSERT INTO quarantined_extractions
            (tenant_id, reason, detail, extractor, extractor_version,
             ontology_version)
        VALUES (%s, 'unbound_predicate', 'retained_for', 'test', 'bp19', %s)
        RETURNING id
        """, (tenant, ONTOLOGY)).fetchone()["id"]
    tok = grant(resolver, tenant, ["reviewer"])

    in_queue = db.execute(
        "SELECT * FROM review_queue WHERE tenant_id = %s AND kind ="
        " 'quarantine'", (tenant,)).fetchall()
    assert [r["ref_id"] for r in in_queue] == [qid]

    r = op_client.post("/v1/actions/triage_quarantine",
                       json={"quarantine_id": qid, "decision": "resolved",
                             "note": "ontology gap: retention periods"},
                       headers=bearer(tok))
    assert r.status_code == 200, r.text

    # Out of the review queue, decision recorded, flywheel label written.
    assert db.execute(
        "SELECT * FROM review_queue WHERE tenant_id = %s AND kind ="
        " 'quarantine'", (tenant,)).fetchall() == []
    row = db.execute("SELECT status FROM quarantined_extractions"
                     " WHERE id = %s", (qid,)).fetchone()
    assert row["status"] == "resolved"
    (label,) = db.execute(
        "SELECT * FROM labels WHERE tenant_id = %s", (tenant,)).fetchall()
    assert label["label_type"] == "correction"
    assert label["payload"]["quarantine_id"] == qid
    assert label["payload"]["note"] == "ontology gap: retention periods"

    # Re-triaging a closed item is a conflict.
    assert op_client.post("/v1/actions/triage_quarantine",
                          json={"quarantine_id": qid,
                                "decision": "dismissed"},
                          headers=bearer(tok)).status_code == 409


def test_resolve_flagged_document_adjudicates_and_requeues(
        op_client, resolver, store, db, dispatcher, pipeline, tenant):
    from knowledge_hub.models import Document, DocType

    raw = make_raw(tenant)
    pipeline.ingest_raw(raw)
    doc = Document(tenant_id=tenant, raw_document_id=raw.id,
                   doc_type=DocType.prose, title="Mistagged form")
    store.insert_document(doc)
    db.execute("UPDATE documents SET review_status = 'review',"
               " review_reason = 'declared prose, detected form'"
               " WHERE id = %s", (doc.id,))
    item_id = dispatcher.dispatch(tenant, raw.id)
    db.execute("UPDATE dispatch_queue SET status = 'done', acked_at = now()"
               " WHERE id = %s", (item_id,))
    tok = grant(resolver, tenant, ["reviewer"])

    assert [r["ref_id"] for r in db.execute(
        "SELECT * FROM review_queue WHERE tenant_id = %s AND kind ="
        " 'document'", (tenant,)).fetchall()] == [doc.id]

    r = op_client.post("/v1/actions/resolve_flagged_document",
                       json={"document_id": doc.id,
                             "corrected_data_track": "form"},
                       headers=bearer(tok))
    assert r.status_code == 200, r.text

    # Out of the review queue; the CLAIM corrected at its source; the raw
    # doc requeued so processing picks the adjudicated tag back up.
    assert db.execute(
        "SELECT review_status FROM documents WHERE id = %s",
        (doc.id,)).fetchone()["review_status"] == "resolved"
    assert db.execute(
        "SELECT native_metadata ->> 'data_track' AS t FROM raw_documents"
        " WHERE id = %s", (raw.id,)).fetchone()["t"] == "form"
    assert db.execute(
        "SELECT status FROM dispatch_queue WHERE id = %s",
        (item_id,)).fetchone()["status"] == "queued"


# ---------------------------------------------------------------------------
# The catalog surface is role-scoped and registry-generated
# ---------------------------------------------------------------------------


def test_action_catalog_is_role_scoped_and_registry_generated(
        op_client, operator_app, resolver, surface):
    operator_tok = grant(resolver, surface, ["operator"])
    reviewer_tok = grant(resolver, surface, ["reviewer"])

    all_actions = {a["name"] for a in op_client.get(
        "/v1/actions", headers=bearer(operator_tok)).json()["actions"]}
    review_actions = {a["name"] for a in op_client.get(
        "/v1/actions", headers=bearer(reviewer_tok)).json()["actions"]}

    assert review_actions == {"resolve_merge", "resolve_as_new",
                              "split_merge", "triage_quarantine",
                              "resolve_flagged_document"}
    assert all_actions == review_actions | {
        "pause_source", "resume_source", "retry_failed_item",
        "acknowledge_alert", "add_source", "edit_scope"}

    # The endpoint list IS the registry, spelled as URLs.
    generated = {e for e in operator_app.endpoints()
                 if e.startswith("POST /v1/actions/")}
    assert generated == {f"POST /v1/actions/{n}" for n in all_actions}

    # A reviewer calling an operator-scoped action: refused, not absent —
    # the write catalog is static and the refusal is the audit signal.
    assert op_client.post("/v1/actions/pause_source",
                          json={"source_ref": "x"},
                          headers=bearer(reviewer_tok)).status_code == 403
