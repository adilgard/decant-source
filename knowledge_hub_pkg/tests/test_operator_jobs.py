"""d.s Stage 2: console folder ingest, end to end against the real stack.

What must hold:

* the adapter's scope options are deterministic and counted — recursion
  toggle, include/exclude globs (relative posix path, case-insensitive,
  exclude wins), and the eligible-extension filter that SKIPS AND COUNTS
  unknown types instead of failing the run;
* the job queue is plain Postgres state: insert -> claim (FOR UPDATE SKIP
  LOCKED) -> finish; a dead runner's 'running' rows requeue;
* ingest_folder validates server-side (absolute, exists, directory,
  readable; a typo'd ontology version fails at CREATION, not mid-run),
  resolves the ontology version NOW and freezes it into params, and is
  audited; reviewer role is refused;
* THE STAGE 2 GATE — the forward swap: ingest under the active version,
  swap the active selection, ingest again, and the new documents'
  extraction runs land under the NEW version while a job pinned to an
  explicit version extracts under ITS version regardless of the swap.
  Proven on extraction_runs (the idempotency ledger — written even when
  the model stages nothing, so the assertion is deterministic) and on the
  capture-time pin riding raw_documents.native_metadata.

The gate test drives the REAL pipeline (SeaweedFS WORM landing, Docling
parse, bge-m3 embeddings, live qwen extraction, resolution) — the same
bar as test_full_slice.
"""
from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path

import pytest

from factories import ONTOLOGY

from knowledge_hub.factstore_pg import PostgresFactStore
from knowledge_hub.models import SourceRegistryEntry
from knowledge_hub.operator_http import (
    OperatorGate,
    OperatorService,
    WriteCallError,
    WriteRefused,
    register_operator_defaults,
)
from knowledge_hub.serving import Principal
from knowledge_hub.sources_fs import ELIGIBLE_EXTENSIONS, FilesystemSourceAdapter

TEST_BUCKET = "kh-raw-test"


# ---------------------------------------------------------------------------
# Adapter scope options — pure filesystem, no DB.
# ---------------------------------------------------------------------------

@pytest.fixture()
def tree(tmp_path: Path) -> Path:
    (tmp_path / "a.txt").write_text("alpha", encoding="utf-8")
    (tmp_path / "b.bin").write_bytes(b"\x00\x01")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "c.md").write_text("# gamma", encoding="utf-8")
    (tmp_path / "sub" / "drafts").mkdir()
    (tmp_path / "sub" / "drafts" / "d.txt").write_text("delta",
                                                       encoding="utf-8")
    return tmp_path


def scan_ids(adapter) -> list[str]:
    return [item.native_id for item in adapter.backfill("t")]


def test_adapter_defaults_recurse_and_do_not_filter(tree):
    ids = scan_ids(FilesystemSourceAdapter("s", tree))
    assert set(ids) == {"a.txt", "b.bin", "sub/c.md", "sub/drafts/d.txt"}


def test_adapter_recurse_off_stays_at_the_root(tree):
    ids = scan_ids(FilesystemSourceAdapter("s", tree, recurse=False))
    assert set(ids) == {"a.txt", "b.bin"}


def test_adapter_globs_match_relative_path_and_exclude_wins(tree):
    adapter = FilesystemSourceAdapter(
        "s", tree, include=["*.txt"], exclude=["sub/drafts/*"])
    assert set(scan_ids(adapter)) == {"a.txt"}
    assert adapter.excluded_by_glob == 3   # b.bin, c.md (include), d.txt (exclude)


def test_adapter_extension_filter_skips_and_counts_never_fatal(tree):
    adapter = FilesystemSourceAdapter("s", tree,
                                      extensions=ELIGIBLE_EXTENSIONS)
    assert set(scan_ids(adapter)) == {"a.txt", "sub/c.md",
                                      "sub/drafts/d.txt"}
    assert adapter.skipped_unknown == ["b.bin"]
    assert adapter.stats()["skipped_unknown"] == 1


def test_adapter_extra_metadata_rides_every_item(tree):
    adapter = FilesystemSourceAdapter(
        "s", tree, extensions=ELIGIBLE_EXTENSIONS,
        extra_metadata={"ontology_version_override": "pin-1.0"})
    for item in adapter.backfill("t"):
        assert item.native_metadata["ontology_version_override"] == "pin-1.0"
        assert item.native_metadata["source_ref"] == "s"  # base keys intact


def test_adapter_for_honors_registry_scope_config(tree):
    from knowledge_hub.deploy_launch import adapter_for
    entry = SourceRegistryEntry(
        tenant_id="t", source_ref="s", source_system="filesystem",
        config={"root": str(tree), "recurse": False, "include": ["*.txt"]})
    adapter, why = adapter_for(entry)
    assert why is None
    assert adapter.recurse is False and adapter.include == ["*.txt"]


# ---------------------------------------------------------------------------
# The job queue — plain Postgres state.
# ---------------------------------------------------------------------------

def test_job_queue_lifecycle(store, tenant):
    job_id = store.insert_job(tenant, "folder_ingest", {"path": "/x"},
                              created_by="op-test")
    claimed = store.claim_next_job()
    assert claimed["id"] == job_id and claimed["status"] == "running"
    assert claimed["params"] == {"path": "/x"}
    store.update_job_counts(tenant, job_id, {"files_landed": 3})
    store.finish_job(tenant, job_id, status="done",
                     counts={"files_landed": 3})
    row = store.get_job(tenant, job_id)
    assert row["status"] == "done" and row["counts"]["files_landed"] == 3
    assert row["finished_at"] is not None
    assert store.list_jobs(tenant)[0]["id"] == job_id


def test_stale_running_jobs_requeue(store, tenant):
    job_id = store.insert_job(tenant, "folder_ingest", {})
    assert store.claim_next_job()["id"] == job_id     # now 'running'
    assert store.requeue_stale_jobs() >= 1            # dead-runner recovery
    assert store.get_job(tenant, job_id)["status"] == "queued"
    # Drain it so later claim-based tests aren't handed this leftover.
    store.finish_job(tenant, store.claim_next_job()["id"], status="done")


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


def test_ingest_folder_validates_server_side(gate_and_service, tenant,
                                             tmp_path):
    gate, _ = gate_and_service
    op = operator(tenant)
    for bad_path, why in [
        ("relative/folder", "ABSOLUTE"),
        (str(tmp_path / "missing"), "does not exist"),
    ]:
        with pytest.raises(WriteCallError, match=why):
            gate.execute("ingest_folder", {"path": bad_path}, op)
    file_not_dir = tmp_path / "f.txt"
    file_not_dir.write_text("x", encoding="utf-8")
    with pytest.raises(WriteCallError, match="not a directory"):
        gate.execute("ingest_folder", {"path": str(file_not_dir)}, op)
    with pytest.raises(WriteCallError, match="not imported"):
        gate.execute("ingest_folder", {"path": str(tmp_path),
                                       "ontology_version": "typo-9.9"}, op)


def test_an_elided_path_says_so_instead_of_just_missing(gate_and_service,
                                                        store, tenant,
                                                        tmp_path):
    """A path with an ellipsis in it was abbreviated for display somewhere and
    then pasted. 'Does not exist' is true and useless: in a 148-character path
    the one wrong character is invisible, so the operator goes looking for a
    missing folder that is actually sitting right there. Real failure, first
    full-title attempt, 2026-08-04."""
    gate, _ = gate_and_service
    op = operator(tenant)
    deep = tmp_path / "a" / "b" / "target"
    deep.mkdir(parents=True)
    for elided in (str(tmp_path / "a" / "…" / "target"),
                   str(tmp_path / "a" / "..." / "target")):
        with pytest.raises(WriteCallError, match="shortened for display"):
            gate.execute("ingest_folder", {"path": elided}, op)
    # The unabbreviated one is accepted, so the guard cannot be swallowing
    # a path that would have worked.
    out = gate.execute("ingest_folder", {"path": str(deep)}, op)
    assert out["result"]["path"] == str(deep.resolve())
    # ...and close it out. claim_next_job takes the oldest QUEUED row of any
    # tenant, so a job left queued here is a job some later test's runner
    # claims instead of its own.
    store.finish_job(tenant, out["result"]["job_id"], status="done", counts={})


def test_cancelling_a_queued_job_stops_it_before_it_runs(gate_and_service,
                                                         store, tenant,
                                                         tmp_path):
    """Nothing has claimed it, so there is nobody to cooperate with: it is
    finished on the spot and the runner never sees it."""
    gate, _ = gate_and_service
    op = operator(tenant)
    out = gate.execute("ingest_folder", {"path": str(tmp_path)}, op)
    job_id = out["result"]["job_id"]

    killed = gate.execute("cancel_job", {"job_id": job_id,
                                         "reason": "wrong folder"}, op)
    assert killed["result"]["was"] == "queued"
    assert killed["result"]["stopped"] == "now"

    job = store.get_job(tenant, job_id)
    assert job["status"] == "failed"
    assert "wrong folder" in job["error"]
    assert job["cancel_requested_at"] is not None
    assert job["cancel_requested_by"] == op.principal_id
    # And it is gone from the queue, so no runner can pick it up.
    assert store.claim_next_job() is None


def test_cancelling_a_finished_job_is_refused(gate_and_service, store, tenant,
                                              tmp_path):
    """A control that pretends to stop something already over is worse than
    no control: the honest answer is that undoing it is a different action."""
    gate, _ = gate_and_service
    op = operator(tenant)
    out = gate.execute("ingest_folder", {"path": str(tmp_path)}, op)
    job_id = out["result"]["job_id"]
    store.finish_job(tenant, job_id, status="done", counts={})

    with pytest.raises(WriteCallError, match="already finished"):
        gate.execute("cancel_job", {"job_id": job_id}, op)


def test_cancel_is_idempotent_and_keeps_the_first_asks_timestamp(
        gate_and_service, store, tenant, tmp_path):
    """The first ask is when the operator decided, and that is the moment the
    audit trail should carry."""
    gate, _ = gate_and_service
    op = operator(tenant)
    job_id = gate.execute("ingest_folder", {"path": str(tmp_path)},
                          op)["result"]["job_id"]
    store.request_job_cancel(tenant, job_id, requested_by="first-asker")
    first = store.get_job(tenant, job_id)["cancel_requested_at"]
    store.request_job_cancel(tenant, job_id, requested_by="second-asker")
    again = store.get_job(tenant, job_id)
    assert again["cancel_requested_at"] == first
    assert again["cancel_requested_by"] == "first-asker"
    store.finish_job(tenant, job_id, status="failed", counts={})


def test_cancelling_an_unknown_job_is_a_lookup_error(gate_and_service, tenant):
    gate, _ = gate_and_service
    with pytest.raises(LookupError, match="not found"):
        gate.execute("cancel_job", {"job_id": 99999999}, operator(tenant))


def test_a_running_job_stops_at_a_pass_boundary_and_keeps_its_work(
        gate_and_service, store, test_dsn, tenant, tmp_path):
    """The whole point of cooperative cancellation: the run stops, the counters
    survive, and the queue is left in a state a later job can drain.

    The flag is set through the STORE rather than the write op, because the op
    short-circuits a queued job to finished and the runner would then never see
    it — that shortcut is covered separately. Setting the flag and then letting
    the runner claim it puts the job in exactly the state a mid-run click
    produces (running, flag set) with no race to lose.
    """
    from knowledge_hub.operator_jobs import JobRunner

    gate, _ = gate_and_service
    op = operator(tenant)
    (tmp_path / "a.md").write_text("# One\n\nSome prose.\n", encoding="utf-8")
    (tmp_path / "b.md").write_text("# Two\n\nMore prose.\n", encoding="utf-8")
    job_id = gate.execute("ingest_folder", {"path": str(tmp_path)},
                          op)["result"]["job_id"]

    store.request_job_cancel(tenant, job_id, requested_by="mid-run-clicker")
    assert store.get_job(tenant, job_id)["status"] == "queued"

    # dsn + bucket explicitly: a bare JobRunner() reads settings and would
    # point at the PILOT database and the long-retention bucket.
    runner = JobRunner(dsn=test_dsn, s3_bucket=TEST_BUCKET,
                       s3_retention=timedelta(minutes=15))
    assert runner.run_pending() == 1             # it DID run, then stopped

    job = store.get_job(tenant, job_id)
    assert job["status"] == "failed"
    assert "cancelled by the operator" in job["error"]
    assert "drain pass" in job["error"]
    # The counters from the last completed pass survived — not a traceback.
    assert job["counts"]["cancelled"] is True
    assert job["counts"]["files_landed"] == 2
    assert "Traceback" not in (job["error"] or "")
    # And capture's work stands: the files are landed, so a later job replays
    # them as duplicates instead of re-ingesting.
    with store.transaction(tenant) as conn:
        landed = conn.execute(
            "SELECT count(*) AS n FROM raw_documents WHERE tenant_id = %s",
            (tenant,)).fetchone()["n"]
    assert landed == 2


def test_eligible_extensions_are_per_job_not_global(gate_and_service, store,
                                                    tenant, tmp_path):
    """A folder holding an unusual format widens ITS OWN eligibility.

    The eligible-suffix set has exactly one reader (console folder ingest),
    so widening the shipped constant would change what every existing
    folder job ingests — files skipped-and-counted today would start
    landing and then fail in a parser never meant to read them. Per job,
    the blast radius is the one folder that asked."""
    from knowledge_hub.sources_fs import ELIGIBLE_EXTENSIONS

    gate, _ = gate_and_service
    out = gate.execute("ingest_folder",
                       {"path": str(tmp_path), "extensions": "XML, .Md, xml"},
                       operator(tenant))
    job = store.get_job(tenant, out["result"]["job_id"])
    # Normalized: dotted, lowercased, deduped, sorted — an operator typing a
    # file extension should not have to guess the spelling.
    assert job["params"]["extensions"] == [".md", ".xml"]
    # And the shipped default is untouched by any of it.
    assert ".xml" not in ELIGIBLE_EXTENSIONS
    store.finish_job(tenant, out["result"]["job_id"], status="done",
                     counts={})


def test_extensions_field_refuses_globs(gate_and_service, tenant, tmp_path):
    """It takes suffixes, not patterns. Accepting '*.xml' here would filter
    nothing and look like it worked."""
    gate, _ = gate_and_service
    with pytest.raises(WriteCallError, match="not a file suffix"):
        gate.execute("ingest_folder",
                     {"path": str(tmp_path), "extensions": "*.xml"},
                     operator(tenant))


def test_ingest_folder_freezes_params_and_is_audited(gate_and_service,
                                                     store, tenant,
                                                     tmp_path):
    gate, _ = gate_and_service
    out = gate.execute("ingest_folder",
                       {"path": str(tmp_path), "recurse": False,
                        "include": "*.md, reports/*", "exclude": " "},
                       operator(tenant))
    r = out["result"]
    job = store.get_job(tenant, r["job_id"])
    assert job["status"] == "queued" and job["kind"] == "folder_ingest"
    p = job["params"]
    assert p["ontology_version"] == ONTOLOGY        # resolved NOW, frozen
    assert p["recurse"] is False
    assert p["include"] == ["*.md", "reports/*"]    # comma-string -> list
    assert p["exclude"] is None                     # blank -> no filter
    assert p["extensions"] is None                  # unset -> shipped default
    assert p["source_ref"].startswith("folder-")    # stable per path
    again = gate.execute("ingest_folder", {"path": str(tmp_path)},
                         operator(tenant))
    assert again["result"]["source_ref"] == p["source_ref"]
    with store.transaction(tenant) as conn:
        audits = conn.execute(
            "SELECT outcome FROM operator_audit"
            " WHERE tenant_id = %s AND action = 'ingest_folder'",
            (tenant,)).fetchall()
    assert [a["outcome"] for a in audits] == ["applied", "applied"]
    # Leave no queued leftovers for the runner-based tests.
    for jid in (r["job_id"], again["result"]["job_id"]):
        store.finish_job(tenant, jid, status="done")


def test_ingest_folder_refused_for_reviewer(gate_and_service, tenant,
                                            tmp_path):
    gate, _ = gate_and_service
    reviewer = Principal(tenant_id=tenant, principal_id="rv-test",
                         roles=["reviewer"])
    with pytest.raises(WriteRefused):
        gate.execute("ingest_folder", {"path": str(tmp_path)}, reviewer)


# ---------------------------------------------------------------------------
# THE STAGE 2 GATE — runner end to end + the forward ontology swap.
# ---------------------------------------------------------------------------

@pytest.fixture()
def keep_baseline_active(store, tenant):
    yield
    store.set_active_ontology(tenant, ONTOLOGY)


def _job_runs(store, tenant, source_ref):
    """(ontology_version values on extraction_runs, raw override stamps)
    for every document a source landed."""
    with store.transaction(tenant) as conn:
        rows = conn.execute(
            """
            SELECT r.native_metadata ->> 'ontology_version_override' AS pin,
                   er.ontology_version
            FROM raw_documents r
            JOIN documents d ON d.raw_document_id = r.id
                            AND d.tenant_id = r.tenant_id
            JOIN extraction_runs er ON er.document_id = d.id
                                   AND er.tenant_id = r.tenant_id
            WHERE r.tenant_id = %s
              AND r.native_metadata ->> 'source_ref' = %s
            """, (tenant, source_ref)).fetchall()
    return rows


def test_forward_swap_end_to_end(gate_and_service, store, tenant, test_dsn,
                                 tmp_path, keep_baseline_active):
    """change active ontology -> ingest new folder -> facts land under the
    selected version (the Stage 2 exit condition), plus the explicit-pin
    variant Stage 3 rides on."""
    from knowledge_hub.operator_jobs import JobRunner

    gate, _ = gate_and_service
    op = operator(tenant)
    runner = JobRunner(dsn=test_dsn, s3_bucket=TEST_BUCKET,
                       s3_retention=timedelta(minutes=15))

    # A second vocabulary, identical CONTENT under a new version string, so
    # the extractor's behavior is unchanged and only provenance moves.
    _, baseline_def = store.get_ontology_definition(tenant, ONTOLOGY)
    v2 = f"swap-{uuid.uuid4().hex[:8]}"
    store.insert_ontology_version(tenant, v2, baseline_def)

    def make_folder(name: str, text: str, with_junk: bool = False) -> Path:
        folder = tmp_path / name
        folder.mkdir()
        (folder / "doc.txt").write_text(text, encoding="utf-8")
        if with_junk:
            (folder / "junk.zzz").write_bytes(b"\x00")
        return folder

    def run_job(folder: Path, **extra) -> dict:
        out = gate.execute("ingest_folder",
                           {"path": str(folder), **extra}, op)
        assert runner.run_pending() == 1
        job = store.get_job(tenant, out["result"]["job_id"])
        assert job["status"] == "done", job["error"]
        return job

    # 1. Ingest under the ACTIVE baseline.
    job_a = run_job(make_folder(
        "before-swap",
        "SOP-014 was authored by Dana Reyes. The QA Team owns SOP-014.",
        with_junk=True))
    assert job_a["params"]["ontology_version"] == ONTOLOGY
    assert job_a["counts"]["files_landed"] == 1
    assert job_a["counts"]["skipped_unknown"] == 1      # junk.zzz, not fatal
    runs_a = _job_runs(store, tenant, job_a["params"]["source_ref"])
    assert runs_a and all(r["pin"] == ONTOLOGY and
                          r["ontology_version"] == ONTOLOGY
                          for r in runs_a)

    # 2. THE SWAP: select v2, ingest a NEW folder — everything lands under v2.
    store.set_active_ontology(tenant, v2, activated_by="op-test")
    job_b = run_job(make_folder(
        "after-swap",
        "The cleaning log references SOP-014. Building A houses mixer M-3."))
    assert job_b["params"]["ontology_version"] == v2    # resolved at creation
    runs_b = _job_runs(store, tenant, job_b["params"]["source_ref"])
    assert runs_b and all(r["pin"] == v2 and r["ontology_version"] == v2
                          for r in runs_b)

    # 3. Explicit pin BEATS the active selection (the Stage 3 property).
    job_c = run_job(make_folder(
        "pinned",
        "Dana Reyes participated in the quarterly audit."),
        ontology_version=ONTOLOGY)                      # v2 is active
    runs_c = _job_runs(store, tenant, job_c["params"]["source_ref"])
    assert runs_c and all(r["pin"] == ONTOLOGY and
                          r["ontology_version"] == ONTOLOGY
                          for r in runs_c)

    # Staged facts carry the same provenance the ledger shows (when the
    # model staged any — the ledger assertions above are the deterministic
    # core; this one documents where facts inherit it from).
    with store.transaction(tenant) as conn:
        staged = conn.execute(
            "SELECT DISTINCT ontology_version FROM pending_facts"
            " WHERE tenant_id = %s", (tenant,)).fetchall()
    assert {s["ontology_version"] for s in staged} <= {ONTOLOGY, v2}
