"""Test harness: the REAL pilot stack (docker-compose), not mocks.

Postgres: a dedicated database `kh_factstore_test` is (re)built once per
session from knowledge_hub_baseline_schema.sql + every migrations/*.sql in
order, so the pilot `knowledge_hub` database is never touched. Every test
runs under its own tenant_id, which makes tests mutually invisible by
construction — exactly the isolation property the store must guarantee.

SeaweedFS + OpenBao: capture tests hit the real services from the same
compose file — a dedicated object-lock bucket (`kh-raw-test`, short WORM
retention so probe objects age out) and per-tenant vault paths under the
dev-mode KV mount (tenant ids are unique per test, so paths never collide).
"""
from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path

import psycopg
import pytest

from knowledge_hub.capture import CaptureService
from knowledge_hub.config import settings
from knowledge_hub.dispatch_pg import PostgresDispatcher
from knowledge_hub.factstore_pg import PostgresFactStore
from knowledge_hub.pipeline import Pipeline
from knowledge_hub.rawstore_s3 import S3RawStore

INFRA_DIR = Path(__file__).resolve().parents[2]
TEST_DB = "kh_factstore_test"


@pytest.fixture(scope="session", autouse=True)
def _local_secrets_in_tmp(tmp_path_factory):
    """No test may write the REPO's local credential store (d.s Stage 3).

    In local posture — now the default — anything that touches the credential
    seam resolves `settings.local_secrets_file` against the CWD, which for the
    suite is the infra root. Left alone, a capture test would quietly create a
    real `.secrets.local.json` next to the developer's `.env`, and a later run
    would inherit whatever the previous one left in it. Tests that share
    credential state through a file in the source tree are exactly the kind of
    cross-run coupling this suite works hard to avoid elsewhere (per-test tenant
    ids, a dedicated test database, a dedicated bucket).

    Set as an ENV VAR as well as on the singleton, deliberately: env vars
    outrank a .env in pydantic-settings, so this survives the several tests that
    call reload_settings() — which would otherwise reset the field to its
    relative class default and point it back at the repo.
    """
    from _pytest.monkeypatch import MonkeyPatch

    from knowledge_hub.config import settings

    mp = MonkeyPatch()
    path = tmp_path_factory.mktemp("kh-secrets") / ".secrets.local.json"
    mp.setenv("KH_LOCAL_SECRETS_FILE", str(path))
    original = settings.local_secrets_file
    settings.local_secrets_file = str(path)
    yield path
    settings.local_secrets_file = original
    mp.undo()

DIM = settings.embedding_dim
ONTOLOGY = "baseline-0.1"


def unit_vec(axis: int, dim: int = DIM) -> list[float]:
    v = [0.0] * dim
    v[axis] = 1.0
    return v


def mix_vec(a: int, b: int, wa: float = 0.9, wb: float = 0.1) -> list[float]:
    v = [0.0] * DIM
    v[a], v[b] = wa, wb
    return v


@pytest.fixture(scope="session")
def test_dsn() -> str:
    admin_dsn = settings.postgres_dsn
    # connect_timeout on every suite connection: Docker Desktop's dual-stack
    # localhost proxy can black-hole one address family under load (see
    # factstore_pg._conn) — a bounded per-address timeout falls through to
    # the working address instead of wedging the whole session.
    with psycopg.connect(admin_dsn, autocommit=True,
                         connect_timeout=10) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
        conn.execute(f"CREATE DATABASE {TEST_DB}")
    dsn = admin_dsn.rsplit("/", 1)[0] + "/" + TEST_DB

    schema = (INFRA_DIR / "knowledge_hub_baseline_schema.sql").read_text(encoding="utf-8")
    with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
        # ag_catalog must be ON the search_path for create_graph()/create_vlabel()
        # to resolve, and LAST so new objects land in public (NOTES.md gotcha).
        conn.execute("SET search_path = public, ag_catalog;")
        conn.execute(schema)
        for migration in sorted((INFRA_DIR / "migrations").glob("*.sql")):
            conn.execute(migration.read_text(encoding="utf-8"))
    return dsn


@pytest.fixture(scope="session")
def store(test_dsn: str) -> PostgresFactStore:
    s = PostgresFactStore(dsn=test_dsn)
    yield s
    s.close()


@pytest.fixture(scope="session")
def pipeline(store: PostgresFactStore) -> Pipeline:
    return Pipeline(store=store)


@pytest.fixture()
def tenant() -> str:
    return f"t-{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# Capture path (Build Prompt 2): real SeaweedFS + real OpenBao + outbox
# ---------------------------------------------------------------------------
TEST_BUCKET = "kh-raw-test"


@pytest.fixture(scope="session")
def raw_store(store: PostgresFactStore) -> S3RawStore:
    # Short retention: WORM stays active for the whole test run, and probe
    # objects age out of COMPLIANCE hold instead of pinning the dev volume.
    return S3RawStore(store=store, bucket=TEST_BUCKET,
                      retention=timedelta(minutes=15))


@pytest.fixture(scope="session")
def secrets():
    """The SecretsProvider for the ACTIVE posture (d.s Stage 3).

    Goes through the factory rather than naming OpenBao, so the capture-path
    tests that consume this fixture run on a bench with no vault (local posture,
    the default) and against the real vault when the suite is run with
    KH_POSTURE=deployed. The tests themselves are unchanged and cannot tell the
    difference — which is the property the ABC was for.

    Tests that specifically prove the VAULT implementation construct
    OpenBaoSecretsProvider directly (test_secrets_openbao.py); they are about
    the backend, not the seam.
    """
    from knowledge_hub.credentials import make_secrets_provider
    return make_secrets_provider()


@pytest.fixture(scope="session")
def dispatcher(store: PostgresFactStore) -> PostgresDispatcher:
    return PostgresDispatcher(store)


@pytest.fixture()
def capture(pipeline: Pipeline, raw_store: S3RawStore,
            dispatcher: PostgresDispatcher, secrets) -> CaptureService:
    return CaptureService(pipeline, raw_store, dispatcher, secrets=secrets)


# ---------------------------------------------------------------------------
# Processing Stage B (Build Prompt 3): real Docling + real bge-m3 tokenizer +
# live Ollama embeddings — session-scoped because they load models once.
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def parser():
    from knowledge_hub.parsing_docling import DoclingParser
    return DoclingParser()


@pytest.fixture(scope="session")
def chunker():
    from knowledge_hub.chunking import SectionChunker
    return SectionChunker()


@pytest.fixture(scope="session")
def embedder():
    from knowledge_hub.embedding_ollama import OllamaEmbedder
    return OllamaEmbedder()


@pytest.fixture()
def processing(pipeline: Pipeline, raw_store: S3RawStore,
               dispatcher: PostgresDispatcher, parser, chunker, embedder):
    from knowledge_hub.processing import ProcessingService
    return ProcessingService(pipeline, raw_store, parser, chunker, embedder,
                             dispatcher=dispatcher)


# ---------------------------------------------------------------------------
# Extraction (Build Prompt 4): ontology binding from the seeded row, live
# qwen3.6 joint strategy, deterministic structured map + grounder, and the
# extraction_queue outbox — session-scoped where construction is expensive.
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def binding(store: PostgresFactStore):
    from knowledge_hub.ontology import PostgresOntologyBinding
    return PostgresOntologyBinding(store, version=ONTOLOGY)


@pytest.fixture(scope="session")
def extraction_dispatcher(store: PostgresFactStore) -> PostgresDispatcher:
    return PostgresDispatcher(store, table="extraction_queue")


@pytest.fixture(scope="session")
def llm_strategy(binding):
    from knowledge_hub.extraction_llm import LLMJointExtractionStrategy
    return LLMJointExtractionStrategy(binding)


@pytest.fixture(scope="session")
def structured_strategy(binding):
    from knowledge_hub.extraction_structured import StructuredMapStrategy
    return StructuredMapStrategy(binding)


@pytest.fixture(scope="session")
def grounder():
    from knowledge_hub.grounding import SpanGrounder
    return SpanGrounder()


@pytest.fixture()
def extraction(pipeline: Pipeline, raw_store: S3RawStore, binding,
               llm_strategy, structured_strategy, grounder,
               extraction_dispatcher):
    from knowledge_hub.extraction import ExtractionService
    return ExtractionService(pipeline, raw_store, binding, llm_strategy,
                             structured_strategy, grounder,
                             dispatcher=extraction_dispatcher)


@pytest.fixture()
def full_processing(pipeline: Pipeline, raw_store: S3RawStore,
                    dispatcher: PostgresDispatcher, parser, chunker, embedder,
                    extraction_dispatcher):
    """ProcessingService wired to hand finished documents to extraction."""
    from knowledge_hub.processing import ProcessingService
    return ProcessingService(pipeline, raw_store, parser, chunker, embedder,
                             dispatcher=dispatcher,
                             extraction_dispatcher=extraction_dispatcher)


# ---------------------------------------------------------------------------
# Resolution Stage D (Build Prompt 5): the tiered scorer (real Splink/DuckDB
# + live Ollama adjudication) behind the Scorer seam, driven by
# ResolutionService. Session-scoped scorer: policies re-read per sweep, so
# caching the object is safe.
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def scorer(store: PostgresFactStore):
    from knowledge_hub.scoring_tiered import TieredScorer
    return TieredScorer(store)


@pytest.fixture()
def resolution(pipeline: Pipeline, scorer, embedder):
    from knowledge_hub.resolution import ResolutionService
    return ResolutionService(pipeline, scorer, embedder)
