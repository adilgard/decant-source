"""Raw landing: version increments per (tenant, source_system, native_id);
idempotent by (tenant, content_hash)."""
from __future__ import annotations

from factories import make_raw, sha


def test_version_increments_on_new_bytes(pipeline, store, tenant):
    native = "CRM-CONTRACT-77"
    v1 = make_raw(tenant, source_native_id=native, content_hash=sha("bytes v1"))
    v2 = make_raw(tenant, source_native_id=native, content_hash=sha("bytes v2"))

    id1 = pipeline.ingest_raw(v1)
    id2 = pipeline.ingest_raw(v2)

    assert v1.version == 1
    assert v2.version == 2
    assert id1 != id2
    assert store.get_raw_document(tenant, id2).version == 2


def test_same_bytes_are_a_noop(pipeline, store, tenant):
    native = "CRM-CONTRACT-88"
    raw = make_raw(tenant, source_native_id=native, content_hash=sha("same bytes"))
    id1 = pipeline.ingest_raw(raw)

    replay = make_raw(tenant, source_native_id=native, content_hash=sha("same bytes"))
    id2 = pipeline.ingest_raw(replay)

    assert id2 == id1  # no new row
    with store.transaction(tenant) as conn:
        n = conn.execute(
            "SELECT count(*) AS n FROM raw_documents"
            " WHERE tenant_id = %s AND source_native_id = %s",
            (tenant, native)).fetchone()["n"]
    assert n == 1


def test_version_sequences_are_per_tenant_and_per_source(pipeline, tenant):
    other_tenant = f"{tenant}-b"
    pipeline.ingest_raw(make_raw(tenant, source_native_id="X", content_hash=sha()))

    r_other_tenant = make_raw(other_tenant, source_native_id="X", content_hash=sha())
    pipeline.ingest_raw(r_other_tenant)
    assert r_other_tenant.version == 1  # tenant B's 'X' is its own sequence

    r_other_source = make_raw(tenant, source_system="gmail",
                              source_native_id="X", content_hash=sha())
    pipeline.ingest_raw(r_other_source)
    assert r_other_source.version == 1  # gmail's 'X' != sharepoint's 'X'
