"""d.s Stage 3: scoped, backgrounded, reversible re-extraction.

What must hold:

* ONTOLOGY SUPERSESSION (the retraction primitive's third trigger,
  deterministic — no LLM): when a document's facts promote under a NEW
  ontology version, the old version's facts retire in the SAME transaction
  with the SAME cutover timestamp, reason='ontology_superseded' — retained,
  never deleted; "what did this document yield under A vs B" stays
  answerable; a normal same-version promotion never trips it; a
  re-extraction that promotes NOTHING leaves the old facts current
  (promotion-gated — an empty new yield must not erase a served corpus);
* the scope machinery is one WHERE builder: the preview count the operator
  confirms IS the population the job materializes; materialization is
  idempotent and frozen (resume never re-scopes);
* reextract_scope refuses a scope-less call (a blanket run never happens
  by omission), contradictory scopes, unknown versions, and scope==target;
  the affected count rides the result; the call is audited; reviewer
  refused;
* END TO END on the real stack: ingest a folder under version A (Stage 2),
  swap the active selection to B, re-extract scope_version=A — every
  document's ledger carries ok-runs under BOTH versions, per-document
  progress lands done, no document ever serves two vocabularies at once,
  and re-running the same job replays everything as no-ops (the
  idempotency ledger keys on the version) with zero new fact rows.
"""
from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path

import pytest

from factories import ONTOLOGY, land_document, make_entity, make_raw

from knowledge_hub.models import DocType, Document, ExtractionRun, PendingFact
from knowledge_hub.operator_http import (
    OperatorGate,
    OperatorService,
    WriteCallError,
    WriteRefused,
    register_operator_defaults,
)
from knowledge_hub.pipeline import (
    RETRACTED_BY_EXTRACTOR,
    RETRACTED_BY_ONTOLOGY,
)
from knowledge_hub.serving import Principal

TEST_BUCKET = "kh-raw-test"


# ---------------------------------------------------------------- helpers --
def import_version(store, tenant: str) -> str:
    """A fresh ontology version cloned from the baseline definition (same
    vocabulary — only provenance moves)."""
    _, definition = store.get_ontology_definition(tenant, ONTOLOGY)
    version = f"rx-{uuid.uuid4().hex[:8]}"
    store.insert_ontology_version(tenant, version, definition)
    return version


def stage_fact(store, tenant: str, subject_id: int, literal: str, *,
               doc_id: int, version: str, extractor: str = "test",
               extractor_version: str = "rx") -> None:
    store.stage_pending({}, [PendingFact(
        tenant_id=tenant, subject_ref=f"entity:{subject_id}",
        predicate="owns", object_literal=literal, ontology_version=version,
        source_document_id=doc_id, extractor=extractor,
        extractor_version=extractor_version)])


def doc_facts(store, tenant: str, doc_id: int) -> list[dict]:
    with store.transaction(tenant) as conn:
        return conn.execute(
            "SELECT id, object_literal, ontology_version, valid_from,"
            " valid_to, retraction_reason, extractor, extractor_version"
            " FROM facts"
            " WHERE tenant_id = %s AND source_document_id = %s ORDER BY id",
            (tenant, doc_id)).fetchall()


@pytest.fixture()
def keep_baseline_active(store, tenant):
    yield
    store.set_active_ontology(tenant, ONTOLOGY)


# ---------------------------------------------------------------------------
# Ontology supersession — deterministic, through the REAL promotion path.
# ---------------------------------------------------------------------------

def test_ontology_supersession_retires_retains_and_is_atomic(
        pipeline, store, tenant):
    doc = land_document(pipeline, store, tenant)
    vendor = make_entity(tenant, "Granite Botanicals")
    store.upsert_entity(vendor)
    v2 = import_version(store, tenant)

    # Version A promotes two facts — the served corpus.
    stage_fact(store, tenant, vendor.id, "Net-30", doc_id=doc.id,
               version=ONTOLOGY)
    stage_fact(store, tenant, vendor.id, "Building A", doc_id=doc.id,
               version=ONTOLOGY)
    a_ids = pipeline.promote_pending(tenant)
    assert len(a_ids) == 2

    # A same-version wave must NOT trip the trigger (normal operation).
    stage_fact(store, tenant, vendor.id, "extra-A", doc_id=doc.id,
               version=ONTOLOGY)
    pipeline.promote_pending(tenant)
    rows = {r["id"]: r for r in doc_facts(store, tenant, doc.id)}
    assert all(r["valid_to"] is None for r in rows.values())

    # Version B promotes: A retires (retained + tagged), B is current, one
    # cutover — no window serves both vocabularies.
    stage_fact(store, tenant, vendor.id, "Net-45", doc_id=doc.id,
               version=v2)
    b_ids = pipeline.promote_pending(tenant)
    assert len(b_ids) == 1
    rows = doc_facts(store, tenant, doc.id)
    a_rows = [r for r in rows if r["ontology_version"] == ONTOLOGY]
    b_rows = [r for r in rows if r["ontology_version"] == v2]
    assert len(a_rows) == 3 and len(b_rows) == 1        # RETAINED, never deleted
    assert all(r["valid_to"] is not None and
               r["retraction_reason"] == RETRACTED_BY_ONTOLOGY
               for r in a_rows)
    assert all(r["valid_to"] is None for r in b_rows)
    # One shared cutover instant: B's validity begins exactly where A ends.
    cutovers = {r["valid_to"] for r in a_rows}
    assert len(cutovers) == 1
    assert b_rows[0]["valid_from"] == cutovers.pop()

    # "A vs B" stays answerable: version A's yield is three literals.
    assert {r["object_literal"] for r in a_rows} == \
        {"Net-30", "Building A", "extra-A"}


def test_extractor_supersession_retires_same_version_plugin_upgrade(
        pipeline, store, tenant):
    """The fourth trigger: a plugin upgrade re-yields under the SAME
    ontology version (the ledger keys on extractor_version, so it is fresh
    work, never a replay) — without extractor supersession the re-yield
    would promote BESIDE the old rows and the corpus would silently
    double. The old producer version's facts must retire exactly like an
    ontology cutover: retained, reason-tagged, one shared timestamp."""
    doc = land_document(pipeline, store, tenant)
    vendor = make_entity(tenant, "Alpine Extracts")
    store.upsert_entity(vendor)

    # Plugin 1.0's yield — the served corpus.
    stage_fact(store, tenant, vendor.id, "Net-30", doc_id=doc.id,
               version=ONTOLOGY, extractor_version="1.0")
    stage_fact(store, tenant, vendor.id, "Building A", doc_id=doc.id,
               version=ONTOLOGY, extractor_version="1.0")
    assert len(pipeline.promote_pending(tenant)) == 2

    # Plugin 1.1 re-yields under the SAME ontology version: 1.0 retires
    # (retained + tagged), 1.1 is current, one cutover.
    stage_fact(store, tenant, vendor.id, "Net-30", doc_id=doc.id,
               version=ONTOLOGY, extractor_version="1.1")
    assert len(pipeline.promote_pending(tenant)) == 1
    rows = doc_facts(store, tenant, doc.id)
    old = [r for r in rows if r["extractor_version"] == "1.0"]
    new = [r for r in rows if r["extractor_version"] == "1.1"]
    assert len(old) == 2 and len(new) == 1      # RETAINED, never deleted
    assert all(r["valid_to"] is not None and
               r["retraction_reason"] == RETRACTED_BY_EXTRACTOR
               for r in old)
    assert all(r["valid_to"] is None for r in new)
    # One shared cutover instant: 1.1's validity begins where 1.0 ends.
    cutovers = {r["valid_to"] for r in old}
    assert len(cutovers) == 1
    assert new[0]["valid_from"] == cutovers.pop()

    # Old-vs-new stays answerable: 1.0's yield is both literals.
    assert {r["object_literal"] for r in old} == {"Net-30", "Building A"}


def test_extractor_supersession_never_touches_other_producers(
        pipeline, store, tenant):
    """Same-producer on purpose: a different extractor NAME promoting
    under the same version is a strategy switch, not an upgrade — the
    incumbent's facts must stay served. And the incumbent re-promoting at
    its own unchanged version must trip nothing (normal operation)."""
    doc = land_document(pipeline, store, tenant)
    vendor = make_entity(tenant, "Basalt Trading")
    store.upsert_entity(vendor)

    stage_fact(store, tenant, vendor.id, "Net-30", doc_id=doc.id,
               version=ONTOLOGY, extractor="llm", extractor_version="1.0")
    pipeline.promote_pending(tenant)

    # A DIFFERENT producer promotes same-version: nothing retires.
    stage_fact(store, tenant, vendor.id, "Net-45", doc_id=doc.id,
               version=ONTOLOGY, extractor="parser", extractor_version="9.9")
    pipeline.promote_pending(tenant)
    rows = doc_facts(store, tenant, doc.id)
    assert all(r["valid_to"] is None for r in rows), \
        "a strategy switch must not silently retire the incumbent"

    # The same producer at the SAME version promotes again: still nothing.
    stage_fact(store, tenant, vendor.id, "extra", doc_id=doc.id,
               version=ONTOLOGY, extractor="llm", extractor_version="1.0")
    pipeline.promote_pending(tenant)
    rows = doc_facts(store, tenant, doc.id)
    assert all(r["valid_to"] is None for r in rows)


def test_supersession_is_promotion_gated(pipeline, store, tenant):
    """A re-extraction that promotes nothing leaves the old facts served —
    an empty new yield never silently erases a corpus."""
    doc = land_document(pipeline, store, tenant)
    vendor = make_entity(tenant, "Cascade Supply")
    store.upsert_entity(vendor)
    import_version(store, tenant)   # B exists, but nothing staged under it

    stage_fact(store, tenant, vendor.id, "Net-30", doc_id=doc.id,
               version=ONTOLOGY)
    pipeline.promote_pending(tenant)
    pipeline.promote_pending(tenant)   # idempotent re-run: nothing pending
    rows = doc_facts(store, tenant, doc.id)
    assert len(rows) == 1 and rows[0]["valid_to"] is None


# ---------------------------------------------------------------------------
# Scope machinery — one WHERE builder for preview and materialization.
# ---------------------------------------------------------------------------

def seed_extracted_doc(pipeline, store, tenant: str, *, version: str,
                       source_ref: str) -> Document:
    raw = make_raw(tenant, source_system="filesystem",
                   native_metadata={"source_ref": source_ref})
    pipeline.ingest_raw(raw)
    doc = Document(tenant_id=tenant, raw_document_id=raw.id,
                   doc_type=DocType.prose, title="scoped")
    store.insert_document(doc)
    store.insert_extraction_run(ExtractionRun(
        tenant_id=tenant, document_id=doc.id, unit_hash=uuid.uuid4().hex,
        strategy="llm_joint", extractor="test", extractor_version="rx",
        ontology_version=version))
    return doc


def test_scope_count_materialize_and_resume(pipeline, store, tenant):
    v_old = import_version(store, tenant)
    seed_extracted_doc(pipeline, store, tenant, version=v_old,
                       source_ref="src-a")
    seed_extracted_doc(pipeline, store, tenant, version=v_old,
                       source_ref="src-b")
    seed_extracted_doc(pipeline, store, tenant, version=ONTOLOGY,
                       source_ref="src-a")

    assert store.count_scope_documents(
        tenant, {"scope_version": v_old}) == 2
    assert store.count_scope_documents(
        tenant, {"scope_version": v_old, "source_ref": "src-a"}) == 1
    assert store.count_scope_documents(tenant, {}) == 3   # explicit blanket

    job_id = store.insert_job(tenant, "reextract_scope", {})
    assert store.materialize_job_documents(
        tenant, job_id, {"scope_version": v_old}) == 2
    pending = store.pending_job_documents(tenant, job_id)
    assert len(pending) == 2

    # Resume semantics: one done, re-materialize is a no-op (the scope was
    # frozen), and the pending scan yields exactly the unfinished one.
    store.mark_job_document(tenant, job_id, pending[0], status="done")
    assert store.materialize_job_documents(
        tenant, job_id, {"scope_version": v_old}) == 2    # unchanged
    assert store.pending_job_documents(tenant, job_id) == [pending[1]]
    counts = store.job_document_counts(tenant, job_id)
    assert counts == {"docs_pending": 1, "docs_done": 1, "docs_failed": 0}
    store.finish_job(tenant, job_id, status="done")


# ---------------------------------------------------------------------------
# The audited write op.
# ---------------------------------------------------------------------------

@pytest.fixture()
def gate_and_service(store):
    from knowledge_hub.capture import SourceRegistry
    service = OperatorService(store, resolution=None,
                              registry=SourceRegistry(store))
    gate = OperatorGate(store)
    register_operator_defaults(gate, service)
    return gate, service


def operator(tenant):
    return Principal(tenant_id=tenant, principal_id="op-test",
                     roles=["operator"])


def test_reextract_scope_refusals_are_specific(gate_and_service, store,
                                               tenant):
    gate, _ = gate_and_service
    op = operator(tenant)
    with pytest.raises(WriteCallError, match="never happens by default"):
        gate.execute("reextract_scope", {}, op)
    with pytest.raises(WriteCallError, match="not both"):
        gate.execute("reextract_scope",
                     {"scope_version": ONTOLOGY, "all_documents": True}, op)
    with pytest.raises(WriteCallError, match="not imported"):
        gate.execute("reextract_scope",
                     {"ontology_version": "typo-9.9",
                      "scope_version": ONTOLOGY}, op)
    with pytest.raises(WriteCallError, match="not a known ontology"):
        gate.execute("reextract_scope", {"scope_version": "typo-9.9"}, op)
    with pytest.raises(WriteCallError, match="already extracted under"):
        # active is baseline here, so scope==target.
        gate.execute("reextract_scope", {"scope_version": ONTOLOGY}, op)
    with pytest.raises(WriteRefused):
        gate.execute("reextract_scope", {"all_documents": True},
                     Principal(tenant_id=tenant, principal_id="rv",
                               roles=["reviewer"]))


def test_reextract_scope_freezes_target_and_counts(
        gate_and_service, pipeline, store, tenant, keep_baseline_active):
    gate, service = gate_and_service
    v_old = import_version(store, tenant)
    v_new = import_version(store, tenant)
    seed_extracted_doc(pipeline, store, tenant, version=v_old,
                       source_ref="src-x")
    store.set_active_ontology(tenant, v_new)

    preview = service.reextract_preview(tenant, {"scope_version": v_old})
    assert preview["affected_documents"] == 1

    out = gate.execute("reextract_scope", {"scope_version": v_old},
                       operator(tenant))
    r = out["result"]
    assert r["ontology_version"] == v_new       # active AT CREATION, frozen
    assert r["affected_documents"] == 1
    job = store.get_job(tenant, r["job_id"])
    assert job["kind"] == "reextract_scope"
    assert job["params"]["ontology_version"] == v_new
    # A later swap must not reach the frozen job params.
    store.set_active_ontology(tenant, ONTOLOGY)
    assert store.get_job(tenant, r["job_id"])["params"]["ontology_version"] \
        == v_new
    store.finish_job(tenant, r["job_id"], status="done")   # keep queue clean


# ---------------------------------------------------------------------------
# THE STAGE 3 GATE — end to end on the real stack.
# ---------------------------------------------------------------------------

def _versions_served(store, tenant: str, source_ref: str) -> dict:
    """Per document of a source: current + superseded versions."""
    with store.transaction(tenant) as conn:
        rows = conn.execute(
            """
            SELECT d.id AS doc_id, f.ontology_version,
                   f.valid_to IS NULL AS current, f.retraction_reason
            FROM raw_documents r
            JOIN documents d ON d.raw_document_id = r.id
                            AND d.tenant_id = r.tenant_id
            JOIN facts f ON f.tenant_id = r.tenant_id
                        AND (f.source_document_id = d.id
                             OR (f.source_document_id IS NULL
                                 AND f.source_chunk_id IN (
                                     SELECT id FROM chunks c
                                     WHERE c.tenant_id = r.tenant_id
                                       AND c.document_id = d.id)))
            WHERE r.tenant_id = %s
              AND r.native_metadata ->> 'source_ref' = %s
            """, (tenant, source_ref)).fetchall()
    by_doc: dict[int, list] = {}
    for r in rows:
        by_doc.setdefault(r["doc_id"], []).append(r)
    return by_doc


def test_reextract_gate_end_to_end(gate_and_service, store, tenant,
                                   test_dsn, tmp_path,
                                   keep_baseline_active):
    """The build prompt's demonstration: change ontology, re-extract a
    small scope, new facts under the new version, old retained + marked
    superseded — plus resumability/idempotency by re-running the job."""
    from knowledge_hub.operator_jobs import JobRunner

    gate, _ = gate_and_service
    op = operator(tenant)
    runner = JobRunner(dsn=test_dsn, s3_bucket=TEST_BUCKET,
                       s3_retention=timedelta(minutes=15))

    # 1. Ingest a small folder under the ACTIVE baseline (Stage 2 path).
    folder = tmp_path / "corpus"
    folder.mkdir()
    (folder / "sop.txt").write_text(
        "SOP-014 was authored by Dana Reyes. The QA Team owns SOP-014.",
        encoding="utf-8")
    out = gate.execute("ingest_folder", {"path": str(folder)}, op)
    source_ref = out["result"]["source_ref"]
    assert runner.run_pending() == 1
    ingest_job = store.get_job(tenant, out["result"]["job_id"])
    assert ingest_job["status"] == "done", ingest_job["error"]

    # 2. Swap: import v2 (same vocabulary), select it active.
    v2 = import_version(store, tenant)
    store.set_active_ontology(tenant, v2, activated_by="op-test")

    # 3. Re-extract the old version's documents, narrowed to this source.
    out = gate.execute("reextract_scope",
                       {"scope_version": ONTOLOGY,
                        "source_ref": source_ref}, op)
    assert out["result"]["affected_documents"] == 1
    assert runner.run_pending() == 1
    job = store.get_job(tenant, out["result"]["job_id"])
    assert job["status"] == "done", job["error"]
    c = job["counts"]
    assert c["docs_total"] == 1 and c["docs_done"] == 1 \
        and c["docs_failed"] == 0
    assert c["units_extracted"] >= 1          # fresh work under v2, no replay

    # The ledger carries ok-runs under BOTH versions — A-vs-B answerable.
    with store.transaction(tenant) as conn:
        versions = {r["ontology_version"] for r in conn.execute(
            """
            SELECT DISTINCT er.ontology_version
            FROM extraction_runs er
            JOIN documents d ON d.id = er.document_id
            JOIN raw_documents r ON r.id = d.raw_document_id
            WHERE er.tenant_id = %s AND er.status = 'ok'
              AND r.native_metadata ->> 'source_ref' = %s
            """, (tenant, source_ref)).fetchall()}
    assert versions == {ONTOLOGY, v2}

    # Serving invariants, deterministic over model-dependent yields: a
    # document never serves two vocabularies at once, and when the new
    # version's facts are current every old-version fact is retained with
    # the ontology_superseded tag (never deleted).
    for doc_id, rows in _versions_served(store, tenant, source_ref).items():
        current = {r["ontology_version"] for r in rows if r["current"]}
        assert len(current) <= 1, f"doc {doc_id} serves {current}"
        if current == {v2}:
            old = [r for r in rows if r["ontology_version"] == ONTOLOGY]
            assert old, "old facts must be RETAINED, not deleted"
            assert all(r["retraction_reason"] == RETRACTED_BY_ONTOLOGY
                       for r in old)

    # 4. Idempotency: an identical second job replays as no-ops — nothing
    # double-writes, fact rows unchanged.
    with store.transaction(tenant) as conn:
        facts_before = conn.execute(
            "SELECT count(*) AS n FROM facts WHERE tenant_id = %s",
            (tenant,)).fetchone()["n"]
    out2 = gate.execute("reextract_scope",
                        {"scope_version": ONTOLOGY,
                         "source_ref": source_ref}, op)
    assert runner.run_pending() == 1
    job2 = store.get_job(tenant, out2["result"]["job_id"])
    assert job2["status"] == "done", job2["error"]
    assert job2["counts"]["units_extracted"] == 0
    assert job2["counts"]["units_replayed"] >= 1
    with store.transaction(tenant) as conn:
        facts_after = conn.execute(
            "SELECT count(*) AS n FROM facts WHERE tenant_id = %s",
            (tenant,)).fetchone()["n"]
    assert facts_after == facts_before
