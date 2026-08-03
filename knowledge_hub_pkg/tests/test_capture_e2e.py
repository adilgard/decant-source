"""Capture path end-to-end, against the real stack: a file in a watched
folder lands — immutable, hashed, tenant-scoped — as a real raw_documents
row, and dispatch fires. Idempotency and versioning flow through Prompt 1's
_persist_raw/_next_version, never around them."""
from __future__ import annotations

import hashlib
import os
import time

import pytest

from knowledge_hub.sources_fs import FilesystemSourceAdapter


def write_file(root, rel, content: bytes, mtime: float | None = None):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


@pytest.fixture()
def watched(tmp_path):
    """A watched folder with three files at distinct, stable mtimes."""
    base = time.time() - 3600
    write_file(tmp_path, "contracts/acme.txt", b"ACME master services agreement",
               mtime=base)
    write_file(tmp_path, "contracts/globex.txt", b"Globex renewal terms",
               mtime=base + 10)
    write_file(tmp_path, "notes.md", b"# capture pilot notes", mtime=base + 20)
    return tmp_path


def adapter_for(watched, ref="fs-pilot"):
    return FilesystemSourceAdapter(source_ref=ref, root=watched)


def queue_rows(store, tenant):
    with store.transaction(tenant) as conn:
        return conn.execute(
            "SELECT * FROM dispatch_queue WHERE tenant_id = %s ORDER BY id",
            (tenant,)).fetchall()


def raw_rows(store, tenant):
    with store.transaction(tenant) as conn:
        return conn.execute(
            "SELECT * FROM raw_documents WHERE tenant_id = %s ORDER BY id",
            (tenant,)).fetchall()


def test_file_lands_end_to_end(capture, store, raw_store, tenant, watched):
    result = capture.run_source(tenant, adapter_for(watched))

    assert result.status == "ok" and result.mode == "backfill"
    assert result.landed == 3 and result.replayed == 0 and result.dispatched == 3

    rows = raw_rows(store, tenant)
    assert len(rows) == 3
    by_native = {r["source_native_id"]: r for r in rows}
    acme = by_native["contracts/acme.txt"]

    # The row is tenant-scoped, hashed, versioned, ACL-carrying, and points
    # at a version-pinned object URI.
    assert acme["tenant_id"] == tenant
    assert acme["content_hash"] == hashlib.sha256(
        b"ACME master services agreement").hexdigest()
    assert acme["version"] == 1
    assert acme["status"] == "landed"
    assert acme["source_system"] == "filesystem"
    assert acme["source_acl"]["model"] == "posix.v1"
    assert acme["source_acl"]["raw"]["mode"].startswith("0o")
    assert isinstance(acme["source_acl"]["grants"], list)
    assert acme["native_metadata"]["absolute_path"].endswith("acme.txt")
    assert acme["raw_uri"].startswith("s3://") and "versionId=" in acme["raw_uri"]

    # Raw bytes stored faithfully, untransformed.
    assert raw_store.get(acme["raw_uri"]) == b"ACME master services agreement"

    # Dispatch fired: one reference-only message per landed doc.
    messages = queue_rows(store, tenant)
    assert {m["raw_document_id"] for m in messages} == {r["id"] for r in rows}
    assert all(m["status"] == "queued" for m in messages)

    # Registry recorded the completed backfill.
    entry = capture.registry.get(tenant, "fs-pilot")
    assert entry.backfill_done and entry.backfill_cursor is None
    assert entry.cursor is not None and entry.last_run_at is not None


def test_relanding_identical_bytes_is_noop(capture, store, tenant, watched):
    capture.run_source(tenant, adapter_for(watched))
    before = raw_rows(store, tenant)

    # Fresh adapter, same folder, forced full re-pull: same bytes everywhere.
    result = capture.run_source(tenant, adapter_for(watched), mode="backfill")

    assert result.landed == 0 and result.replayed == 3
    after = raw_rows(store, tenant)
    assert [r["id"] for r in after] == [r["id"] for r in before]  # no new rows
    assert len(queue_rows(store, tenant)) == 3  # dispatch idempotent too


def test_changed_bytes_increment_version(capture, store, tenant, watched):
    capture.run_source(tenant, adapter_for(watched))

    write_file(watched, "contracts/acme.txt", b"ACME MSA v2 - renegotiated",
               mtime=time.time() + 5)
    result = capture.run_source(tenant, adapter_for(watched))  # auto -> incremental

    assert result.mode == "incremental"
    assert result.landed == 1  # only the changed file came back past the cursor
    versions = {
        (r["source_native_id"], r["version"]) for r in raw_rows(store, tenant)
        if r["source_native_id"] == "contracts/acme.txt"
    }
    assert versions == {("contracts/acme.txt", 1), ("contracts/acme.txt", 2)}


def test_incremental_picks_up_only_new_files(capture, store, tenant, watched):
    capture.run_source(tenant, adapter_for(watched))
    write_file(watched, "contracts/initech.txt", b"Initech pilot order",
               mtime=time.time() + 5)

    result = capture.run_source(tenant, adapter_for(watched))

    assert result.mode == "incremental" and result.landed == 1
    natives = {r["source_native_id"] for r in raw_rows(store, tenant)}
    assert "contracts/initech.txt" in natives and len(natives) == 4


class FlakyDispatcher:
    """Fails the Nth dispatch once — simulates dying partway through a pull."""

    def __init__(self, inner, fail_on_call: int):
        self.inner, self.fail_on, self.calls = inner, fail_on_call, 0

    def dispatch(self, tenant_id, raw_document_id):
        self.calls += 1
        if self.calls == self.fail_on:
            self.fail_on = -1  # only once
            raise ConnectionError("simulated crash mid-pull")
        return self.inner.dispatch(tenant_id, raw_document_id)


def test_interrupted_backfill_resumes_without_duplicates(
        capture, store, tenant, watched):
    capture.dispatcher = FlakyDispatcher(capture.dispatcher, fail_on_call=2)

    with pytest.raises(ConnectionError):
        capture.run_source(tenant, adapter_for(watched))

    entry = capture.registry.get(tenant, "fs-pilot")
    assert not entry.backfill_done
    assert entry.backfill_cursor is not None  # item 1 checkpointed
    assert entry.status_reason.startswith("interrupted:")
    assert len(raw_rows(store, tenant)) >= 1

    # Re-run resumes: everything lands exactly once, dispatch complete.
    result = capture.run_source(tenant, adapter_for(watched))
    assert result.status == "ok"
    rows = raw_rows(store, tenant)
    assert len(rows) == 3
    assert len({r["content_hash"] for r in rows}) == 3  # no duplicate rows
    assert {m["raw_document_id"] for m in queue_rows(store, tenant)} == \
        {r["id"] for r in rows}
    assert capture.registry.get(tenant, "fs-pilot").backfill_done


def test_disabled_source_is_skipped(capture, tenant, watched):
    adapter = adapter_for(watched)
    capture.registry.register(tenant, adapter.source_ref, adapter.source_system)
    capture.registry.set_status(tenant, adapter.source_ref, "disabled", "paused")

    result = capture.run_source(tenant, adapter)

    assert result.status == "skipped" and result.landed == 0
