# API service — the enforcement boundary made physical (Build Prompt S5)

`knowledge_hub/service_http.py` implements the S1 `ServingService` seam AND
the HTTP+JSON surface in front of it. This closes the serving layer end to
end: source → … → facts, served through ONE enforced door.

## Why a service, not a library (Decision 6)

Agents live OUTSIDE the enforcement boundary. A library an agent imports
could reach around the choke point or self-assert identity; a process
boundary can't be reached around — the caller holds an opaque credential
and an HTTP client, nothing else. The service IS the boundary.

It is deliberately **stdlib-only** (`http.server` over a framework-free
`ServingApp.handle()` core): the entire boundary — routing, auth, dispatch,
error mapping — is plain auditable code with zero framework magic between
the request and the S2 gate. No new runtime dependencies.

## What dispatches where (S1–S4 assembled, nothing new built)

| Route | Dispatches to | Answer |
|---|---|---|
| `POST /v1/ops/<name>` | `InProcessOperationCatalog.execute` (S3) | S1 envelopes; composites per-step-tagged + trace |
| `POST /v1/retrieve` | `DenseRetrievalService.retrieve` (S4) | EvidenceEnvelopes; `enrich` is a caller param |
| `GET /v1/ops` | `catalog.list_for(principal)` | the scope-filtered public catalog (no SQL leaves authoring) |
| `GET /v1/health` | warm-enforce + vault ping | component booleans + installed version, never tenant data |
| `GET /v1/metrics` | `LatencyStats.snapshot()` | per-endpoint p50/p95/p99 vs the §4 budget |

Endpoints are GENERATED from the S3 registry: one POST per registered
op/composite (`ServingApp.endpoints()` is the registry spelled as URLs).
Adding an op = a registry change + redeploy. There is **no ad-hoc query
endpoint and no raw-SQL endpoint** — an unregistered/out-of-scope name
answers 404 `unknown_operation` (the absence rule applied to the URL space).

## Tenancy: ops are registered PER TENANT (read this before wiring an agent)

`register_serving_defaults(catalog, tenant_id)` registers the op surface for
ONE tenant, and dispatch looks the operation up by `principal.tenant_id`. A
principal whose tenant was never registered therefore sees an empty catalog
on `GET /v1/ops` and gets `404 unknown_operation` on every call — the
absence rule working exactly as designed, and indistinguishable from "that
op does not exist" if you are not expecting it.

The registered set comes from `SERVING_TENANTS`. **Measured on the pilot
2026-08-07: `SERVING_TENANTS=ops,finance`, while every one of the 206,506
facts lives in tenant `default`.** `ops` holds a single `labels` row and the
console's operator principals; `finance` appears nowhere in the database at
all. So the service as configured serves two tenants that hold no data,
while the corpus sits in a tenant it does not serve.

That is a configuration fork nobody has recorded a decision for — add
`default` to `SERVING_TENANTS`, or move the corpus into `ops` — and it is
NOT settled here. What is settled is that the symptom is a 404, not an empty
result set, so it reads like a missing op rather than a tenancy mismatch.

## Identity: resolved at the boundary, never asserted

`Authorization: Bearer <opaque token>` → S2's `OpenBaoCredentialResolver`
(sha256 digest → hub-owned `serving/principals/<digest>` registry) →
`Principal`. The request says WHAT; the principal determines what it may
SEE. A body field claiming a tenant is an unknown param → 400. Missing /
unknown / malformed / **revoked** credentials → 401 with a generic body
(never says whether the token exists). Post-auth enforcement failure
(grants unreachable) → 503 fail-closed.

## Connections: bounded by construction (Decision 7)

One shared process holds ONE `PostgresChokePoint` = ONE read-only serving
connection for every tenant (row-level tenancy — same posture as the S4
FactStore DSN-cache fix). No per-tenant connections, no raw connection
accessor anywhere in the stack (tested). psycopg3 serializes statements on
the shared connection under the threading server; `service.warm()` runs at
startup so the lazy connect never races the first burst.

**Tenancy-parameterized**: `build_serving_app(dsn=…, tenants=…)` takes the
connection target and tenant context as inputs — the future schema/
DB-per-tenant model (#3) runs the same builder once per tenant with a
per-tenant DSN. Shared now, splittable later, no rewrite.

## Usage instrumentation: serialization is the read (Decision 4a/4b)

Each served envelope is dumped THROUGH a `TrackedEnvelope` proxy inside a
per-request `UsageTracker`, so every request emits one `EnvelopeUsage` per
envelope — WHO read it, when, which fields were serialized, and which
uncertainty-state values were served.

Sink: **`PostgresUsageRecorder` by default** (migration 019 `serving_usage`,
+ `models.EnvelopeUsageRow` in lock-step). `SERVING_USAGE_SINK=jsonl` falls
back to the append-only file at `settings.serving_usage_log` — a deliberate
opt-out, never a silent degradation: the Postgres recorder verifies its
table at construction and REFUSES to start rather than dropping rows for the
life of the process. Inserts run on the recorder's own connection under
`kh_operator`, not the choke point's — which is SELECT-only by grant now and
physically cannot write this.

`principal_id` and `served_at` landed on `EnvelopeUsage` 2026-08-07 and are
what make the §8.8 positive half answerable. Before them the record could
not express attribution at all: `tenant_id` is shared by several principals,
so no amount of wiring would have told you which consumer read what.
`principal_id` is REQUIRED with no default — a log that is only sometimes
attributable cannot answer the question it exists to answer.

Insert failures are logged and swallowed, deliberately: a usage-log outage
must not become a serving outage. `check_usage_attribution` is what notices
a sink that has quietly stopped accepting rows.

## Latency: the §4 budget, enforced observably

Per-request wall time per endpoint feeds bounded rings (`LATENCY_WINDOW`
samples); `/v1/metrics` reports nearest-rank p50/p95/p99 (same estimator as
benchmark.py, so serving and benchmark numbers compare) plus
`within_budget` against `LATENCY_BUDGET_P95_MS = 300`.

## Running it

```
python -m knowledge_hub.service_http --tenant <tenant> [--host H --port P --dsn DSN]
```

Defaults from `config.py`: `serving_host` 127.0.0.1 (exposing beyond the
host is a deployment decision), `serving_port` 8080, `serving_tenants`
(comma-separated), `serving_usage_log`. All overridable via env
(`SERVING_HOST`, `SERVING_PORT`, `SERVING_TENANTS`, `SERVING_USAGE_LOG`).
Refuses to start if the serving connection won't warm.

## Explicitly NOT built (bookmarked, deferred)

* **No LLM-synthesis endpoint** — if a readable-summary need appears, use
  deterministic templating over envelopes first.
* **No `ask(free_text)` fuzzy entry point.**

Both are future ABOVE-the-choke-point consumers that would CALL declared
ops through this same surface; adding either changes nothing here.

## THE ISOLATION PROPERTY — three trusted DSN holders, not two

**Superseded the original §8.8 rider on 2026-08-07** after a read-only
inventory (Stage 0) measured what the rider had assumed. What it found:

* **Zero external agents hold a hub DSN.** Not "a few to migrate" — none
  exist. A workspace-wide sweep for DSN patterns hit only this repo, the
  staging kit's copy of it, and prose in handoff docs. The `Tax Agent`
  FactStore is its own file store and never touches Postgres; `rag-eval-kg`
  is Neo4j; `Hermes Agents` is an empty directory. There are no notebooks.
* **Not one real agent principal has ever been issued.** The registry held
  13 principals: 9 `operator`, and 4 role-less ones that are all smoke-test
  artifacts on tenant `_smoketest`.
* **The serving layer had never served a live envelope.**
  `serving_usage.jsonl` was 0 bytes.

So the rider's steps 1–4 had an empty work list, and step 5 verified
something that had not happened. Meanwhile the property those steps existed
to protect was void for a reason the rider never named: **the database made
no distinction between consumers at all.** Every process connected as the
same superuser `kh`, so "only the pipeline holds a DSN" was unenforceable
and `check_side_doors` could not fail. See the honest restatement below.

### The trusted set

Three processes may hold a Postgres DSN. Each is an enforced boundary in its
own right; the count is three because there are three boundaries, not
because three is convenient.

| Holder | Boundary it enforces | DB role |
|---|---|---|
| Pipeline (`PostgresFactStore`) | in-process; the only writer of domain data | `kh_pipeline` |
| Serving service (`PostgresChokePoint`, :8080) | S2 gate — label filter, read-only, per-request principal | `kh_serving` |
| Operator service (`operator_http`, :8081) | role-gated named writes, every attempt audited (migration 010) | `kh_operator` |

The operator console was the largest DSN holder outside the original two,
and reading the rider literally would have called it the biggest side door
in the system. That reading is wrong. The console is not an unenforced
consumer that slipped past the gate — it is a *second gate*, with its own
credential resolver, its own role scope, fixed named write operations, and
an audit trail that records refusals as well as successes. Its reads are
pipeline counters, queue listings and candidate-pair evidence, which are not
envelope-shaped and deliberately do not live on the choke point
(`operator_reads.py` header). Migrating it onto the read-only serving ops
would be impossible by shape and a downgrade by function.

**Anything outside those three is a side door.** That still includes the
reporting and corpus scripts, the verifier, and the deploy tooling — all of
which connect for legitimate reasons and none of which needs write access to
domain data. They move to `kh_report` (SELECT-only) rather than onto ops:
they read the *operational* tables, not the served surface, so authoring ops
for them would widen the serving surface to satisfy a spreadsheet.

### What makes the property real

1. **Distinct login roles, so the database can tell consumers apart.**
   `kh_pipeline` / `kh_serving` / `kh_operator` / `kh_report`, owned by
   `roles.ensure_serving_roles()` as idempotent DDL in the apply path. NOT a
   numbered migration: `CREATE ROLE` is cluster-level while migrations are
   per-database, and grants must re-run as new tables appear — the same
   reasoning that keeps `ensure_ledger` out of the numbered set.
2. **`kh_serving` holds SELECT and nothing else**, so the read-only promise
   is a grant the server enforces rather than a `SET default_transaction_
   read_only = on` the client sets on itself. The session GUC stays as belt
   and braces; it is no longer the only thing standing there.
3. **`check_side_doors` gets a real allowlist.** It was passing vacuously —
   its default allowlist is the DSN's own username, and every consumer used
   that one username, so every connection was allowlisted. It now takes the
   trusted role set and fails on a connection from anything else.
4. **Reads are attributable.** `EnvelopeUsage` carries `principal_id` and
   `served_at`, and the durable sink is a Postgres table (migration 019),
   so "the agents' reads flow through ops" is a query, not a claim.

### Done means

* `pg_stat_activity` shows only the four roles above, each on its expected
  process — and `check_side_doors` demonstrably FAILS when that is untrue.
* No consumer connects as a superuser.
* A named principal's reads are attributable in `serving_usage`.
* The trusted set is three processes, and adding a fourth is a decision
  someone has to write down here — not something a new DSN does quietly.

## Tests

`tests/test_service_http.py` (13 tests; real Postgres + real OpenBao + live
bge-m3 + a real threading server on an ephemeral port; full suite 173
green): every registered op/composite + retrieval reachable with validated
S1 envelope JSON; no unregistered/raw surface (probed); registry change →
endpoint appears on rebuild and nothing else changes; unauthenticated /
unknown / malformed / revoked credentials 401 generic; identity
unassertable via body; identical-data cross-tenant isolation end-to-end
over HTTP on every surface incl. composite steps; above-grant labels
invisible over HTTP, granted role sees them; composite per-step tags +
trace (params raw text, never vectors); usage log records serialized
fields + served states per envelope; zero backend growth across 30 calls
by 10 tenants; latency percentiles + error counts on /v1/metrics; health
reports components + installed version; check_stack version-integrity
logic green on the real install and failing on drift.

check_stack.py: `version integrity` now runs FIRST (editable-install drift
mislabels benchmark provenance — bit twice); `serving service (S5)` is the
closing check (assemble → health → 401 without credential → gated HTTP
round-trip). Package 0.11.0 (pyproject + `__init__` + editable reinstall,
`importlib.metadata` verified — and now check_stack catches drift itself).
