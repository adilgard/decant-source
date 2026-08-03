"""S3RawStore — boto3 implementation of RawStore against the SeaweedFS S3
gateway (works on any S3-compatible store).

Layout: one bucket, per-tenant prefix, content-addressed keys:

    s3://<bucket>/<tenant_id>/<hash[:2]>/<content_hash>?versionId=<vid>

`_bucket_for(tenant_id)` is the bucket-per-tenant swap point, mirroring
PostgresFactStore._dsn_for — nothing above it changes.

WORM, verified not assumed (upstream SeaweedFS #8350, reproduced live on the
deployed 4.40 build): creating a bucket with ObjectLockEnabledForBucket=True
makes it REPORT object-lock 'Enabled' and store retention metadata — while
enforcing nothing (overwrite and delete both succeed). Enforcement only
becomes real once bucket VERSIONING is explicitly enabled; then S3 semantics
apply: a plain PUT to a locked key is never rejected — it stacks a new
version — and protection means the locked VERSION cannot be deleted
(AccessDenied) and stays readable. Consequences here:

  * _ensure_bucket enables versioning explicitly and verifies both flags,
    refusing to start against a store that can't hold them;
  * put() stamps COMPLIANCE retention on every object and returns a
    version-PINNED URI, so a landed object stays retrievable even if
    something later writes over its key;
  * verify_worm() probes actual enforcement with a sacrificial object —
    run by check_stack.py and the test suite on every deployment.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional
from urllib.parse import parse_qs, quote, urlparse

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from knowledge_hub.config import settings
from knowledge_hub.factstore_pg import PostgresFactStore
from knowledge_hub.interfaces import RawStore
from knowledge_hub.models import RawDocument


def sha256_hex(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


class WormNotEnforcedError(RuntimeError):
    """The deployed object store accepted lock configuration but does not
    actually enforce it (the #8350 failure mode)."""


class S3RawStore(RawStore):
    def __init__(
        self,
        store: PostgresFactStore,
        bucket: Optional[str] = None,
        retention: Optional[timedelta] = None,
        s3_client: Any = None,
    ):
        # The DB is the authority on WHAT has landed (raw_documents rows);
        # the object store holds the bytes. exists() answers from the DB.
        self._store = store
        self._bucket = bucket or settings.s3_raw_bucket
        self._retention = retention if retention is not None else timedelta(
            days=settings.s3_raw_retention_days)
        self._s3 = s3_client or boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            config=Config(connect_timeout=5, retries={"max_attempts": 3}),
            region_name="us-east-1",
        )
        self._ensure_bucket()

    # ------------------------------------------------------------- tenancy --
    def _bucket_for(self, tenant_id: str) -> str:
        # Single bucket + tenant prefix today. The bucket-per-tenant swap
        # happens HERE (map tenant_id -> its bucket); nothing above changes.
        return self._bucket

    @staticmethod
    def _key_for(tenant_id: str, content_hash: str) -> str:
        return f"{tenant_id}/{content_hash[:2]}/{content_hash}"

    # ------------------------------------------------------------ bootstrap --
    def _ensure_bucket(self) -> None:
        """Create/verify the landing bucket: object-lock enabled AND
        versioning explicitly Enabled — the pair that makes lock enforcing on
        SeaweedFS (#8350: lock without versioning silently enforces nothing)."""
        bucket = self._bucket
        existing = {b["Name"] for b in self._s3.list_buckets().get("Buckets", [])}
        if bucket not in existing:
            self._s3.create_bucket(Bucket=bucket, ObjectLockEnabledForBucket=True)
        self._s3.put_bucket_versioning(
            Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})

        status = self._s3.get_bucket_versioning(Bucket=bucket).get("Status")
        if status != "Enabled":
            raise WormNotEnforcedError(
                f"bucket {bucket!r}: versioning is {status!r}, not 'Enabled' — "
                "object-lock cannot enforce without it (SeaweedFS #8350)")
        try:
            cfg = self._s3.get_object_lock_configuration(Bucket=bucket)
            lock = cfg.get("ObjectLockConfiguration", {}).get("ObjectLockEnabled")
        except ClientError as e:
            raise WormNotEnforcedError(
                f"bucket {bucket!r}: object-lock configuration unreadable "
                f"({e.response['Error'].get('Code')})") from e
        if lock != "Enabled":
            raise WormNotEnforcedError(
                f"bucket {bucket!r}: object-lock is {lock!r}, not 'Enabled' — "
                "recreate the bucket with ObjectLockEnabledForBucket=True")

    # -------------------------------------------------------------- RawStore --
    def put(self, tenant_id: str, content: bytes, meta: Mapping[str, Any]) -> str:
        content_hash = meta.get("content_hash") or sha256_hex(content)
        if meta.get("content_hash") and meta["content_hash"] != sha256_hex(content):
            raise ValueError(
                f"meta content_hash {meta['content_hash']!r} does not match the bytes")
        bucket = self._bucket_for(tenant_id)
        key = self._key_for(tenant_id, content_hash)

        # Content-addressed write-once: same bytes -> same key -> any existing
        # version already IS these bytes; return it pinned, write nothing.
        try:
            head = self._s3.head_object(Bucket=bucket, Key=key)
            return self._uri(bucket, key, head.get("VersionId"))
        except ClientError as e:
            if e.response["Error"]["Code"] not in ("404", "NoSuchKey", "NotFound"):
                raise

        # S3 user metadata must be ASCII and small: quote the native id, keep
        # full-fidelity metadata on the raw_documents row (native_metadata).
        s3_meta = {"content-hash": content_hash}
        if meta.get("native_id"):
            s3_meta["native-id"] = quote(str(meta["native_id"]))[:1024]
        put_kwargs: dict[str, Any] = dict(
            Bucket=bucket, Key=key, Body=content, Metadata=s3_meta,
            ObjectLockMode="COMPLIANCE",
            ObjectLockRetainUntilDate=datetime.now(timezone.utc) + self._retention,
        )
        if meta.get("mime_type"):
            put_kwargs["ContentType"] = meta["mime_type"]
        response = self._s3.put_object(**put_kwargs)

        version_id = response.get("VersionId")
        if not version_id:
            # No version id => versioning is not actually on => nothing pins
            # or protects these bytes. Refuse to pretend it landed immutably.
            raise WormNotEnforcedError(
                f"put to {bucket}/{key} returned no VersionId — bucket "
                "versioning is not effective, object-lock cannot protect this "
                "object (SeaweedFS #8350)")
        return self._uri(bucket, key, version_id)

    def get(self, uri: str) -> bytes:
        bucket, key, version_id = self._parse_uri(uri)
        kwargs: dict[str, Any] = dict(Bucket=bucket, Key=key)
        if version_id:
            kwargs["VersionId"] = version_id
        return self._s3.get_object(**kwargs)["Body"].read()

    def exists(self, tenant_id: str, content_hash: str) -> Optional[RawDocument]:
        with self._store.transaction(tenant_id) as conn:
            row = conn.execute(
                "SELECT * FROM raw_documents"
                " WHERE tenant_id = %s AND content_hash = %s",
                (tenant_id, content_hash),
            ).fetchone()
        return RawDocument(**row) if row else None

    # ------------------------------------------------------------ WORM probe --
    def verify_worm(self) -> dict[str, Any]:
        """Prove (not assume) enforcement with a sacrificial object: the locked
        version must survive a plain-PUT overwrite, refuse version-specific
        delete (AccessDenied), and survive an unversioned delete. Returns an
        honest report; raises WormNotEnforcedError if any protection is absent."""
        bucket = self._bucket
        key = f"_worm_probe/{uuid.uuid4().hex}"
        original = b"worm probe " + uuid.uuid4().hex.encode()
        put = self._s3.put_object(
            Bucket=bucket, Key=key, Body=original,
            ObjectLockMode="COMPLIANCE",
            ObjectLockRetainUntilDate=datetime.now(timezone.utc) + timedelta(minutes=10),
        )
        version_id = put.get("VersionId")
        report: dict[str, Any] = {"bucket": bucket, "version_id": version_id}
        if not version_id:
            raise WormNotEnforcedError("probe put returned no VersionId (#8350)")

        # 1. Plain overwrite must not clobber the locked version.
        self._s3.put_object(Bucket=bucket, Key=key, Body=b"overwrite attempt")
        survived = self._s3.get_object(
            Bucket=bucket, Key=key, VersionId=version_id)["Body"].read()
        report["overwrite_protected"] = survived == original

        # 2. Version-specific delete of the locked version must be rejected.
        try:
            self._s3.delete_object(Bucket=bucket, Key=key, VersionId=version_id)
            report["delete_rejected"] = False
        except ClientError as e:
            report["delete_rejected"] = (
                e.response["Error"]["Code"] in ("AccessDenied", "InvalidRequest"))

        # 3. Unversioned delete (delete marker) must leave the version readable.
        self._s3.delete_object(Bucket=bucket, Key=key)
        try:
            still = self._s3.get_object(
                Bucket=bucket, Key=key, VersionId=version_id)["Body"].read()
            report["survives_unversioned_delete"] = still == original
        except ClientError:
            report["survives_unversioned_delete"] = False

        if not (report["overwrite_protected"] and report["delete_rejected"]
                and report["survives_unversioned_delete"]):
            raise WormNotEnforcedError(f"object-lock not enforcing: {report}")
        return report

    # -------------------------------------------------------------- uri glue --
    @staticmethod
    def _uri(bucket: str, key: str, version_id: Optional[str]) -> str:
        uri = f"s3://{bucket}/{key}"
        return f"{uri}?versionId={version_id}" if version_id else uri

    @staticmethod
    def _parse_uri(uri: str) -> tuple[str, str, Optional[str]]:
        parsed = urlparse(uri)
        if parsed.scheme != "s3" or not parsed.netloc or not parsed.path.lstrip("/"):
            raise ValueError(f"not an s3 raw-store uri: {uri!r}")
        version = parse_qs(parsed.query).get("versionId", [None])[0]
        return parsed.netloc, parsed.path.lstrip("/"), version
