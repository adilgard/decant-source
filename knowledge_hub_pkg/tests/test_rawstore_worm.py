"""RawStore against real SeaweedFS: write-once semantics, version-pinned
URIs, and the upstream #8350 verification — COMPLIANCE object-lock must
actually protect landed bytes on the DEPLOYED version, not just report
'Enabled'. These assertions are the proof; if a SeaweedFS upgrade regresses
enforcement, this file goes red."""
from __future__ import annotations

import uuid

import pytest
from botocore.exceptions import ClientError

from factories import make_raw
from knowledge_hub.rawstore_s3 import sha256_hex


def land(raw_store, tenant, content: bytes) -> str:
    return raw_store.put(tenant, content, meta={
        "content_hash": sha256_hex(content),
        "native_id": "worm/test.bin",
        "mime_type": "application/octet-stream",
    })


def unique_bytes() -> bytes:
    return b"landed object " + uuid.uuid4().hex.encode()


def test_put_get_roundtrip_version_pinned(raw_store, tenant):
    content = unique_bytes()
    uri = land(raw_store, tenant, content)

    assert uri.startswith(f"s3://kh-raw-test/{tenant}/")
    assert "versionId=" in uri  # pinned: later writes can't redirect this URI
    assert raw_store.get(uri) == content


def test_put_is_write_once_per_content(raw_store, tenant):
    content = unique_bytes()
    assert land(raw_store, tenant, content) == land(raw_store, tenant, content)


def test_put_rejects_mismatched_hash(raw_store, tenant):
    with pytest.raises(ValueError, match="content_hash"):
        raw_store.put(tenant, b"actual bytes",
                      meta={"content_hash": sha256_hex(b"different bytes")})


def test_8350_overwrite_of_landed_object_is_rejected(raw_store, tenant):
    """The #8350 verification. A landed object must survive every S3-level
    attempt to change or remove it while under COMPLIANCE retention:
      1. an overwrite attempt must not alter what the landed URI returns
         (S3 semantics: the PUT stacks a new version; the locked version is
         untouchable — verified, since lock-without-versioning silently
         enforces NOTHING on SeaweedFS);
      2. deleting the landed version outright must be rejected;
      3. an unversioned delete must not take the landed bytes away."""
    content = unique_bytes()
    uri = land(raw_store, tenant, content)
    bucket, key, version_id = raw_store._parse_uri(uri)
    s3 = raw_store._s3

    # 1. Overwrite attempt: the landed object is not clobbered.
    s3.put_object(Bucket=bucket, Key=key, Body=b"attacker overwrite")
    assert raw_store.get(uri) == content

    # 2. Version-specific delete of the landed version: rejected outright.
    with pytest.raises(ClientError) as excinfo:
        s3.delete_object(Bucket=bucket, Key=key, VersionId=version_id)
    assert excinfo.value.response["Error"]["Code"] == "AccessDenied"

    # 3. Unversioned delete (delete marker): landed bytes still retrievable.
    s3.delete_object(Bucket=bucket, Key=key)
    assert raw_store.get(uri) == content


def test_verify_worm_probe_reports_enforced(raw_store):
    report = raw_store.verify_worm()
    assert report["overwrite_protected"]
    assert report["delete_rejected"]
    assert report["survives_unversioned_delete"]


def test_exists_returns_landing_record(raw_store, pipeline, tenant):
    content = unique_bytes()
    content_hash = sha256_hex(content)
    assert raw_store.exists(tenant, content_hash) is None  # never landed

    uri = land(raw_store, tenant, content)
    raw = make_raw(tenant, content_hash=content_hash, raw_uri=uri)
    pipeline.ingest_raw(raw)

    found = raw_store.exists(tenant, content_hash)
    assert found is not None and found.id == raw.id
    assert found.raw_uri == uri and found.tenant_id == tenant
    # ...and it is tenant-scoped: another tenant never sees it.
    assert raw_store.exists(f"{tenant}-other", content_hash) is None
