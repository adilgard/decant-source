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
envelope — fields serialized + uncertainty-state values served. Sink:
`JsonlUsageRecorder` (append-only JSONL, `settings.serving_usage_log`).
A Postgres usage table is the bookmarked production sink — that lands as a
migration + models.py in the same commit (SERVING_NOTES rule).

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

## RIDER — required follow-up: migrate the already-acting agents

The agents already acting on hub data must move onto this service as their
**single read path**. Their old direct-Postgres queries are the side door:
any consumer holding a DSN bypasses the choke point entirely and voids the
isolation property this whole layer exists to guarantee. Scope:

1. Inventory every external consumer holding a Postgres DSN for the hub DB
   (agent configs, connection strings in env files, notebook snippets).
2. Provision each a serving credential (`OpenBaoCredentialResolver.
   register_principal`) with the roles its labels require.
3. Replace direct SQL with `POST /v1/ops/*` / `POST /v1/retrieve` calls; if
   a query has no registered op, AUTHOR the op (OPERATIONS_NOTES workflow) —
   do not widen the surface ad hoc.
4. Revoke the agents' DB credentials. **Done = no external consumer keeps
   direct DB access**; the only remaining DSN holders are the internal
   pipeline (PostgresFactStore) and the service itself.
5. Verify: `pg_stat_activity` shows no non-pipeline clients; the usage log
   shows the agents' reads.

This is scoped here, not executed here.

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
