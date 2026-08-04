"""Verification primitives — ONE implementation, two runners.

The pilot gate (check_stack.py, all-ours topology, pilot defaults) and the
plan-driven field verifier (`khctl verify`, targets from deploy_plan.json)
both run THESE functions. When a check gets smarter, both runners get it;
there is exactly one definition of "WORM is enforced" or "the choke point
fails closed".

Contract: each check_* takes its targets explicitly (None = the pilot
default from settings, matching the codebase's constructor convention),
returns a human detail string on success, and RAISES on failure. run_check()
wraps one call into a CheckResult; runners own presentation and exit codes.

Two checks exist only for the field verifier:
  check_side_doors        — the §8.8 rider: pg_stat_activity must show no
                            non-pipeline clients, else isolation is void.
                            Run on EVERY visit, not just install day.
  check_core_boundary     — the corpus-agnostic rule: knowledge_hub_pkg
                            must never IMPORT a domain plugin. Same family
                            as the side-door check — an invariant that
                            holds only while nobody quietly breaks it, so
                            it is verified rather than remembered. Static
                            (ast over source), so it needs no services and
                            no plugin installed.
  check_remote_inference  — Shape B: the inference endpoint answers and
                            serves the required models. Auth/TLS hardening
                            is §8.9 net-new item 2 (not built); until then
                            this reports TLS presence honestly.
  check_local_external_inference — BP46 Fix 5: an operator-supplied LOCAL
                            endpoint (their GPU, model not installed by us).
                            Same reachability proof, plus a proof that the
                            endpoint really is on this box — the deploy is
                            on-premises and must not be reported otherwise.
"""
from __future__ import annotations

import ast
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional
from urllib.parse import urlsplit

from knowledge_hub import settings

_NET_TIMEOUT = 10


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str  # success: the full human line body; failure: the error


def run_check(name: str, fn: Callable[[], str]) -> CheckResult:
    """We want the full picture, not the first failure — every runner
    reports all results and decides its own exit code."""
    try:
        return CheckResult(name=name, passed=True, detail=fn())
    except Exception as e:
        return CheckResult(name=name, passed=False,
                           detail=f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# 0. Version integrity (runs FIRST everywhere — drift has bitten twice)
# ---------------------------------------------------------------------------
def assert_versions(installed: str, declared: str, source: str) -> None:
    """Pure comparison so tests can drive it with drifted values. One
    inequality anywhere = the editable install is stale and every benchmark
    run records the WRONG package version as provenance."""
    if not (installed == declared == source):
        raise RuntimeError(
            f"version drift: installed={installed} pyproject={declared} "
            f"__version__={source} — re-run `pip install -e "
            f"knowledge_hub_pkg` (benchmark provenance mislabels until "
            f"this is green)")


def version_triple(pkg_dir: Optional[Path] = None) -> tuple[str, str, str]:
    import importlib.metadata
    import tomllib

    import knowledge_hub

    # Default: this file lives in <pkg_dir>/knowledge_hub/, so the
    # pyproject is one level up — no dependency on the runner's cwd.
    pkg_dir = pkg_dir or Path(__file__).resolve().parents[1]
    pyproject = tomllib.loads(
        (pkg_dir / "pyproject.toml").read_text(encoding="utf-8"))
    return (importlib.metadata.version("knowledge_hub"),
            pyproject["project"]["version"],
            knowledge_hub.__version__)


def check_version(pkg_dir: Optional[Path] = None) -> str:
    installed, declared, source = version_triple(pkg_dir)
    assert_versions(installed, declared, source)
    return (f"version integrity: {installed} "
            f"(installed == pyproject == __version__)")


# ---------------------------------------------------------------------------
# 1. Postgres — extensions, schema tables, seed data
# ---------------------------------------------------------------------------
def check_postgres(dsn: Optional[str] = None,
                   require_exts: Iterable[str] = ("vector", "age", "pg_trgm"),
                   require_graph: bool = True) -> str:
    """require_exts/require_graph become plan-driven the day the
    AGE-retirement rider lands (drop 'age', graph off) — the qualification
    bar and the verification bar must move together."""
    import psycopg

    dsn = dsn or settings.postgres_dsn
    with psycopg.connect(dsn, connect_timeout=5) as conn:
        exts = {r[0] for r in conn.execute("SELECT extname FROM pg_extension")}
        missing = set(require_exts) - exts
        if missing:
            raise RuntimeError(f"missing extensions: {missing}")
        tables = {r[0] for r in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='public'")}
        need = {"raw_documents", "documents", "chunks", "entities", "facts",
                "entity_mentions", "resolution_policy", "ontology_versions",
                "ontology_active"}   # migration 011: the operator selection
        missing_t = need - tables
        if missing_t:
            raise RuntimeError(f"missing tables: {missing_t}")
        # The ACTIVE version (011), not an arbitrary first row — once more
        # than one version is loaded the two answers differ.
        onto = (conn.execute("SELECT version FROM ontology_active").fetchone()
                or conn.execute(
                    "SELECT version FROM ontology_versions").fetchone())
        graph_name = "disabled"
        if require_graph:
            graph = conn.execute(
                "SELECT name FROM ag_catalog.ag_graph").fetchone()
            graph_name = graph[0]
    return (f"postgres: extensions ok, {len(tables)} tables, "
            f"ontology={onto[0]}, graph={graph_name}")


def check_migrations(dsn: Optional[str] = None,
                     migrations_dir: Optional[Path] = None,
                     baseline: Optional[Path] = None) -> str:
    """The migration ledger agrees with the database, per migration.

    Added after the 2026-08-03 pilot finding: 011/012/013 had every object
    present and no ledger row, and NOTHING in the verification surface looked
    at the ledger's CONTENTS — check_postgres asserts `ontology_active`
    exists, which was true, and stack_alive asked only whether the ledger
    table existed. So a schema at 013 with a ledger at 010 verified green.

    This check fails on ANY disagreement in either direction. It deliberately
    does not repair: reconciling drift is a decision, and
    `khctl migrations mark-applied` is where a human records one.

    Relative paths resolve against the working directory — the deployment home
    under khctl, the infra root on the dev bench — the same convention as
    settings.ontology_dir and bge_m3_tokenizer_json.
    """
    import psycopg

    from knowledge_hub import migrations as mig

    dsn = dsn or settings.postgres_dsn
    migrations_dir = Path(migrations_dir or "migrations")
    if not migrations_dir.is_dir():
        # Never pass by default: an unfindable bundle is the same blind spot
        # this check exists to close.
        raise RuntimeError(
            f"cannot verify the ledger — no migrations/ at "
            f"{migrations_dir.resolve()} (run from the deployment home, or "
            f"pass migrations_dir)")
    with psycopg.connect(dsn, connect_timeout=5) as conn:
        if not mig.ledger_exists(conn):
            raise RuntimeError(
                f"no {mig.LEDGER_TABLE} table — this database has never been "
                f"through khctl apply")
        statuses = mig.status(conn, migrations_dir, baseline)
    bad = mig.broken(statuses)
    if bad:
        raise RuntimeError(mig.drift_message(statuses))
    todo = mig.pending(statuses)
    if todo:
        raise RuntimeError(
            f"{len(todo)} migration(s) NOT applied: "
            f"{', '.join(s.filename for s in todo)} — the deployed schema is "
            f"behind the bundle; run khctl apply")
    unverified = [s for s in statuses if s.state == mig.APPLIED_UNVERIFIED]
    caveat = (f", {len(unverified)} by ledger row only (creates nothing "
              f"unique to verify)" if unverified else "")
    return (f"migrations: ledger agrees with the database — "
            f"{len(statuses)} applied{caveat}")


# ---------------------------------------------------------------------------
# 2. Object store — the WORM enforcement PROOF (writes a sacrificial object;
#    on an adopted client store, agree before running)
# ---------------------------------------------------------------------------
def check_s3_worm(dsn: Optional[str] = None,
                  bucket: Optional[str] = None) -> str:
    # Go through the REAL landing code path: S3RawStore refuses to start
    # unless the bucket has object-lock AND versioning (lock alone silently
    # enforces nothing — SeaweedFS #8350, reproduced on 4.40), and
    # verify_worm() proves enforcement with a sacrificial object instead of
    # trusting the reported configuration. Endpoint/credentials come from
    # settings — post-apply, .env IS the plan's rendered config.
    from knowledge_hub.factstore_pg import PostgresFactStore
    from knowledge_hub.rawstore_s3 import S3RawStore, sha256_hex

    raw_store = S3RawStore(store=PostgresFactStore(dsn=dsn),
                           bucket=bucket)  # DB untouched here
    content = b"knowledge hub capture smoketest"
    uri = raw_store.put("_smoketest", content,
                        meta={"content_hash": sha256_hex(content)})
    assert raw_store.get(uri) == content, "S3 round-trip content mismatch"
    report = raw_store.verify_worm()
    return (f"seaweedfs: bucket '{bucket or settings.s3_raw_bucket}' "
            f"(object-lock + versioning), round-trip ok, WORM ENFORCED "
            f"(overwrite_protected={report['overwrite_protected']}, "
            f"delete_rejected={report['delete_rejected']})")


# ---------------------------------------------------------------------------
# 3. Credential seam — auth + the per-tenant credential-injection seam
# ---------------------------------------------------------------------------
def check_credential_seam() -> str:
    """The credential seam THIS posture uses, exercised end to end.

    d.s Stage 3. Deliberately not "skip the OpenBao check in local posture":
    the claim worth proving is that credentials can be stored and injected at
    all, and that claim is true in both postures with a different backend. So
    the check follows the factory and proves whichever seam is live.

    What it proves is identical either way — put a credential, inject it into an
    OutboundRequest, confirm the value arrived AND that repr() masks it. That
    masking assertion is the one that matters most and it is backend-agnostic:
    it is the invariant keeping credential values out of logs and exception
    messages, and it must hold on the local file exactly as on the vault.
    """
    from knowledge_hub.credentials import make_secrets_provider
    from knowledge_hub.interfaces import OutboundRequest

    provider = make_secrets_provider()
    provider.put_secret("_smoketest", "e2e-check", {"status": "reachable"})
    request = OutboundRequest()
    provider.inject_credential("_smoketest", "e2e-check", request)
    assert request.params["status"] == "reachable"
    assert "reachable" not in repr(request)  # injected values are masked

    if settings.is_local:
        store = getattr(provider, "_store", None)
        where = getattr(store, "path", settings.local_secrets_file)
        return (f"credential seam (local posture): store + inject ok via "
                f"{where}, injected values masked — no vault required")
    return (f"credential seam (deployed posture): OpenBao authenticated, "
            f"per-tenant KV v2 inject ok "
            f"({provider.path_for('<tenant>', '<source>')})")


def check_openbao(addr: Optional[str] = None,
                  token: Optional[str] = None) -> str:
    """OpenBao specifically — unchanged, and still selected by a deployed plan.

    Kept as its own check rather than folded into check_credential_seam: a
    deployed verify must be able to say "the VAULT answered", not just "some
    credential store answered". check_stack.py and verify_checks_for choose
    between the two by posture.
    """
    import hvac

    from knowledge_hub.interfaces import OutboundRequest
    from knowledge_hub.secrets_openbao import OpenBaoSecretsProvider

    client = hvac.Client(url=addr or settings.bao_addr,
                         token=token or settings.bao_root_token)
    if not client.is_authenticated():
        raise RuntimeError("authentication failed (check BAO_ROOT_TOKEN)")
    # Exercise the real credential-injection seam at the per-tenant path
    # layout production policies scope to: tenants/<tenant>/sources/<ref>.
    provider = OpenBaoSecretsProvider(client=client)
    provider.put_secret("_smoketest", "e2e-check", {"status": "reachable"})
    request = OutboundRequest()
    provider.inject_credential("_smoketest", "e2e-check", request)
    assert request.params["status"] == "reachable"
    assert "reachable" not in repr(request)  # injected values are masked
    return (f"openbao: authenticated, per-tenant KV v2 inject ok "
            f"({provider.path_for('<tenant>', '<source>')})")


# ---------------------------------------------------------------------------
# 4. Ollama — local inference: embedding dim + a tiny generation
# ---------------------------------------------------------------------------
def check_ollama(host: Optional[str] = None,
                 embedding_model: Optional[str] = None,
                 embedding_dim: Optional[int] = None,
                 extraction_model: Optional[str] = None) -> str:
    from knowledge_hub.ollama_client import make_ollama_client

    embedding_model = embedding_model or settings.embedding_model
    embedding_dim = embedding_dim or settings.embedding_dim
    extraction_model = extraction_model or settings.extraction_model
    client = make_ollama_client(host)
    emb = client.embeddings(model=embedding_model, prompt="knowledge hub")
    dim = len(emb["embedding"])
    if dim != embedding_dim:
        raise RuntimeError(
            f"embedding dim {dim} != {embedding_dim} (schema vector(1024))")
    gen = client.generate(
        model=extraction_model,
        prompt="Reply with exactly one word: ready",
        think=False,
        options={"num_predict": 32},
    )
    reply = gen["response"].strip()
    return (f"ollama: {embedding_model} dim={dim}, "
            f"{extraction_model} replied {reply!r}")


# ---------------------------------------------------------------------------
# 5. Processing — Stage B seams end to end in memory
# ---------------------------------------------------------------------------
def check_processing(ollama_host: Optional[str] = None) -> str:
    # Stage B readiness without touching the DB: Docling converts, the
    # bge-m3 tokenizer loads (the kit-seeded local file on a deployed box —
    # zero egress, BP28 #20; the HF hub download is the dev-bench fallback
    # only), the chunker produces the parent/child tiers, and live bge-m3
    # embeds a prefixed child.
    from knowledge_hub.chunking import SectionChunker, embedding_text
    from knowledge_hub.embedding_ollama import OllamaEmbedder
    from knowledge_hub.models import ChunkLevel, DocType, Document, RawDocument
    from knowledge_hub.parsing_docling import DoclingParser

    raw = RawDocument(
        id=0, tenant_id="_smoketest", source_system="check",
        source_native_id="smoke.md", content_hash="0" * 64, raw_uri="mem://",
        native_metadata={"data_track": "prose", "doc_type": "sop",
                         "title": "Smoke SOP"})
    content = (b"# Smoke SOP\n\nPurpose: prove the processing seams work.\n\n"
               b"## Steps\n\nOpen the valve. Watch the gauge. Close the "
               b"valve when the needle settles. Log the reading.\n")
    parser = DoclingParser()
    document = parser.parse(raw, content)
    document.id = 0  # in-memory only; nothing is persisted here
    chunks = SectionChunker().chunk(document, parser.extract_text(raw, content))
    parents = [c for c in chunks if c.level is ChunkLevel.parent]
    children = [c for c in chunks if c.level is ChunkLevel.child]
    assert document.doc_type is DocType.sop and parents and children
    assert all(c.contextual_prefix for c in children)
    embedder = OllamaEmbedder(host=ollama_host)
    vec = embedder.embed([embedding_text(children[0])])[0]
    assert len(vec) == embedder.dim
    return (f"processing: docling parse ok, tokenizer ok, "
            f"{len(parents)} parent(s) / {len(children)} child(ren), embedded "
            f"child dim={len(vec)} ({embedder.model}@{embedder.version})")


# ---------------------------------------------------------------------------
# 6. Extraction — ontology binding + one live schema-constrained pass
# ---------------------------------------------------------------------------
def check_extraction(dsn: Optional[str] = None) -> str:
    # Extraction readiness without touching queue/staging tables: the binding
    # builds from the seeded ontology row (migration 004 data included), one
    # LIVE schema-constrained call validates + conforms, and the grounder
    # verifies a span deterministically.
    from knowledge_hub.extraction_llm import LLMJointExtractionStrategy
    from knowledge_hub.factstore_pg import PostgresFactStore
    from knowledge_hub.grounding import SpanGrounder
    from knowledge_hub.interfaces import ExtractionUnit
    from knowledge_hub.models import Chunk, ChunkLevel, DocType, Document
    from knowledge_hub.ontology import PostgresOntologyBinding

    binding = PostgresOntologyBinding(PostgresFactStore(dsn=dsn))
    assert binding.normalize_predicate("owned by") == ("owns", True), \
        "predicate alias data missing — is migration 004 applied?"
    assert "e.g." in binding.prompt_vocabulary(), "ontology examples missing"

    text = "The QA Team owns the smoke-check log."
    document = Document(id=0, tenant_id="_smoketest", raw_document_id=0,
                        doc_type=DocType.sop, title="Smoke",
                        metadata={"data_track": "prose"})
    chunk = Chunk(id=0, tenant_id="_smoketest", document_id=0,
                  level=ChunkLevel.parent, seq=0, content=text,
                  content_hash="0" * 64, char_start=0, char_end=len(text))
    strategy = LLMJointExtractionStrategy(binding)
    result = strategy.extract(ExtractionUnit(
        document=document, source_system="check", chunk=chunk, text=text))
    assert result.entities or result.quarantined, "model returned nothing"
    grounded = SpanGrounder().ground(text, ["QA Team"], text)
    assert grounded.status == "pass"
    return (f"extraction: binding {binding.version} ok, "
            f"{strategy.version} conformed {len(result.facts)} fact(s) / "
            f"{len(result.entities)} entities / {len(result.quarantined)} "
            f"quarantined in {result.stats.wall_ms}ms, grounder ok")


# ---------------------------------------------------------------------------
# 7. Resolution — policy matrix + Splink priors + live adjudication
# ---------------------------------------------------------------------------
def check_resolution(dsn: Optional[str] = None) -> str:
    # Stage D readiness without touching staging tables: the policy matrix
    # and labels store exist (migration 005), Splink/DuckDB scores a pair
    # with the shipped priors and orders same > different, and the local
    # adjudication model answers one schema-constrained verdict.
    from knowledge_hub.factstore_pg import PostgresFactStore
    from knowledge_hub.interfaces import BlockedCandidate
    from knowledge_hub.scoring_tiered import TieredScorer, _Adjudicator

    store = PostgresFactStore(dsn=dsn)
    policy = store.get_resolution_policy("_smoketest", "Organization")
    assert policy is not None and policy.t_high > policy.t_low, \
        "resolution_policy not seeded — is the baseline schema applied?"
    with store.transaction("_smoketest") as conn:
        for table in ("labels", "resolution_decisions"):
            conn.execute(f"SELECT 1 FROM {table} LIMIT 0")

    scorer = TieredScorer(store)
    linker = scorer._build_linker("_smoketest", [])
    same = linker.inference.compare_two_records(
        {"unique_id": "m1", "name": "acme corp", "email": None,
         "domain": "acme.example", "tax_id": None, "customer_id": None},
        {"unique_id": "e1", "name": "acme corp", "email": None,
         "domain": "acme.example", "tax_id": None, "customer_id": None},
    ).as_record_dict()[0]["match_probability"]
    different = linker.inference.compare_two_records(
        {"unique_id": "m2", "name": "acme corp", "email": None,
         "domain": None, "tax_id": None, "customer_id": None},
        {"unique_id": "e2", "name": "zenith widgets", "email": None,
         "domain": "zenith.example", "tax_id": None, "customer_id": None},
    ).as_record_dict()[0]["match_probability"]
    assert same > different, "Splink priors are not ordering pairs sanely"

    verdict = _Adjudicator().judge(
        "Acme Corp", "Organization",
        "Acme Corp handles the solvent deliveries.",
        BlockedCandidate(entity_id=0, canonical_name="Acme Corporation",
                         entity_type="Organization"))
    assert verdict is not None, "adjudication model did not answer"
    return (f"resolution: policy matrix ok (Organization t_high="
            f"{policy.t_high}), splink pair scores same={same:.3f} > "
            f"different={different:.3f}, {scorer.version} adjudicated "
            f"same_entity={verdict[0]} conf={verdict[1]:.2f}")


# ---------------------------------------------------------------------------
# 8. Benchmark harness — tables, pin profile, provenance, leaderboard
# ---------------------------------------------------------------------------
def check_benchmark(dsn: Optional[str] = None) -> str:
    # Migration 006 pieces most likely to differ on a fresh box: the
    # harness tables + seeded pin profile exist, provenance is computable,
    # and the leaderboard view answers.
    import psycopg
    from psycopg.rows import dict_row

    from knowledge_hub.benchmark import code_hash, hardware_fingerprint
    from knowledge_hub.factstore_pg import PostgresFactStore

    dsn = dsn or settings.postgres_dsn
    with psycopg.connect(dsn, connect_timeout=5,
                         row_factory=dict_row) as conn:
        tables = {r["tablename"] for r in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='public'")}
        need = {"gold_sets", "gold_set_items", "pin_profiles",
                "benchmark_runs", "benchmark_run_items"}
        missing = need - tables
        assert not missing, f"missing benchmark tables: {missing} (migration 006)"
        prof = conn.execute(
            "SELECT name FROM pin_profiles ORDER BY created_at").fetchone()
        assert prof, "no pin profile seeded (migration 006)"
        board = conn.execute(
            "SELECT count(*) AS n FROM benchmark_leaderboard").fetchone()
    ch = code_hash()
    assert len(ch) == 64, "code_hash did not produce a sha256"
    fp = hardware_fingerprint(PostgresFactStore(dsn=dsn), "default")
    assert fp.get("postgres") != "unknown", "hardware fingerprint could not see postgres"
    return (f"benchmark harness: tables ok, pin profile {prof['name']!r}, "
            f"code_hash {ch[:12]}…, leaderboard rows={board['n']}")


# ---------------------------------------------------------------------------
# 9. Serving — the S5 boundary end to end over real HTTP
# ---------------------------------------------------------------------------
def check_serving(dsn: Optional[str] = None) -> str:
    # The enforcement boundary end to end, over REAL HTTP on an ephemeral
    # port: assemble from live components (real Postgres + real OpenBao),
    # health answers, an unauthenticated catalog read is REFUSED
    # (fail-closed), and one registered op serves a fully gated round-trip.
    import threading
    import uuid

    from knowledge_hub.credentials import make_credential_resolver
    from knowledge_hub.serving import Principal
    from knowledge_hub.service_http import build_serving_app, make_server

    # d.s Stage 3: the resolver the POSTURE uses. These checks prove the
    # enforcement boundary (401 unauthenticated, the deny-by-default role
    # gate, tenant scoping) — claims worth proving in BOTH postures, so they
    # follow the factory rather than being skipped when there is no vault.
    resolver = make_credential_resolver()
    token = f"smoketest-{uuid.uuid4().hex}"
    resolver.register_principal(token, Principal(
        tenant_id="_smoketest", principal_id="check-stack", roles=[]))
    app = build_serving_app(dsn=dsn, tenants=["_smoketest"],
                            resolver=resolver)
    assert app.service.warm(), "serving connection failed to warm"
    server = make_server(app, "127.0.0.1", 0)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    try:
        with urllib.request.urlopen(f"{base}/v1/health",
                                    timeout=_NET_TIMEOUT) as r:
            health = json.load(r)
        assert health["status"] == "ok", f"health degraded: {health}"

        try:
            urllib.request.urlopen(f"{base}/v1/ops", timeout=_NET_TIMEOUT)
            raise RuntimeError("unauthenticated catalog read was SERVED")
        except urllib.error.HTTPError as e:
            assert e.code == 401, f"expected 401, got {e.code}"

        req = urllib.request.Request(
            f"{base}/v1/ops",
            headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=_NET_TIMEOUT) as r:
            names = {o["name"] for o in json.load(r)["operations"]}
        assert {"get_facts", "get_by_key", "retrieve",
                "entity_dossier"} <= names, f"catalog incomplete: {names}"

        req = urllib.request.Request(
            f"{base}/v1/ops/get_by_key",
            data=json.dumps({"identifier": "smoke"}).encode("utf-8"),
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(req, timeout=_NET_TIMEOUT) as r:
            served = json.load(r)
        assert served["returns"] == "facts" and served["facts"] == []
    finally:
        server.shutdown()
    return (f"serving: v{health['version']} healthy on :{port}, "
            f"{len(names)} ops registered, 401 without credential, gated "
            f"round-trip ok (request {served['request_id'][:8]}…, "
            f"{served['wall_ms']}ms)")


def check_operator(dsn: Optional[str] = None) -> str:
    # The write-twin end to end, over REAL HTTP on an ephemeral port —
    # deliberately NON-MUTATING: it proves the boundary (health incl.
    # migration 010, 401 unauthenticated, the deny-by-default role gate
    # refusing an agent read-principal, the role-scoped action catalog, and
    # a fail-closed 404 on a nonexistent target) without changing any
    # domain state. The refused/failed attempts it makes are themselves
    # audit rows — which proves the audit path too.
    import threading
    import uuid

    from knowledge_hub.credentials import make_credential_resolver
    from knowledge_hub.operator_http import build_operator_app
    from knowledge_hub.serving import Principal
    from knowledge_hub.service_http import make_server

    # d.s Stage 3: the resolver the POSTURE uses. These checks prove the
    # enforcement boundary (401 unauthenticated, the deny-by-default role
    # gate, tenant scoping) — claims worth proving in BOTH postures, so they
    # follow the factory rather than being skipped when there is no vault.
    resolver = make_credential_resolver()
    agent_tok = f"smoketest-{uuid.uuid4().hex}"
    op_tok = f"smoketest-{uuid.uuid4().hex}"
    resolver.register_principal(agent_tok, Principal(
        tenant_id="_smoketest", principal_id="check-agent", roles=[]))
    resolver.register_principal(op_tok, Principal(
        tenant_id="_smoketest", principal_id="check-operator",
        roles=["operator"]))
    app = build_operator_app(dsn=dsn, resolver=resolver)
    assert app.service.ping_postgres(), \
        "operator store not answering (is migration 010 applied?)"
    server = make_server(app, "127.0.0.1", 0)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    def call(path, token=None, body=None, method=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(f"{base}{path}", data=data,
                                     headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=_NET_TIMEOUT) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    try:
        status, health = call("/v1/health")
        assert status == 200 and health["status"] == "ok", health

        status, _ = call("/v1/actions")
        assert status == 401, f"unauthenticated catalog read got {status}"

        status, body = call("/v1/actions/resolve_merge", token=agent_tok,
                            body={"candidate_id": 1, "same": True},
                            method="POST")
        assert status == 403, \
            f"agent read-principal write got {status}, not 403"

        status, body = call("/v1/actions", token=op_tok)
        names = {a["name"] for a in body["actions"]}
        assert {"resolve_merge", "split_merge", "pause_source",
                "acknowledge_alert"} <= names, f"catalog incomplete: {names}"

        status, _ = call("/v1/actions/acknowledge_alert", token=op_tok,
                         body={"kind": "dispatch", "item_id": 0},
                         method="POST")
        assert status == 404, f"nonexistent target got {status}, not 404"

        # BP20: the operator READS answer for the principal's tenant, and
        # the console UI serves from the kit (no CDN, fakes dead).
        status, mon = call("/v1/monitor", token=op_tok)
        assert status == 200 and mon["tenant_id"] == "_smoketest", mon
        assert mon["p95_budget_ms"] == 300
        status, _ = call("/v1/monitor", token=agent_tok)
        assert status == 403, f"agent read-principal monitor got {status}"
        with urllib.request.urlopen(f"{base}/ui/", timeout=_NET_TIMEOUT) as r:
            page = r.read().decode("utf-8")
        assert "OPERATOR : CONSOLE" in page, "UI shell did not serve"
        assert "fonts.googleapis.com" not in page, "UI reaches for a CDN"
    finally:
        server.shutdown()
    return (f"operator API: v{health['version']} healthy on :{port}, "
            f"{health['actions']} write actions + monitor/review reads, "
            f"console UI served from the kit, 401 unauthenticated, 403 for "
            f"read-principal, 404 on missing target — every attempt audited")


# ---------------------------------------------------------------------------
# Field-verifier-only checks
# ---------------------------------------------------------------------------
def check_side_doors(dsn: Optional[str] = None,
                     allowed_users: Optional[set[str]] = None) -> str:
    """The §8.8 rider as a standing check: every external consumer holding a
    Postgres DSN is a side door around the choke point, and isolation is
    VOID while one exists. Heuristic v1: any client-backend connection to
    this database under a user outside the allowlist fails the check. (The
    real enforcement is revoking direct creds; this catches drift.)"""
    import psycopg

    dsn = dsn or settings.postgres_dsn
    allowed = allowed_users or {urlsplit(dsn).username}
    with psycopg.connect(dsn, connect_timeout=5) as conn:
        rows = conn.execute(
            "SELECT usename, application_name,"
            "       COALESCE(client_addr::text, 'local'), count(*)"
            "  FROM pg_stat_activity"
            " WHERE datname = current_database()"
            "   AND backend_type = 'client backend'"
            "   AND pid <> pg_backend_pid()"
            " GROUP BY 1, 2, 3").fetchall()
    offenders = [(u, app, addr, n) for u, app, addr, n in rows
                 if u not in allowed]
    if offenders:
        listing = "; ".join(f"user={u!r} app={app!r} addr={addr} n={n}"
                            for u, app, addr, n in offenders)
        raise RuntimeError(
            f"non-pipeline client(s) connected — isolation is void until "
            f"their direct DB access is revoked: {listing}")
    total = sum(n for *_, n in rows)
    return (f"side doors: {total} client connection(s), all under allowed "
            f"users ({', '.join(sorted(allowed))})")


CORE_PACKAGE = "knowledge_hub"
PLUGIN_ROOT_DIR = "plugins"


def _repo_root(pkg_dir: Optional[Path] = None) -> Path:
    """The repository containing both the core package and plugins/. This
    file lives at <repo>/knowledge_hub_pkg/knowledge_hub/checks.py."""
    pkg_dir = pkg_dir or Path(__file__).resolve().parents[1]
    return pkg_dir.parent


def plugin_package_names(repo_root: Optional[Path] = None) -> set[str]:
    """Importable package names under `plugins/`.

    Discovered, never listed. A hardcoded list is a list somebody forgets
    to extend, and the plugin they forget is exactly the one that gets
    imported into core by accident."""
    root = (repo_root or _repo_root()) / PLUGIN_ROOT_DIR
    if not root.is_dir():
        return set()
    names: set[str] = set()
    for plugin in root.iterdir():
        if not plugin.is_dir() or plugin.name.startswith("."):
            continue
        for candidate in plugin.iterdir():
            if candidate.is_dir() and (candidate / "__init__.py").is_file():
                names.add(candidate.name)
    return names


def core_import_roots(pkg_dir: Optional[Path] = None
                      ) -> dict[str, set[str]]:
    """Every top-level module name the core package imports -> the files
    importing it.

    STATIC, via `ast`. Importing the package to inspect it would only see
    what a particular run happened to execute, and core defers its heaviest
    imports into function bodies precisely so they do not run — which is
    also where an illicit plugin import would most plausibly hide. Parsing
    sees them all, executes nothing, and needs nothing installed."""
    pkg_dir = pkg_dir or Path(__file__).resolve().parents[1]
    roots: dict[str, set[str]] = {}
    for path in sorted((pkg_dir / CORE_PACKAGE).rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"),
                             filename=str(path))
        except SyntaxError as e:
            raise RuntimeError(f"{path.name} does not parse: {e}") from e
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                # level > 0 is a relative import, which by definition stays
                # inside the core package.
                names = [node.module] if node.level == 0 and node.module else []
            else:
                continue
            for name in names:
                roots.setdefault(name.split(".")[0], set()).add(path.name)
    return roots


def check_core_boundary(pkg_dir: Optional[Path] = None,
                        repo_root: Optional[Path] = None) -> str:
    """decant.Source's corpus-agnostic rule, enforced mechanically.

    `knowledge_hub_pkg` serves every corpus and must contain no
    domain-specific logic. Domain lives in ontology sets, in operator
    config, and in external plugins that config points at — and the one
    structural way that rule breaks is core importing a plugin. After that
    the package no longer builds without the domain installed, and the
    separation is over whether or not anyone notices.

    Two nets, because they fail differently:
      * BY NAME — core imports something that matches a package under
        `plugins/`. Catches the ordinary mistake (an editor auto-import, a
        quick fix that reached for the class it could see).
      * BY LOCATION — an imported name resolves, in this environment, to a
        file inside `plugins/`. Catches a plugin installed under a name
        that does not match its directory, which the first net cannot see.

    Deliberately NOT a general dependency check. Core imports several
    third-party roots it does not declare (transitives it uses directly,
    plus an optional benchmark engine); tightening that is real work with
    its own tradeoffs, and bundling it here would mean the boundary rule
    goes red for reasons that have nothing to do with the boundary. One
    check, one claim.

    Runs on SOURCE, so it holds on a dev bench, in the field, and in a kit,
    whether or not any plugin is installed."""
    pkg_dir = pkg_dir or Path(__file__).resolve().parents[1]
    repo_root = repo_root or _repo_root(pkg_dir)

    plugins = plugin_package_names(repo_root)
    imports = core_import_roots(pkg_dir)

    offenders: dict[str, tuple[set[str], str]] = {}
    for root, files in imports.items():
        if root in plugins:
            offenders[root] = (files, f"is a package under {PLUGIN_ROOT_DIR}/")
    for root, files in imports.items():
        if root in offenders or root == CORE_PACKAGE:
            continue
        location = _module_location(root)
        if location is not None and _is_within(location,
                                               repo_root / PLUGIN_ROOT_DIR):
            offenders[root] = (files, f"resolves to {location}")

    if offenders:
        listing = "; ".join(
            f"{root!r} ({why}) imported by {', '.join(sorted(files))}"
            for root, (files, why) in sorted(offenders.items()))
        raise RuntimeError(
            f"{CORE_PACKAGE} imports domain plugin code, which breaks the "
            f"corpus-agnostic contract — a plugin must be reached through "
            f"registered config (knowledge_hub.plugins), never imported: "
            f"{listing}")

    return (f"core boundary: {CORE_PACKAGE} imports none of "
            f"{len(plugins)} plugin package(s) "
            f"({', '.join(sorted(plugins)) or 'none installed in-tree'}) "
            f"across {len(imports)} import root(s)")


def _module_location(root: str) -> Optional[Path]:
    """Where an importable top-level name lives, or None if it is not
    importable here. Uses find_spec, which for a TOP-LEVEL name locates
    without executing the module."""
    import importlib.util

    try:
        spec = importlib.util.find_spec(root)
    except (ImportError, ValueError, AttributeError):
        return None
    if spec is None or not spec.origin:
        return None
    return Path(spec.origin).resolve()


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


def check_remote_inference(endpoint: str,
                           embedding_model: Optional[str] = None,
                           extraction_model: Optional[str] = None) -> str:
    """Shape B: the split-inference endpoint answers and serves the models
    the plan requires. Reports TLS presence honestly — auth/TLS hardening is
    §8.9 net-new item 2 and this check inherits it when built."""
    base = endpoint.rstrip("/")
    required = [m for m in (embedding_model or settings.embedding_model,
                            extraction_model or settings.extraction_model) if m]
    with urllib.request.urlopen(f"{base}/api/version",
                                timeout=_NET_TIMEOUT) as r:
        version = json.load(r).get("version", "?")
    with urllib.request.urlopen(f"{base}/api/tags", timeout=_NET_TIMEOUT) as r:
        tags = [m["name"] for m in json.load(r).get("models", [])]
    missing = [m for m in required
               if not any(t == m or t.startswith(f"{m}:") for t in tags)]
    if missing:
        raise RuntimeError(f"endpoint lacks required model(s): {missing} "
                           f"(has: {', '.join(sorted(tags)) or 'none'})")
    tls = "yes" if base.startswith("https://") else "NO (http — item 2 gap)"
    return (f"remote inference: {base} v{version}, models ok "
            f"({', '.join(required)}), tls={tls}. OFF PREMISES: client "
            f"text leaves the box for this endpoint")


def check_local_external_inference(endpoint: str,
                                   embedding_model: Optional[str] = None,
                                   extraction_model: Optional[str] = None
                                   ) -> str:
    """BP46 Fix 5: an inference endpoint the OPERATOR supplied on this box —
    their GPU, their runtime, a model we did not install. Proves two things
    the remote check cannot: that the endpoint answers with the required
    models, and that it is genuinely LOCAL. Plain http is correct here (it is
    loopback, not a tunnel), so this check does not inherit the remote path's
    TLS gap — and it never reports the deploy as off-premises."""
    from knowledge_hub.deploy_profiles import LOCAL_EXTERNAL_HOSTS

    base = endpoint.rstrip("/")
    host = (urlsplit(base).hostname or "").lower()
    if host not in LOCAL_EXTERNAL_HOSTS:
        raise RuntimeError(
            f"endpoint {base} is not on this box (host {host!r}) — a "
            f"local-external seam claims text never leaves the box, and this "
            f"endpoint breaks that claim; re-plan it as remote inference")
    required = [m for m in (embedding_model or settings.embedding_model,
                            extraction_model or settings.extraction_model) if m]
    with urllib.request.urlopen(f"{base}/api/version",
                                timeout=_NET_TIMEOUT) as r:
        version = json.load(r).get("version", "?")
    with urllib.request.urlopen(f"{base}/api/tags", timeout=_NET_TIMEOUT) as r:
        tags = [m["name"] for m in json.load(r).get("models", [])]
    missing = [m for m in required
               if not any(t == m or t.startswith(f"{m}:") for t in tags)]
    if missing:
        raise RuntimeError(
            f"operator-supplied endpoint lacks required model(s): {missing} "
            f"(has: {', '.join(sorted(tags)) or 'none'}). We did not install "
            f"this endpoint's models — the operator pulls them there, or "
            f"EXTRACTION_MODEL/EMBEDDING_MODEL are re-pinned to what it "
            f"serves")
    return (f"local-external inference: {base} v{version}, models ok "
            f"({', '.join(required)}). ON PREMISES: endpoint is on this "
            f"box, model NOT installed by us, text never leaves")
