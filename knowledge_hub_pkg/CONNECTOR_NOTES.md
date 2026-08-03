# Connectors (contract hardening + Microsoft Graph) — notes

Delivered 2026-07-24: the `SourceAdapter` contract hardened against the five
Graph-shaped seams (+ two it forced into the open), and the first real API
connector — `sources_msgraph.py` (SharePoint/OneDrive, files-first) — filling
in the hardened template. Code + tests DONE (236 passed / 4 pre-existing
minisign skips; 19 new: 7 contract-conformance on the real stack, 12 Graph
unit over a scripted transport). **Live M365 tenant validation is the one
remaining gate** (admin consent + real throttling + real ACL edge cases
cannot be faked — runbook below).

## The two contract breaks the brief didn't name (found during hardening)

1. **Ordered-cursor assumption.** The old contract required cursor tokens
   whose string order equals scan order, and the registry defended
   checkpoints with `GREATEST(...)`. Graph delta links are opaque — a later
   state does not compare greater — so the monotonic guard would silently
   refuse to advance. Fix: `SourceAdapter.cursor_ordering: "ordered"|"opaque"`;
   opaque checkpoints are stored verbatim (last-write-wins is safe: the
   adapter is the sole writer and yields in sweep order). Two riders:
   * `final_cursor()` — the deltaLink materializes only on a sweep's final
     page, and an EMPTY sweep yields no items to piggyback it on; the flow
     persists the final cursor after iterator exhaustion so tokens advance
     instead of quietly aging toward expiry.
   * `CursorInvalid` — delta tokens die (HTTP 410 resyncRequired, corrupt
     state). The capture flow resets checkpoints and re-runs as backfill in
     the same call — safe end to end (landing is content-hash idempotent,
     changed items version up). A CursorInvalid during backfill is an
     adapter bug and re-raises.

2. **Deletes didn't exist.** Graph delta reports `@removed`; the contract
   could only say "here are bytes." Per §8.1g: `SourceItem.change =
   "upsert"|"tombstone"`, tombstones ONLY from explicit signals (never
   inferred from absence — a vanished drive is logged and dropped, not
   tombstoned). Landing stamps `deleted_at` on every version row of the
   logical doc (migration 008); bytes stay in WORM; a re-observed upsert
   REVIVES the rows (recycle-bin restore outranks the old delete signal).
   **Storm guard** (§8.1g's mass-delete caution): one run may tombstone at
   most max(10, 10% of live corpus) docs — beyond that the run halts
   un-checkpointed, degrades the source for review; re-run with
   `allow_mass_tombstone=True` to apply deliberately. Downstream propagation
   (facts valid_to, serving-side filtering on deleted_at) is deliberately
   NOT built here — capture records the authoritative signal; reacting is
   the processing/serving follow-on (flagged as next unblocked).

## Design decisions

- **Auth = client credentials** (application permissions + admin consent) —
  a daemon has no user and no refresh token. OpenBao holds ONLY long-lived
  material at `tenants/<tenant>/sources/<credential_ref>`:
  `{"directory_id", "client_id", "client_secret"}`. Access tokens are minted
  on demand, cached in memory only, masked via OutboundRequest, re-minted on
  expiry skew or once on a mid-pull 401 (second 401 = SecretsError → the
  flow degrades that source). No vault write-back → SecretsProvider ABC
  unchanged. Delegated flows with rotating refresh tokens (if a client ever
  forces one) are the documented future extension; `put_secret` is the
  existing write path.
- **`credential_ref`** (defaults to `source_ref`): sibling sources of one
  auth family (msgraph-files + msgraph-mail, one Entra app) share one vault
  credential — rotation stays a one-place edit.
- **Mid-pull SecretsError now degrades the source** (previously only
  `prepare()` failures did); checkpoints already taken mean the next good
  run resumes.
- **Composite per-drive cursor** (decided over source-per-site):
  one `msgraph-files` source; opaque JSON
  `{"v":1,"done":{driveId: deltaLink},"cur":{"drive","link"}}`, drives swept
  in sorted-id order, every item's cursor re-fetches its own page (resume
  replays ≤1 page; at-least-once + hash idempotency make replay free). New
  drive in the tenant → no `done` entry → auto-enumerated from scratch.
  Backfill IS the initial delta enumeration, so the first incremental run
  rides changes immediately.
- **ACL normalized, groups BY REFERENCE** (decision): versioned
  `SourceAcl` model (`posix.v1`, `msgraph.driveItem.v1`) with `AclGrant`
  principals (user/group/site_group/application/link/anyone), normalized
  roles read|write|owner, `via` direct|inherited|link, faithful payload in
  `.raw`. Group grants carry the Entra group id, never a member list —
  membership resolves at serving time (§2 #9: access change = one row edit).
  Serving-side needs a per-tenant Entra group projection eventually
  (flagged, not built). The filesystem adapter was re-expressed against the
  same model (posix.v1) to prove the normalizer isn't Graph-shaped.
- **Throttling is transport-level, not contract-level**: `ThrottledTransport`
  honors Retry-After on 429/503/504 (expo backoff + jitter otherwise, caps
  120s/300s), retries transient 5xx, counts what it endured →
  `CaptureRunResult.source_stats` via `adapter.stats()`.
- **Oversized files are DEFERRED, not streamed** (§8.1e "defer truly-huge"):
  > `max_content_bytes` (default 256 MiB) → skipped + counted
  (`skipped_oversized`), cursor still advances. Streaming is future work if
  a real corpus demands it. Items that 403/404 between listing and download
  → `skipped_unreadable` (the fs adapter's discipline).
- **Tenant guard**: `prepare()` records its tenant; `require_prepared()` in
  the iterators turns a cross-tenant reuse of a credentialed adapter (cached
  token crossing the isolation boundary) into a loud RuntimeError.
- Package: 0.16.2 → **0.17.0** (new module, migration 008, `requests`
  promoted to an explicit dependency — was already pinned in the lock via
  hvac).

## Live M365 validation runbook (the remaining gate)

> **STATUS 2026-07-24:** M365 dev-sandbox path CLOSED (Developer Program
> denied — partner-restricted). Running this runbook ON-SITE against the
> real tenant, week of 2026-07-27, with IT (the admin steps 1–2
> below) and the agents-DB engineer; on-site game plan to be drafted before the visit.

1. **Entra app registration** (portal → App registrations → New):
   single-tenant app, no redirect URI (daemon). Record Directory (tenant)
   ID + Application (client) ID; create a client secret (note expiry — this
   is the credential that rotates via OpenBao).
2. **API permissions** (Microsoft Graph → *Application* permissions):
   `Files.Read.All` + `Sites.Read.All` (tenant-wide read; simplest for our
   own tenant). Least-privilege alternative for wary clients:
   `Sites.Selected` + per-site grants via the sites/permissions API — the
   adapter already takes a site allowlist, so this is config, not code.
   Then **Grant admin consent** (the button — needs a tenant admin).
3. **Provision the credential** (compose stack up):
   ```python
   from knowledge_hub.secrets_openbao import OpenBaoSecretsProvider
   OpenBaoSecretsProvider().put_secret("default", "msgraph-files", {
       "directory_id": "<tenant-guid>",
       "client_id": "<app-guid>",
       "client_secret": "<secret>",
   })
   ```
4. **Run it**:
   ```python
   from knowledge_hub.capture import CaptureService
   from knowledge_hub.dispatch_pg import PostgresDispatcher
   from knowledge_hub.pipeline import Pipeline
   from knowledge_hub.rawstore_s3 import S3RawStore
   from knowledge_hub.secrets_openbao import OpenBaoSecretsProvider
   from knowledge_hub.sources_msgraph import MsGraphFilesAdapter

   pipeline = Pipeline()
   capture = CaptureService(pipeline, S3RawStore(store=pipeline.store),
                            PostgresDispatcher(pipeline.store),
                            secrets=OpenBaoSecretsProvider())
   adapter = MsGraphFilesAdapter("msgraph-files", sites="all")
   print(capture.run_source("default", adapter))   # backfill
   print(capture.run_source("default", adapter))   # incremental (delta)
   ```
5. **The gauntlet** (each asserts a hardened seam against reality):
   - [ ] backfill lands every file; `source_acl.model == "msgraph.driveItem.v1"`
   - [ ] kill mid-backfill (Ctrl-C) → re-run resumes, zero duplicate rows
   - [ ] edit a file in SharePoint → incremental lands version 2 only
   - [ ] change an item's permissions (add a group, create an anonymous
         link) → re-observed item's ACL shows the group BY id + the link
         grant with scope/expiry
   - [ ] delete a file → tombstone: every version row gets `deleted_at`
   - [ ] restore from recycle bin → rows revived (`deleted_at` cleared)
   - [ ] delete a whole folder tree (> storm threshold) → run halts
         degraded "tombstone storm"; re-run `allow_mass_tombstone=True`
         applies
   - [ ] let the delta token expire (or corrupt `source_registry.cursor` by
         hand) → next run auto-resyncs via backfill, `status_reason` says so
   - [ ] `run_source(...).source_stats` shows real throttle counts on a big
         pull; wall-clock survives 429 storms
   - [ ] landed docs flow parse→chunk→extract→resolve UNTOUCHED (the
         downstream is door-agnostic — this is the whole point)

---

# QuickBooks Online adapter (2026-08-03)

`sources_qbo.py` — the second connector family, filling in the hardened
template with zero contract reopenings and ONE anticipated extension (the
rotating-refresh-token write-back the 2026-07-24 notes flagged as "the
documented future extension"). Package 0.28.0. Both forks decided:
QuickBooks ONLINE (not Desktop — no REST surface, Windows-chained, sunsetting)
and the NARROW rotation seam (not widening SecretsProvider).

## The one extension: CredentialRotator

Intuit offers NO daemon flow — auth is user-consented OAuth whose refresh
token ROTATES (~every 24h of use, hard expiry 100 days idle). No write-back
= self-lockout within a day, so:

- **`CredentialRotator` ABC** (interfaces.py, pure addition): one method,
  `rotate_credential(tenant, source_ref, updates)` — MERGE semantics, fields
  not named survive. `SecretsProvider` stays read-only; only adapters that
  declare rotation ever see a write path; every capture-flow vault write goes
  through this one auditable method. `OpenBaoSecretsProvider` implements it
  via the existing `put_secret` (rotation is an UPDATE — rotating an
  unprovisioned credential raises SecretNotFound, tested).
- **Persist-before-use**: a fresh refresh token is written to the vault
  BEFORE its sibling access token is used; a vault failure raises
  SecretsError and the run degrades — never proceed with an unpersisted
  rotation.
- **Refusal at prepare()**: a provider that is not a CredentialRotator is
  refused loudly (SecretsError) instead of running toward a guaranteed
  future lockout.

## Design decisions

- **Credential** at `tenants/<tenant>/sources/<credential_ref>`:
  `{"client_id", "client_secret", "refresh_token", "realm_id",
  "environment": "production"|"sandbox"}` (environment optional, default
  production; realm_id = the QuickBooks company id). The FIRST refresh token
  comes from a one-time human consent ceremony (runbook below) — the QBO
  equivalent of the Entra admin-consent step.
- **Cursor** (opaque JSON): `{"v":1, "since": <ISO>, "entities": [...],
  "cur": {"entity","pos"}|null}`. Backfill pages each entity via the query
  endpoint (`ORDERBY Id`, STARTPOSITION pagination, 1000-row cap); every
  item's cursor re-fetches its OWN page (resume replays ≤1 page). `since` is
  server-authoritative: the FIRST response's `time` minus a 300s overlap pad,
  captured at sweep start, so the first incremental run rides everything
  from before the backfill began. Incremental = one CDC call
  (`changedSince`), item cursors keep the OLD since (whole-sweep replay on
  resume — cheap, idempotent), final_cursor() advances it from the CDC
  response's server time.
- **Deterministic resync triggers** (CursorInvalid → the existing
  auto-backfill path): since beyond CDC's 30-day lookback (checked locally
  BEFORE the call, 1-day safety margin, plus the server's own 400 mapped);
  a CDC entity list at the 1000-row cap (possible truncation — a re-walk
  costs time, silence costs records); the configured entity set differing
  from the cursor's (a widened set has never been backfilled); unparseable
  state. All four tested.
- **Tombstones**: CDC reports deleted transaction entities explicitly
  (`status: "Deleted"`) → `change="tombstone"` (§8.1g, authoritative signal
  only). List entities (Customer/Vendor/Item/...) are never hard-deleted in
  QBO — deactivation arrives as an `Active: false` UPSERT, which is correct:
  state change, not delete. Storm guard applies unchanged.
- **ACL is company-scope** (like the planned mail adapter's triviality):
  QBO has no per-record permissions. `qbo.company.v1`, one grant —
  principal_type `domain`, principal_id = realm — owner = realm.
- **Structured track, no LLM**: records land as CANONICAL JSON bytes
  (sort_keys — identical record = identical content hash, so re-landing
  stays a no-op) with `data_track: "sor"` + `doc_type: "qbo.<entity>"`
  declared on native_metadata (§8.1a: claims, arbitrated downstream). This
  is the first keyed SoR source for the resolution flywheel — record ids,
  tax ids, emails everywhere (the prose corpus yields zero deterministic
  labels; this is where T0 positives start).
- **Default entity set** (config narrows/widens; changing it later forces a
  deliberate resync): Account, Bill, BillPayment, CreditMemo, Customer,
  Deposit, Employee, Estimate, Invoice, Item, JournalEntry, Payment,
  Purchase, PurchaseOrder, Vendor — all CDC-supported.
- **Transport**: QboTransport = the msgraph throttle discipline
  (Retry-After on 429/503/504, expo+jitter, transient 5xx, 401 re-mint
  exactly once) re-expressed for Intuit; deliberately NOT shared code —
  the proven msgraph module stays untouched (extend, never modify), and the
  ~50 duplicated lines are the price of that. `minorversion=75` (Intuit's
  mandated floor) on every call. Rate context: ~500 req/min per realm.
- **khctl ingest**: qbo joins msgraph in the "runs via its own credentialed
  runbook" category (`adapter_for` unchanged) until a registry-driven
  factory is warranted.

## Sandbox validation runbook (free, instant — unlike M365)

1. **Developer account + app**: developer.intuit.com → create an app
   (QuickBooks Online Accounting scope, `com.intuit.quickbooks.accounting`).
   Record Client ID + Client Secret (per environment — sandbox and
   production keys differ). A sandbox company comes free with the account;
   its realm id is on the sandbox dashboard.
2. **Consent ceremony** (the ONE human step; repeat only if the refresh
   token ever dies): use Intuit's OAuth 2.0 Playground (developer portal →
   your app) — authorize against the sandbox company, copy the REFRESH
   token and realm id. This is admin-free: no tenant admin, your own
   developer account owns the sandbox.
3. **Provision** (compose stack up):
   ```python
   from knowledge_hub.secrets_openbao import OpenBaoSecretsProvider
   OpenBaoSecretsProvider().put_secret("default", "qbo-books", {
       "client_id": "<app key>",
       "client_secret": "<app secret>",
       "refresh_token": "<from the playground>",
       "realm_id": "<sandbox company id>",
       "environment": "sandbox",
   })
   ```
4. **Run it**:
   ```python
   from knowledge_hub.capture import CaptureService
   from knowledge_hub.dispatch_pg import PostgresDispatcher
   from knowledge_hub.pipeline import Pipeline
   from knowledge_hub.rawstore_s3 import S3RawStore
   from knowledge_hub.secrets_openbao import OpenBaoSecretsProvider
   from knowledge_hub.sources_qbo import QboAdapter

   pipeline = Pipeline()
   capture = CaptureService(pipeline, S3RawStore(store=pipeline.store),
                            PostgresDispatcher(pipeline.store),
                            secrets=OpenBaoSecretsProvider())
   adapter = QboAdapter("qbo-books")
   print(capture.run_source("default", adapter))   # backfill
   print(capture.run_source("default", adapter))   # incremental (CDC)
   ```
5. **The gauntlet** (each asserts a hardened seam against reality):
   - [ ] backfill lands every record of every default entity;
         `source_acl.model == "qbo.company.v1"`
   - [ ] kill mid-backfill (Ctrl-C) → re-run resumes, zero duplicate rows,
         completed entities not re-fetched
   - [ ] edit a customer in the sandbox UI → incremental lands version 2 only
   - [ ] deactivate a vendor → arrives as UPSERT with `Active: false`
         (state change, NOT a tombstone)
   - [ ] delete an invoice → CDC tombstone: every version row `deleted_at`
   - [ ] void an invoice → arrives as a changed record (upsert), not deleted
   - [ ] run twice with no changes → zero landed, cursor still advances
         (empty-sweep final_cursor)
   - [ ] hand-corrupt `source_registry.cursor` → auto-resync via backfill
   - [ ] hand-age `since` past 30 days → local CursorInvalid, resync,
         `status_reason` says lookback
   - [ ] let the vault hold a stale refresh token (revoke via the developer
         portal) → SecretsError with the re-consent hint, source degraded,
         other sources unaffected
   - [ ] after ~25h of scheduled runs: vault's refresh_token has silently
         rotated at least once (read it back and compare) — the write-back
         proven live
   - [ ] landed docs flow parse→extract→resolve; deterministic keys
         (qbo ids) produce the first real T0 flywheel labels

## Deliberately deferred (QBO template fill-ins)

- **Per-entity `structured_map` registry config** (column→predicate for the
  StructuredMap strategy): ontology work, registry DATA, not adapter code.
  Until declared, §8.1a arbitration holds the records at review — correct,
  not a bug. This is the next unblocked piece and the one that turns landed
  records into facts.
- **Webhooks** (push instead of CDC polling): needs a public endpoint —
  against the local-first posture; polling cadence is the khctl ingest
  `--watch` loop's job.
- **Attachments** (`Attachable` binary downloads — receipts, PDFs): a
  prose-track sibling sweep, natural follow-on.
- **QuickBooks Desktop**: refused for now (no REST surface, Web-Connector
  SOAP + Windows sidecar against Ubuntu targets, product sunsetting). If a
  client engagement forces it, it is its OWN design, not a template fill-in.

## Deliberately deferred (template fill-ins, not reopenings)

- **Outlook mail adapter** (`msgraph-mail`): same auth/transport/cursor
  machinery; message delta per mailbox; ACL is trivial (mailbox = owner).
- **`$batch` for permissions** (20 requests/1): cuts the per-item ACL call
  volume once real-tenant throttling data says it matters.
- **Per-drive resync**: a 410 currently resyncs the whole source; Graph's
  410 response carries a fresh delta Location that would scope it to one
  drive.
- **Tombstone propagation downstream** + serving-time `deleted_at`
  filtering; Entra group projection for serving-time membership resolution.
- **eTag/cTag short-circuit**: skip re-downloading when the item's cTag
  matches the last landed version's (needs a registry-side lookup; pure
  optimization, idempotency already makes re-downloads harmless).
