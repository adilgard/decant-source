"""API service — the enforcement boundary made physical (Build Prompt S5).

Implements the S1 `ServingService` seam AND the HTTP+JSON surface in front of
it. Its reason for being a SERVICE and not a library is Decision 6: agents
live OUTSIDE the enforcement boundary. A library an agent imports could reach
around the choke point or self-assert identity; a service physically can't be
reached around — the caller holds an opaque credential and an HTTP client,
nothing else. The service IS the boundary.

WHAT DISPATCHES WHERE (nothing new is built here — S1–S4 are assembled):

  * POST /v1/ops/<name>  -> InProcessOperationCatalog.execute (S3). One
    endpoint per REGISTERED op/composite — the route set is generated from
    the registry, so adding an op is a registry change + redeploy, and no
    ad-hoc query / raw-SQL endpoint exists to generate. An unregistered or
    out-of-scope name answers 404 (UnknownOperation — the absence rule
    applied to the URL space).
  * POST /v1/retrieve    -> DenseRetrievalService.retrieve (S4), with the
    `enrich` knob exposed as a caller param.
  * GET  /v1/ops         -> the catalog as THIS principal sees it
    (scope-filtered public views; SQL never leaves the authoring side).
  * GET  /v1/health, /v1/metrics -> unauthenticated operational surface;
    they carry component booleans and latency aggregates, never tenant data.

IDENTITY IS RESOLVED AT THE BOUNDARY, NEVER ASSERTED. The caller presents an
opaque bearer token; `CredentialResolver.resolve_principal` (S2's OpenBao
registry, keyed by sha256 digest) turns it into a Principal server-side.
Request payloads carry WHAT is asked, never who is asking — a body field
claiming a tenant is simply an unknown param. Unresolvable / revoked /
malformed credentials answer 401 with a generic body (fail-closed, and the
error never says whether the token exists).

EVERY DISPATCHED QUERY TRANSITS THE S2 GATE because both dispatch targets
already enforce->gate internally; this module holds a PostgresChokePoint (for
warmup/health) and components that hold one — never a connection, and no
accessor exists to leak one. Connections stay bounded by construction: ONE
choke point = ONE serving connection for every tenant (row-level tenancy;
the S4 FactStore DSN-cache fix is the same posture on the internal path).

ONE SHARED INSTANCE, TENANCY-PARAMETERIZED: `build_serving_app()` takes the
DSN and the tenant list as inputs. Today one shared process serves all
pilot tenants; when the schema/DB-per-tenant model lands, the same builder
runs per-tenant instances with per-tenant DSNs — no rewrite.

USAGE INSTRUMENTATION (Decision 4a/4b): serialization is the read. Each
served envelope is dumped THROUGH a `TrackedEnvelope` proxy inside a
per-request `UsageTracker`, so every request emits one `EnvelopeUsage` per
envelope — fields serialized + uncertainty-state values served. That is the
strip-later evidence: today everything is served maximal; a field is
stripped only when these logs show non-use. `JsonlUsageRecorder` is the
durable sink (append-only JSONL; a Postgres table is a bookmarked follow-up
— schema change + models.py lock-step per SERVING_NOTES).

LATENCY IS OBSERVED PER REQUEST: wall time per endpoint feeds bounded ring
buffers; /v1/metrics reports p50/p95/p99 against the §4 budget
(p95 <= 300ms, the same gate the Axis-C rounds enforced) — the budget is
enforced observably, not aspirationally.

Explicitly NOT here (bookmarked, deferred): no LLM-synthesis endpoint and no
ask(free_text) fuzzy entry. Both are future ABOVE-the-choke-point consumers
that would CALL declared ops through this same surface; nothing about this
module needs to change to add them, and neither is built.

Keep in lock-step with serving.py (payload = envelope shapes as-is),
operations.py (dispatch + catalog views), choke_point.py (identity), and
config.py (serving_* settings).
"""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import logging
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping, Optional

# psycopg is already a hard dependency via the choke point; "stdlib-only"
# above is about the HTTP framework (there isn't one), not the DB driver.
from psycopg.types.json import Jsonb

from knowledge_hub.choke_point import (
    CredentialResolver,
    EnforcementRefused,
    OpenBaoCredentialResolver,
    PostgresChokePoint,
    PrincipalUnresolvable,
)
from knowledge_hub.config import settings
from knowledge_hub.interfaces import Embedder
from knowledge_hub.operations import (
    CompositeResult,
    InProcessOperationCatalog,
    OperationCallError,
    OperationRejected,
    register_serving_defaults,
)
from knowledge_hub.retrieval import DenseRetrievalService
from knowledge_hub.serving import (
    EvidenceEnvelope,
    FactEnvelope,
    InMemoryUsageRecorder,
    Operation,
    Principal,
    RetrievalQuery,
    ServingResponse,
    ServingService,
    UnknownOperation,
    UsageRecorder,
    UsageTracker,
)

# The §4 latency budget the Axis-C rounds enforced on retrieval, now applied
# to the serving surface it was written for: per-request p95 <= 300ms.
LATENCY_BUDGET_P95_MS = 300

# How many recent wall-time samples each endpoint keeps for the percentile
# window (bounded by design — observability may never grow without bound).
LATENCY_WINDOW = 2048

_JSON = "application/json"

logger = logging.getLogger(__name__)


class RawResponse:
    """A non-JSON response body for the shared HTTP adapter (the operator
    UI's static files). Everything else stays a JSON dict."""

    __slots__ = ("content_type", "body")

    def __init__(self, content_type: str, body: bytes):
        self.content_type = content_type
        self.body = body


def installed_version() -> str:
    """The version of the INSTALLED knowledge_hub distribution (what
    benchmark provenance records) — surfaced on /v1/health so drift from the
    source tree is observable, not silent."""
    try:
        return importlib.metadata.version("knowledge_hub")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


# ------------------------------------------------------- latency percentiles --
class LatencyStats:
    """Per-endpoint wall-time aggregation: bounded ring per endpoint,
    nearest-rank percentiles (same estimator as benchmark.py, so serving
    numbers and benchmark numbers stay comparable). Thread-safe — requests
    record concurrently under the threading server."""

    def __init__(self, window: int = LATENCY_WINDOW,
                 budget_p95_ms: float = LATENCY_BUDGET_P95_MS):
        self._lock = threading.Lock()
        self._window = window
        self._budget = budget_p95_ms
        self._samples: dict[str, deque[float]] = {}
        self._errors: dict[str, int] = {}

    def record(self, endpoint: str, wall_ms: float, *,
               error: bool = False) -> None:
        with self._lock:
            self._samples.setdefault(
                endpoint, deque(maxlen=self._window)).append(wall_ms)
            if error:
                self._errors[endpoint] = self._errors.get(endpoint, 0) + 1

    @staticmethod
    def _pct(ordered: list[float], p: float) -> float:
        return ordered[min(int(p * len(ordered)), len(ordered) - 1)]

    def snapshot(self) -> dict[str, Any]:
        """The /v1/metrics body: per-endpoint p50/p95/p99 + counts against
        the §4 budget. Aggregates only — never request payloads."""
        with self._lock:
            endpoints: dict[str, Any] = {}
            for endpoint, samples in sorted(self._samples.items()):
                ordered = sorted(samples)
                p95 = self._pct(ordered, 0.95)
                endpoints[endpoint] = {
                    "count": len(ordered),
                    "errors": self._errors.get(endpoint, 0),
                    "p50_ms": round(self._pct(ordered, 0.50), 2),
                    "p95_ms": round(p95, 2),
                    "p99_ms": round(self._pct(ordered, 0.99), 2),
                    "within_budget": p95 <= self._budget,
                }
        return {"budget_p95_ms": self._budget, "endpoints": endpoints}


# ------------------------------------------------------- durable usage sink --
class JsonlUsageRecorder(UsageRecorder):
    """Durable-enough usage sink for the pilot: one EnvelopeUsage per line,
    append-only JSONL. Fire-and-forget cheap (open handle, write, flush) —
    instrumentation may never slow serving. A Postgres usage table is the
    bookmarked production sink (migration + models.py lock-step, per the
    SERVING_NOTES rule about new tables)."""

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._fh = self._path.open("a", encoding="utf-8")

    def record(self, usage) -> None:
        line = json.dumps(usage.model_dump(mode="json"),
                          separators=(",", ":"))
        with self._lock:
            self._fh.write(line + "\n")
            self._fh.flush()

    def close(self) -> None:
        with self._lock:
            if not self._fh.closed:
                self._fh.close()


class PostgresUsageRecorder(UsageRecorder):
    """The production usage sink (migration 019) — the bookmarked one, now
    built, because the §8.8 verification story needs to QUERY these records
    and a JSONL file cannot answer "did principal X read through ops in the
    last hour" without something parsing the whole file.

    Writes on its OWN connection, deliberately not the choke point's. Two
    reasons: the choke point is SELECT-only at the grant level now
    (kh_serving), so it physically cannot write this; and instrumentation
    that shares the serving connection would serialize behind served
    queries on the same psycopg connection, which is exactly the "may never
    slow serving" rule this class inherits.

    Failure is LOGGED AND SWALLOWED. That is the uncomfortable call, so it
    is written down: a usage-log outage must not turn into a serving
    outage — losing the strip-later evidence for a few requests is
    recoverable, refusing to answer a caller because bookkeeping is down is
    not. The check (`check_usage_attribution`) is what notices a sink that
    has silently stopped accepting rows.
    """

    def __init__(self, dsn: Optional[str] = None):
        import psycopg

        # The OPERATOR role: this is a write, and it is not domain data
        # written by the ingest path, so it should not present as the
        # pipeline in pg_stat_activity.
        self._dsn = dsn or settings.operator_dsn
        self._lock = threading.Lock()
        self._conn = psycopg.connect(self._dsn, autocommit=True,
                                     connect_timeout=10)
        # Verified at CONSTRUCTION, not discovered at first write. record()
        # swallows failures by design, so a missing table would otherwise
        # mean every usage row is silently dropped for the life of the
        # process — the exact silence this whole section exists to remove.
        if not self._conn.execute(
                "SELECT 1 FROM pg_tables WHERE schemaname='public'"
                " AND tablename='serving_usage'").fetchone():
            self._conn.close()
            raise RuntimeError(
                "serving_usage table is missing — migration 019 has not "
                "been applied to this database. Run `khctl apply`, or set "
                "SERVING_USAGE_SINK=jsonl to fall back to the file sink "
                "deliberately rather than by accident.")

    def record(self, usage) -> None:
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO serving_usage (request_id, tenant_id,"
                    " principal_id, envelope_kind, envelope_key,"
                    " fields_read, states_branched, served_at)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (usage.request_id, usage.tenant_id, usage.principal_id,
                     usage.envelope_kind, usage.envelope_key,
                     Jsonb(usage.fields_read), Jsonb(usage.states_branched),
                     usage.served_at))
        except Exception:
            logger.exception(
                "usage record dropped for principal %r request %r — the "
                "sink is not accepting rows; serving continues",
                usage.principal_id, usage.request_id)

    def close(self) -> None:
        with self._lock:
            if not self._conn.closed:
                self._conn.close()


def _dump_tracked(tracker: UsageTracker,
                  env: FactEnvelope | EvidenceEnvelope) -> dict[str, Any]:
    """Serialize one envelope AS-IS (S1 shape, pydantic JSON mode) while
    recording the serve through the tracker: over HTTP, serialization IS the
    field read the strip-later logs need, and reading `state` through the
    proxy records the state value served (the branch evidence)."""
    proxy = tracker.track(env)
    for name in type(env).model_fields:
        getattr(proxy, name)
    return env.model_dump(mode="json")


# ---------------------------------------------------------------- the service --
class KnowledgeHubServingService(ServingService):
    """The S1 `ServingService` seam, assembled from S2–S4: resolve the
    operation (S3 catalog), enforce (S2 — inside every dispatch target),
    retrieve/look up (S4), wrap envelopes, flush usage — one request_id
    across all of it. Holds the choke point and the components; never a
    connection, and offers no accessor to one."""

    def __init__(self, choke: PostgresChokePoint,
                 catalog: InProcessOperationCatalog,
                 retrieval: DenseRetrievalService,
                 recorder: Optional[UsageRecorder] = None):
        self._choke = choke
        self._catalog = catalog
        self._retrieval = retrieval
        self._recorder = recorder or InMemoryUsageRecorder()

    @property
    def catalog(self) -> InProcessOperationCatalog:
        """The registry this service exposes — the public op surface (route
        generation + catalog listing read it; there is nothing else to
        reach)."""
        return self._catalog

    # ------------------------------------------------------------- S1 seam
    def execute(self, operation: str, params: Mapping[str, Any],
                principal: Principal) -> ServingResponse:
        return self._serve(operation, params, principal)[1]

    def operations(self, principal: Principal) -> list[Operation]:
        return self._catalog.list_for(principal)

    # -------------------------------------------------------- serve payloads
    def execute_payload(self, operation: str, params: Mapping[str, Any],
                        principal: Principal) -> dict[str, Any]:
        """One served op/composite as the HTTP JSON body: S1 envelopes
        as-is; composites keep per-step tags + the execution trace."""
        return self._serve(operation, params, principal)[0]

    def retrieve_payload(self, text: str, k: int, enrich: bool,
                         principal: Principal, *,
                         include_retracted: bool = False) -> dict[str, Any]:
        """The retrieval endpoint's body: S4's DenseRetrievalService with the
        `enrich` knob mapped to a caller param. `include_retracted` is the
        temporal audit escape (migration 009): evidence from retracted
        documents serves only when explicitly asked for."""
        request_id = uuid.uuid4().hex
        started = time.perf_counter()
        envelopes = self._retrieval.retrieve(
            RetrievalQuery(text=text, k=k,
                           include_retracted=include_retracted),
            principal, enrich=enrich)
        tracker = UsageTracker(self._recorder, request_id,
                               principal.tenant_id, principal.principal_id)
        evidence = [_dump_tracked(tracker, env) for env in envelopes]
        tracker.flush()
        return {
            "request_id": request_id,
            "tenant_id": principal.tenant_id,
            "operation": "retrieve",
            "returns": "evidence",
            "facts": [],
            "evidence": evidence,
            "served_at": datetime.now(timezone.utc).isoformat(),
            "wall_ms": int((time.perf_counter() - started) * 1000),
        }

    # -------------------------------------------------------------- plumbing
    def _serve(self, operation: str, params: Mapping[str, Any],
               principal: Principal
               ) -> tuple[dict[str, Any], ServingResponse]:
        request_id = uuid.uuid4().hex
        started = time.perf_counter()
        result = self._catalog.execute(operation, dict(params or {}),
                                       principal)
        tracker = UsageTracker(self._recorder, request_id,
                               principal.tenant_id, principal.principal_id)

        facts: list[FactEnvelope] = []
        evidence: list[EvidenceEnvelope] = []
        extra: dict[str, Any] = {}
        if isinstance(result, CompositeResult):
            # Per-step envelopes stay tagged (facts as facts, evidence as
            # evidence — never flattened in the payload); the S1
            # ServingResponse below carries the flattened union for
            # programmatic callers of the seam.
            returns = "composite"
            steps = []
            for step in result.steps:
                facts.extend(step.facts)
                evidence.extend(step.evidence)
                steps.append({
                    "step": step.step,
                    "op": step.op,
                    "returns": step.returns,
                    "facts": [_dump_tracked(tracker, f) for f in step.facts],
                    "evidence": [_dump_tracked(tracker, e)
                                 for e in step.evidence],
                })
            extra["steps"] = steps
            extra["trace"] = [t.model_dump(mode="json") for t in result.trace]
            payload_facts: list[dict] = []
            payload_evidence: list[dict] = []
        else:
            returns = self._catalog.returns_of(principal.tenant_id, operation)
            if returns == "facts":
                facts = list(result)
            else:
                evidence = list(result)
            payload_facts = [_dump_tracked(tracker, f) for f in facts]
            payload_evidence = [_dump_tracked(tracker, e) for e in evidence]

        tracker.flush()
        served_at = datetime.now(timezone.utc)
        wall_ms = int((time.perf_counter() - started) * 1000)
        response = ServingResponse(
            request_id=request_id, tenant_id=principal.tenant_id,
            operation=operation, facts=facts, evidence=evidence,
            served_at=served_at, wall_ms=wall_ms)
        payload = {
            "request_id": request_id,
            "tenant_id": principal.tenant_id,
            "operation": operation,
            "returns": returns,
            "facts": payload_facts,
            "evidence": payload_evidence,
            **extra,
            "served_at": served_at.isoformat(),
            "wall_ms": wall_ms,
        }
        return payload, response

    # ---------------------------------------------------------------- health
    def warm(self) -> bool:
        """Open the single serving connection + cache the public label id by
        running one real (empty-scope) enforcement pass — so the lazy
        connect happens once, at startup, not racing under the first
        concurrent requests."""
        try:
            self._choke.enforce(
                RetrievalQuery(text="warmup"),
                Principal(tenant_id="_warmup", principal_id="_warmup",
                          roles=[]))
            return True
        except EnforcementRefused:
            return False


# ------------------------------------------------------------------- the app --
class ServingApp:
    """The HTTP core, framework-free and transport-separable: `handle()`
    maps (method, path, headers, body) -> (status, JSON body). The socket
    layer below is a thin adapter, so the whole boundary — routing, auth,
    dispatch, error mapping — is plain auditable code with zero framework
    magic between the request and the gate."""

    def __init__(self, service: KnowledgeHubServingService,
                 resolver: CredentialResolver, *,
                 stats: Optional[LatencyStats] = None):
        self.service = service
        self._resolver = resolver
        self.stats = stats or LatencyStats()

    # ------------------------------------------------------------ route table
    def endpoints(self, tenant_id: str) -> list[str]:
        """The generated route set for one tenant: exactly one POST per
        registered op/composite, plus the fixed retrieval/catalog/ops
        surface. This IS the registry, spelled as URLs — nothing else
        answers."""
        generated = [f"POST /v1/ops/{op.name}"
                     for op in self.service.catalog.list_ops(tenant_id)]
        return sorted(generated) + [
            "POST /v1/retrieve",
            "GET /v1/ops",
            "GET /v1/health",
            "GET /v1/metrics",
        ]

    # -------------------------------------------------------------- dispatch
    def handle(self, method: str, path: str, headers: Mapping[str, Any],
               body: bytes) -> tuple[int, dict[str, Any]]:
        try:
            return self._route(method, path.rstrip("/") or "/", headers, body)
        except Exception:
            # Fail closed and quiet: internals never leak into a response.
            return 500, {"error": "internal"}

    def _route(self, method: str, path: str, headers: Mapping[str, Any],
               body: bytes) -> tuple[int, dict[str, Any]]:
        if method == "GET" and path == "/v1/health":
            return self._health()
        if method == "GET" and path == "/v1/metrics":
            return 200, self.stats.snapshot()

        # Everything past this line answers a caller about tenant data:
        # authenticate FIRST, resolve server-side, fail closed.
        try:
            principal = self._principal(headers)
        except PrincipalUnresolvable:
            # Generic on purpose: the body never says whether the token
            # exists, is revoked, or is malformed.
            return 401, {"error": "unauthorized"}

        if method == "GET" and path == "/v1/ops":
            ops = self.service.operations(principal)
            return 200, {"tenant_id": principal.tenant_id,
                         "operations": [op.model_dump(mode="json")
                                        for op in ops]}
        if method == "POST" and path == "/v1/retrieve":
            return self._retrieve(body, principal)
        if method == "POST" and path.startswith("/v1/ops/"):
            name = path[len("/v1/ops/"):]
            if "/" in name or not name:
                return 404, {"error": "not_found"}
            return self._execute(name, body, principal)
        return 404, {"error": "not_found"}

    # ---------------------------------------------------------------- pieces
    def _principal(self, headers: Mapping[str, Any]) -> Principal:
        auth = headers.get("Authorization") or headers.get("authorization")
        if not isinstance(auth, str) or not auth.startswith("Bearer "):
            raise PrincipalUnresolvable("no bearer credential presented")
        return self._resolver.resolve_principal(auth[len("Bearer "):].strip())

    @staticmethod
    def _json_object(body: bytes) -> dict[str, Any]:
        if not body:
            return {}
        parsed = json.loads(body.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("request body must be a JSON object")
        return parsed

    def _execute(self, name: str, body: bytes,
                 principal: Principal) -> tuple[int, dict[str, Any]]:
        started = time.perf_counter()
        status, payload = 500, {"error": "internal"}
        try:
            params = self._json_object(body)
        except (ValueError, UnicodeDecodeError) as e:
            return 400, {"error": "bad_request", "detail": str(e)}
        try:
            payload = self.service.execute_payload(name, params, principal)
            status = 200
        except UnknownOperation:
            # Unregistered AND out-of-scope answer identically: the op is
            # invisible, not forbidden (absence rule, catalog edition).
            status, payload = 404, {"error": "unknown_operation",
                                    "operation": name}
        except OperationCallError as e:
            status, payload = 400, {"error": "bad_request", "detail": str(e)}
        except OperationRejected:
            status, payload = 500, {"error": "internal"}
        except EnforcementRefused:
            # Post-auth enforcement failure (e.g. grants unreachable):
            # server-side fail-closed, not a caller error.
            status, payload = 503, {"error": "enforcement_refused"}
        finally:
            self.stats.record(f"op:{name}",
                              (time.perf_counter() - started) * 1000,
                              error=status >= 400)
        return status, payload

    def _retrieve(self, body: bytes,
                  principal: Principal) -> tuple[int, dict[str, Any]]:
        started = time.perf_counter()
        status, payload = 500, {"error": "internal"}
        try:
            params = self._json_object(body)
        except (ValueError, UnicodeDecodeError) as e:
            return 400, {"error": "bad_request", "detail": str(e)}
        query = params.get("query")
        k = params.get("k", 10)
        enrich = params.get("enrich", False)
        include_retracted = params.get("include_retracted", False)
        unknown = set(params) - {"query", "k", "enrich", "include_retracted"}
        try:
            if unknown:
                raise ValueError(f"unknown param(s) {sorted(unknown)}")
            if not isinstance(query, str) or not query.strip():
                raise ValueError("param 'query' must be a non-empty string")
            if not isinstance(k, int) or isinstance(k, bool):
                raise ValueError("param 'k' must be an int")
            if not isinstance(enrich, bool):
                raise ValueError("param 'enrich' must be a bool")
            if not isinstance(include_retracted, bool):
                raise ValueError("param 'include_retracted' must be a bool")
            payload = self.service.retrieve_payload(
                query, k, enrich, principal,
                include_retracted=include_retracted)
            status = 200
        except ValueError as e:
            status, payload = 400, {"error": "bad_request", "detail": str(e)}
        except EnforcementRefused:
            status, payload = 503, {"error": "enforcement_refused"}
        finally:
            self.stats.record("retrieve",
                              (time.perf_counter() - started) * 1000,
                              error=status >= 400)
        return status, payload

    def _health(self) -> tuple[int, dict[str, Any]]:
        postgres_ok = self.service.warm()
        # F1: sealed ≠ unreachable ≠ ok. `vault` stays a bool (sealed =
        # False — a sealed vault refuses every credential); `vault_status`
        # carries the distinction callers branch on.
        status_fn = getattr(self._resolver, "status", None)
        if callable(status_fn):
            vault_status = status_fn()
        else:
            ping = getattr(self._resolver, "ping", None)
            vault_status = ("ok" if (bool(ping()) if callable(ping)
                                     else True) else "unreachable")
        vault_ok = vault_status == "ok"
        ok = postgres_ok and vault_ok
        return (200 if ok else 503), {
            "status": "ok" if ok else "degraded",
            "version": installed_version(),
            "postgres": postgres_ok,
            "vault": vault_ok,
            "vault_status": vault_status,
        }


# ------------------------------------------------------------ HTTP transport --
class _Handler(BaseHTTPRequestHandler):
    """Thin socket adapter over ServingApp.handle(). All decisions live in
    the app; this class only reads bytes and writes JSON."""

    app: ServingApp  # bound by make_server via a subclass attribute
    protocol_version = "HTTP/1.1"

    def _dispatch(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        status, payload = self.app.handle(self.command, self.path,
                                          self.headers, body)
        if isinstance(payload, RawResponse):
            content_type, data = payload.content_type, payload.body
        else:
            content_type = _JSON
            data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = _dispatch
    do_POST = _dispatch

    def log_message(self, format: str, *args: Any) -> None:
        # Quiet by design: /v1/metrics and the usage log are the
        # observability surface; stderr chatter is neither durable nor
        # aggregated.
        pass


def make_server(app: ServingApp, host: str = "127.0.0.1",
                port: int = 8080) -> ThreadingHTTPServer:
    """A threading HTTP server over the app. Concurrency note: psycopg3
    connections serialize statements internally, so concurrent requests
    share the ONE serving connection safely — call service.warm() before
    serving so the lazy connect isn't racing the first burst."""
    handler = type("KnowledgeHubServingHandler", (_Handler,), {"app": app})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


# ------------------------------------------------------------------ assembly --
def build_serving_app(*, dsn: Optional[str] = None,
                      tenants: tuple[str, ...] | list[str] = (),
                      resolver: Optional[CredentialResolver] = None,
                      embedder: Optional[Embedder] = None,
                      recorder: Optional[UsageRecorder] = None) -> ServingApp:
    """Assemble the shared serving instance. Tenancy-parameterized: the DSN
    and the tenant list are inputs, so the future schema/DB-per-tenant model
    (#3) runs this same builder once per tenant with a per-tenant DSN —
    shared now, splittable later, no rewrite either way."""
    if embedder is None:
        from knowledge_hub.embedding_ollama import OllamaEmbedder
        embedder = OllamaEmbedder()
    if resolver is None:
        # d.s Stage 3: posture picks the implementation (local file vs OpenBao).
        # The choke point downstream is unchanged either way — this decides
        # where an identity is looked up, never whether one is enforced.
        from knowledge_hub.credentials import make_credential_resolver
        resolver = make_credential_resolver()
    choke = PostgresChokePoint(dsn=dsn, resolver=resolver)
    catalog = InProcessOperationCatalog(choke, embedder)
    for tenant_id in tenants:
        register_serving_defaults(catalog, tenant_id)
    retrieval = DenseRetrievalService(choke, embedder, catalog)
    if recorder is None:
        # Default POSTGRES: the attribution query is the point, and a file
        # cannot answer it. `SERVING_USAGE_SINK=jsonl` is the deliberate
        # opt-out — chosen, never fallen into (the Postgres sink refuses to
        # construct rather than degrading quietly).
        if settings.serving_usage_sink == "jsonl":
            recorder = JsonlUsageRecorder(settings.serving_usage_log)
        else:
            recorder = PostgresUsageRecorder()
    service = KnowledgeHubServingService(choke, catalog, retrieval, recorder)
    return ServingApp(service, resolver)


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        description="Knowledge Hub serving service — the enforced door")
    parser.add_argument("--host", default=settings.serving_host)
    parser.add_argument("--port", type=int, default=settings.serving_port)
    parser.add_argument("--dsn", default=None,
                        help="Postgres DSN (default: settings.postgres_dsn)")
    parser.add_argument("--tenant", action="append", default=None,
                        help="tenant to register the default op surface for"
                             " (repeatable; default: settings.serving_tenants)")
    args = parser.parse_args(argv)

    # d.s Stage 1: the posture goes out BEFORE anything is built, so it is on
    # screen even if assembly then fails.
    from knowledge_hub.config import print_posture_banner
    print_posture_banner()

    tenants = args.tenant if args.tenant else [
        t.strip() for t in settings.serving_tenants.split(",") if t.strip()]
    app = build_serving_app(dsn=args.dsn, tenants=tenants)
    if not app.service.warm():
        raise SystemExit(
            "serving connection failed to warm — refusing to start blind")
    server = make_server(app, args.host, args.port)
    routes = app.endpoints(tenants[0]) if tenants else ["(no tenants registered)"]
    print(f"knowledge_hub serving {installed_version()} on "
          f"http://{args.host}:{args.port} — tenants={tenants or '[]'}, "
          f"{len(routes)} routes, p95 budget {LATENCY_BUDGET_P95_MS}ms")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
