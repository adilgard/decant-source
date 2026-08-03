# Operator write API — the write-twin of the read choke point (Build Prompt 19)

`knowledge_hub/operator_http.py` + `../migrations/010_operator_write.sql`.
The operator UI's ACTIONS — resolve reviews, control ingestion, acknowledge
errors, manage sources — as a separate, enforced, audited write path. After
this, the UI is wireable end to end: **reads through serving (S5, :8080),
actions through this API (:8081)**.

## The invariant that did not break

The read serving service is untouched and stays PROVABLY read-only: its
choke-point connection runs `default_transaction_read_only=on`, holds no
accessor, and gained no write door. This module runs beside it as its own
service over the write-capable internal-path `PostgresFactStore` — the same
store the pipeline uses, which is exactly why it is a separate process, not
a mode of the read boundary.

## The serving discipline, mirrored onto writes

| Read side (S2–S5) | Write side (BP19) |
|---|---|
| OpenBao credential → Principal | SAME resolver, same 401-generic fail-closed |
| Label roles gate what you SEE | Write roles gate what you may DO (`reviewer` ⊂ `operator`; agent read-principals match nothing) |
| Fixed op registry, build-time | Fixed `WriteOperation` registry, build-time (`register_operator_defaults`) |
| `{sec:}` marker unwritable without | Unscoped spec unconstructable (`scope` min_length=1); identity-shaped params unregistrable |
| `PostgresChokePoint.enforce→read` | `OperatorGate.execute`: role gate → coerce → handler with tenant injected FROM THE PRINCIPAL only |
| Absence rule (404, never described) | Cross-tenant target = LookupError = 404; nothing mutates |
| Usage instrumentation | `operator_audit` — EVERY attempt, refused ones included |

Error mapping: 401 unresolvable · 403 role-refused (audited `refused`) ·
404 unknown action / target absent in principal's tenant (audited `failed`)
· 409 target exists but not actionable (already reversed / not open / not
alerting) · 400 bad params. Writes are serialized behind one lock (operator
actions are UI clicks; strict audit ordering comes free).

## The v1 write operations

Review resolution (scope: reviewer, operator):
* `resolve_merge {candidate_id, same}` → `ResolutionService.decide_match`
  — merge (reversible, snapshotted) or keep-separate; human_review label.
* `resolve_as_new {mention_id}` → every open pair labeled hard-negative,
  mention becomes a new entity.
* `split_merge {merge_id}` → `reverse_merge`: restores the absorbed entity
  under its original id, repoints dependent facts, re-resolves mentions,
  er_nonmatch reversal label.
* `triage_quarantine {quarantine_id, decision, note?}` → 004 status +
  a `correction` flywheel label (extraction feedback).
* `resolve_flagged_document {document_id, corrected_data_track?, note?}` →
  §8.1a adjudication: human tag wins, claim corrected at its SOURCE
  (raw_documents.native_metadata), dispatch item requeued so processing
  picks the adjudicated doc back up.

Ingestion + alerts (scope: operator):
* `pause_source` / `resume_source` → registry status; capture SKIPS
  disabled sources, so the pause is real (tested through a live capture run).
* `retry_failed_item {queue, item_id}` → outbox row back to queued, ack
  cleared.
* `acknowledge_alert {kind, item_id, note?}` → ack stamp; the item leaves
  `operator_alerts` (a VIEW over real state: unacked failed queue items +
  degraded sources — no new event stream). Degraded sources clear by being
  FIXED (resume / healthy run), not dismissed.

Sources (scope: operator):
* `add_source {source_ref, source_system, config?}` / `edit_scope
  {source_ref, config}` → registry upsert. Credential-shaped config keys
  are REFUSED structurally (`_CREDENTIAL_KEY`); the response carries the
  OpenBao path where the secret belongs + whether one is PRESENT — the
  value never transits, is never stored, never logged.
* `start_pull` — NOT BUILT (bookmarked): needs a pull-request queue the
  capture runner consumes; capture runs are khctl-batch today.

## One action, two records

`operator_audit` (migration 010) answers who/what/when/outcome +
`snapshot_ref` (the domain's reversibility pointer, e.g.
`entity_merges:45` — never duplicated into the audit row). Review
decisions ALSO land in `labels` (005 §3.4) via the domain logic — the
human's calls are exactly the flywheel's gold labels.

## HTTP surface

`POST /v1/actions/<name>` (generated from the registry), `GET /v1/actions`
(role-scoped catalog — empty for an agent), `GET /v1/alerts` (write-role
gated), `GET /v1/health` (proves migration 010), `GET /v1/metrics` (same
LatencyStats as the read side). Same stdlib plumbing — `make_server` is
shared. Run: `python -m knowledge_hub.operator_http` (OPERATOR_HOST /
OPERATOR_PORT, loopback:8081 default).

## Tests

`tests/test_operator_http.py` (12 tests; real Postgres + OpenBao + live
bge-m3, BOTH services on real sockets; suite 299 green + 4 skips):
the headline agreement test — reviewer `resolve_merge` over HTTP →
entity_merges reversible snapshot + er_match label + audit row, and the
merge is VISIBLE through the READ serving layer (absorbed entity's facts
serve under the survivor; absorbed entity serves absence); `split_merge`
reverses it end-to-end (facts re-serve apart, reversal hard-negative
label); keep-separate labels without merging; cross-tenant writes 404 with
zero mutation and the attempt audited in the intruder's tenant; agent
read-principals 403 on every action (12 refusals audited); unknown/
revoked/malformed principals 401; unscoped/identity-asserting specs
unregistrable + tenant-in-body 400 + no raw-mutation URL surface;
pause→capture skips / resume→capture lands (live run); acknowledge clears
the alert view, retry requeues, double-ack 409; quarantine triage leaves
the review queue + correction label; flagged-document adjudication corrects
the claim and requeues.

check_stack: check 10, `operator write API (BP19)` — non-mutating boundary
proof (health/401/403/404, attempts audited); also added to `khctl verify`
for plans with postgres+secrets seams. Package 0.22.0 (pyproject +
`__init__` + editable reinstall verified; migration 010 applied to the
pilot DB + tracked).

## Credential lifecycle (BP23 — the security note)

**Minting.** Exactly three paths create console credentials, all wrapping
`deploy_apply.provision_operator_credential`:

1. **Deploy bootstrap** (`phase_tenants`): ONE operator-role credential per
   tenant, printed once to the deploying operator's terminal — the same
   ceremony as the vault unseal shares. Idempotent via its own vault marker
   (`kh/bootstrap/operators/<tenant>`): a re-run says "already provisioned"
   and never re-mints or re-prints.
2. **`khctl provision-operator --tenant <t> --role operator|reviewer`** —
   the issue-more path (an SME reviewer, later operators). Vault
   custody IS the gate: it works only where the vault accepts your token.
3. **Dev-mint** (`khctl console`, dev/pilot context only): a throwaway key
   so the bench console is reachable immediately. Context gate: DEPLOYED =
   `deploy_plan.json` present AND the env does NOT carry the pilot
   dev-vault literal — a real deployment's root token lives with custody,
   never on disk, so dev-mint structurally cannot fire there. The pilot
   vault is dev-mode (ephemeral): dev keys die with it.

**Storage.** A credential value exists exactly twice, ever: hashed
(sha256) as its registry path in the vault, and on the minting human's
terminal. The registry RECORD carries the identity triple + attribution
(`provisioned_by`, `provisioned_at` — the resolver ignores extra keys);
markers carry principal ids. Nothing token-shaped is ever written to disk,
logs, shortcuts, or the kit (tested: work dirs, operator.log, usage logs
all grepped clean; the kit's no-secrets guard is the second net).

**Use.** The UI sends the value as a Bearer header only; holds it in
memory + sessionStorage for the session; never logs or renders it. 401 on
any resolution failure, indistinguishable for unknown/revoked/malformed.

**Revocation / rotation.** Delete the registry entry (hvac
`delete_metadata_and_all_versions` at `serving/principals/<digest>`) — the
next request 401s (tested in BP19/20). Lost value = re-issue via
`provision-operator`; values cannot be recovered. Expiry/TTL is NOT built —
registry entries live until revoked (bookmarked below).

## Follow-ups (scoped, not built)

* `start_pull` (above) — lands in this registry when the pull-queue exists.
* Review-queue LISTING for the UI: `review_queue` / `operator_alerts` reads
  are operator-shaped, not envelope-shaped — decide whether the UI lists
  reviews via a serving-side operator read or a `GET /v1/reviews` here
  (today: alerts listing exists; review items come from the DB-facing UI
  scaffold).
* Rotation/expiry for operator credentials (vault registry rows are
  static today; same posture as agent tokens). BP23 added attribution +
  revocation-by-delete; TTL/auto-expiry remains open.

## Build Prompt 25 — on-site hardening (operator-service side), 2026-07-26

- **Login validates the console ROLE, not mere resolvability (F3).**
  `GET /v1/actions` — the exact call the UI's unlock makes — now answers
  403 with named-kind guidance ("this is an AGENT serving credential; log
  in with the OPERATOR CONSOLE credential") for any principal without
  reviewer/operator. The agent token printed back-to-back with the
  operator one at bootstrap can no longer unlock a permanently blank
  dashboard.
- **`GET /v1/passages/<chunk_id>` (F18).** The fact→evidence direction of
  the trust question: dereferences a served FactEnvelope's chunk_id to the
  passage text + document title (+ id/seq), reusing the reads layer's
  `_passage`. Role-gated + tenant-scoped exactly like the other operator
  reads; absence (404) for other tenants and unknown ids; read-only.
- **`/v1/health` carries `vault_status` (F1):** `ok | sealed |
  unreachable` — `OpenBaoCredentialResolver.status()` now checks the
  SEALED flag (the old bool-test of the hvac health dict reported
  `vault: true` while sealed, i.e. while every credential was being
  refused). `vault` stays a bool and is False when sealed. Same fields on
  the read service's health. The lock screen branches its login-failure
  message on this. BP31 (BP28 #17): the sealed-vault message the lock
  screen prints now carries `-e BAO_ADDR=http://127.0.0.1:8200` — the
  `bao` CLI defaults to HTTPS while the production listener is plain
  HTTP, so the previous command failed verbatim.
- **`monitor.alerts_open` (F5):** the UI errors badge now counts the
  `operator_alerts` view (unacknowledged failed queue items + degraded
  sources). The old `status='error'` count was structurally zero — no
  pipeline code ever writes that status.
- **`khctl alerts` (F5)** is the first consumer of `GET /v1/alerts` +
  `retry_failed_item` + `acknowledge_alert`: lists open alerts, `--retry
  dispatch:<id>` / `--ack extraction:<id>` act on them. Auth = an
  operator credential via KH_OPERATOR_TOKEN or a hidden getpass prompt;
  401s consult health first and name a sealed vault instead of blaming
  the token.
- **`khctl provision-agent --tenant <t>` (F16):** the re-mint path for
  the AGENT SERVING credential (roles=[], principal `<t>-agent-<hex>`,
  attribution on the record) — same registry write phase_tenants
  bootstraps with (`deploy_apply.provision_agent_credential`), same
  custody gate + print-once ceremony as provision-operator. Old
  credentials keep working until revoked (revocation-by-delete,
  unchanged).

The BP23 "review-queue listing" follow-up above is superseded: `GET
/v1/reviews` has existed since BP20; the alerts consumer now exists too
(the CLI). Remaining open: TTL/expiry (client-later, unchanged) and the
full Errors & health TAB (Design follow-up — the badge is now honest and
the CLI covers triage).
