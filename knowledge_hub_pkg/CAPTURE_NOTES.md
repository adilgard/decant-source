# Capture path (Build Prompt 2) — notes

Delivered: capture-side ABCs in `knowledge_hub/interfaces.py` (SecretsProvider,
RawStore, SourceAdapter, Dispatcher + OutboundRequest/SourceItem/SecretsError),
implementations `secrets_openbao.py`, `rawstore_s3.py`, `sources_fs.py`,
`dispatch_pg.py`, the landing flow `capture.py` (CaptureService + SourceRegistry),
`../migrations/002_capture_registry.sql`, and 25 new tests (44 total, all against
the real dockerized Postgres/SeaweedFS/OpenBao — no mocks). Landing calls INTO
Prompt 1's `_persist_raw` / `_next_version`; idempotency and versioning are not
reimplemented anywhere.

## Pointing it at a folder for the pilot

Put the folder's files where the pilot box can read them (a local directory or an
SFTP/SMB share mounted with sshfs), then run — with the compose stack up and the
venv active:

```python
from knowledge_hub.capture import CaptureService
from knowledge_hub.dispatch_pg import PostgresDispatcher
from knowledge_hub.pipeline import Pipeline
from knowledge_hub.rawstore_s3 import S3RawStore
from knowledge_hub.secrets_openbao import OpenBaoSecretsProvider
from knowledge_hub.sources_fs import FilesystemSourceAdapter

pipeline = Pipeline()
capture = CaptureService(pipeline, S3RawStore(store=pipeline.store),
                         PostgresDispatcher(pipeline.store),
                         secrets=OpenBaoSecretsProvider())
adapter = FilesystemSourceAdapter(source_ref="fs-pilot", root=r"/path/to/folder")
print(capture.run_source("default", adapter))   # first run: backfill
print(capture.run_source("default", adapter))   # later runs: incremental
```

The first call registers `fs-pilot` in `source_registry` and backfills every file
(resumable — re-running after a crash continues from the last checkpoint); every
later call is an incremental pull of files modified since the stored cursor.
Each file lands as an object-locked, version-pinned object in `kh-raw` plus a
`raw_documents` row (tenant-scoped, sha256-hashed, ACL and native metadata
captured), and a reference is enqueued on `dispatch_queue` for the processing
stages. No credentials are needed for the filesystem adapter; for credentialed
sources, provision the secret at `secret/tenants/<tenant>/sources/<source_ref>`
in OpenBao first.

## The #8350 verification (WORM is proven, not assumed)

Reproduced live on the deployed SeaweedFS (4.40, commit 875cd1f67): a bucket
created with `ObjectLockEnabledForBucket=True` **reports** object-lock
`Enabled`, stores COMPLIANCE retention, reads it back — and enforces nothing;
overwrite and delete both succeeded. Enforcement becomes real only when bucket
**versioning** is explicitly enabled. With versioning on, standard S3 lock
semantics hold: a plain PUT to a locked key is *never* "rejected" — it stacks a
new version — and protection means the locked **version** refuses deletion
(`AccessDenied`) and stays readable. Consequences in `rawstore_s3.py`:

- `_ensure_bucket` enables + verifies BOTH flags and refuses to start otherwise
  (`WormNotEnforcedError`);
- `put()` stamps COMPLIANCE retention (default 3650 days, `S3_RAW_RETENTION_DAYS`)
  and returns a **version-pinned** URI (`s3://bucket/key?versionId=…`), so a
  landed object stays retrievable even if something later writes over its key;
- `verify_worm()` proves enforcement with a sacrificial object on every
  `check_stack.py` run, and `test_rawstore_worm.py::test_8350_overwrite_of_landed_object_is_rejected`
  asserts a landed object survives an overwrite attempt, rejects version delete,
  and survives an unversioned delete. If a SeaweedFS upgrade regresses
  enforcement, the suite and check_stack both go red.

Residual honesty: WORM holds at the S3 API surface. A host admin can still
destroy the Docker volume; production hardening (separate storage credentials,
replication) is future work.

## Design decisions

- **Dispatcher = Postgres outbox**, not an in-process queue: at-least-once must
  survive a crash between "landed" and "processing started"; a durable row in
  the same Postgres adds no infra. Enqueue is idempotent per
  `(tenant, raw_document_id)`; **delivery** is at-least-once via lease expiry
  (claim/ack/nack shipped now so the property is testable; consumers arrive
  with the processing stages).
- **Secrets seam**: credentials live at `tenants/<tenant>/sources/<source_ref>`
  in the KV v2 mount — the layout per-tenant production policies scope to; auth
  is pluggable (pass an AppRole/k8s-authed `hvac.Client`; dev mode's root token
  is just the default). `inject_credential` fills an `OutboundRequest`, whose
  repr/str MASK injected keys, so requests can appear in logs/exceptions
  without leaking. A missing/denied secret raises `SecretNotFound` /
  `SecretAccessDenied` (paths only, never values) and CaptureService degrades
  that ONE source in `source_registry` (status + reason) — the tenant's other
  sources keep landing; the source auto-recovers on the next successful run.
- **Resumability**: adapters yield items in deterministic cursor order;
  CaptureService checkpoints `source_registry` after each landed+dispatched
  item (`backfill_cursor` mid-pull, `cursor` as the incremental high-water
  mark). A pull that dies partway resumes strictly after the last safe item —
  verified by test with a dispatcher that crashes mid-backfill.
- **Filesystem adapter**: stable relative path = `native_id`; cursor token
  `f"{mtime_ns:020d}:{path}"` (string order == scan order). `source_acl`
  captures mode/uid/gid/owner/group best-effort per platform; everything else
  observed at acquisition goes to the new `raw_documents.native_metadata`
  (migration 002) — generous by design, it's irreplaceable. Unreadable files
  are skipped and counted (`skipped_unreadable`) rather than wedging the pull
  at their cursor forever. Drive/SharePoint/Gmail adapters later extend the
  same ABC (`prepare()` + two iterators); nothing in the flow changes.
- **models.py ↔ schema lock-step** (same commit): migration 002 ↔
  `RawDocument.native_metadata`, `SourceRegistryEntry`, `DispatchMessage`;
  `_persist_raw` INSERT extended for the new column.

## Reproducibility bundle

No new containers — SeaweedFS and OpenBao were already in docker-compose.yml,
and boto3/hvac were already pinned in requirements.lock.txt. `install-ubuntu.sh`
applies `migrations/*.sql` by glob, so 002 replays automatically (it is applied
and recorded in `schema_migrations` on the local pilot DB). `check_stack.py`
now exercises the real capture code paths: `S3RawStore` bootstrap +
`verify_worm()` for SeaweedFS, and the per-tenant `inject_credential` seam for
OpenBao. All four checks green on this machine 2026-07-22.
