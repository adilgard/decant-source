"""Runtime configuration, read from environment / .env.

Defaults match the pilot docker-compose + Ollama setup, so this works out of the
box locally and is overridable by environment variables on the Ubuntu boxes.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# The one filename both the class config below and reload_settings() honour —
# they must never disagree about what "the .env" means.
DEFAULT_ENV_FILE = ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=DEFAULT_ENV_FILE,
                                      env_file_encoding="utf-8",
                                      extra="ignore")

    # --- Postgres (host connection; the DB runs in Docker, exposed on localhost) ---
    postgres_user: str = "kh"
    postgres_password: str = "kh_pilot_pw"
    postgres_db: str = "knowledge_hub"
    postgres_host: str = "localhost"
    postgres_port: int = 5432

    # --- SeaweedFS S3 gateway ---
    s3_endpoint: str = "http://localhost:8333"
    s3_access_key: str = "kh_s3_admin"
    s3_secret_key: str = "kh_s3_secret_pw"
    s3_raw_bucket: str = "kh-raw"
    # Object-lock (WORM) retention stamped on every landed object. COMPLIANCE
    # mode: not removable via the S3 API until it expires — long by design.
    s3_raw_retention_days: int = 3650

    # --- OpenBao (dev mode locally; same paths/auth seam in production) ---
    bao_addr: str = "http://localhost:8200"
    bao_root_token: str = "kh_pilot_root_token"
    # KV v2 mount holding tenant credentials (dev mode auto-mounts 'secret').
    bao_kv_mount: str = "secret"

    # --- AGE graph projection (Build Prompt 9: RETIRED) ---
    # OFF by default and NOT a casual toggle: the projection was frozen after
    # the temporal spine (BP7/BP8) made its edges KNOWN-STALE (retraction/
    # supersession update only the relational rows). Facts are authoritative;
    # nothing reads the graph (serve path and resolver are SQL over facts).
    # Re-enabling requires the resurrection PROJECT in AGE_DORMANT.md:
    # rebuild-from-facts + wire temporal updates + solve cypher
    # parameterization — never just this flag.
    project_to_age: bool = False

    # --- Processing (parse / chunk) ---
    # The bge-m3 tokenizer file the chunker counts with. The deploy kit
    # ships it and the launcher seeds it into the deployment home (= CWD
    # under khctl launch), so an offline box never touches the HF hub
    # (BP28 #20). A dev bench without the file falls back to the hub.
    bge_m3_tokenizer_json: str = "tokenizer/bge-m3/tokenizer.json"

    # --- Ollama (native on the Windows/GPU host) ---
    ollama_host: str = "http://localhost:11434"
    # Which inference seam the deploy plan chose (rendered into .env by
    # render_env; BP46 Fix 5). "local" = our model on this box;
    # "local-external" = an operator-supplied LOCAL endpoint, model not ours,
    # text still never leaves the box; "remote" = over a tunnel, text leaves
    # the premises. Read it rather than inferring locality from ollama_host —
    # a local-external deploy and a remote one both carry an endpoint.
    inference_seam: str = "local"
    # HTTP budget for EVERY Ollama call (see ollama_client.make_ollama_client).
    # The client library defaults to httpx Timeout(None) — unbounded on connect
    # AND read — which is how one stalled generate hung a whole test run
    # indefinitely (2026-08-03): TCP established, zero bytes, zero CPU, no end.
    # Split because the two failure modes are nothing alike:
    #   connect — a listener that accepts and never answers is Docker Desktop's
    #     dual-stack ::1 black hole (see factstore_pg._conn), not slow work.
    #     Fail fast so the next address family gets its turn.
    #   read — real generation on a 36B MoE legitimately runs minutes, so this
    #     is deliberately generous. The goal is BOUNDED, not fast: an operator
    #     waiting 10 minutes has a slow box, one waiting forever has no signal.
    ollama_connect_timeout_s: float = 5.0
    ollama_read_timeout_s: float = 600.0
    embedding_model: str = "bge-m3"
    extraction_model: str = "qwen3.6"
    # Gray-band ER adjudication (Stage D, Tier 1b). Defaults to the extraction
    # model; swap independently once the benchmark picks a dedicated one.
    adjudication_model: str = "qwen3.6"
    embedding_dim: int = 1024

    # --- Serving service (Build Prompt S5) ---
    # Loopback by default: exposing the boundary beyond the host is a
    # deployment decision, not a code default.
    serving_host: str = "127.0.0.1"
    serving_port: int = 8080
    # Comma-separated tenant ids the runner registers the default op surface
    # for (registration is build-time; the service exposes exactly these).
    serving_tenants: str = ""
    # Append-only JSONL sink for EnvelopeUsage records (the strip-later
    # evidence); a Postgres table is the bookmarked production sink.
    serving_usage_log: str = "serving_usage.jsonl"

    # --- Operator write API (Build Prompt 19) ---
    # The write-twin runs beside the read boundary on its own port; loopback
    # by default for the same reason.
    operator_host: str = "127.0.0.1"
    operator_port: int = 8081

    # --- Ontology registry (d.s Stage 1) ---
    # The git-tracked folder holding portable ontology sets (<version>.json).
    # Relative paths resolve against the working directory — the deployment
    # home under khctl, the infra root on the dev bench — same convention as
    # bge_m3_tokenizer_json above.
    ontology_dir: str = "ontologies"

    @property
    def postgres_dsn(self) -> str:
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


settings = Settings()


def reload_settings(env_file: Optional[Path | str] = None) -> Optional[Path]:
    """Re-read environment + an .env into the EXISTING singleton, in place.
    Returns the file actually read, or None when there was none to read.

    The singleton binds when knowledge_hub.config is first imported — for
    khctl that is process start, in whatever directory khctl was invoked
    from. The launcher then chdirs into the deployment home and, on a fresh
    deploy, apply WRITES the .env the rest of the session must honor
    (minted S3 credentials, the real vault token, pinned models). Every
    consumer holds a reference to the singleton object, so rebinding the
    module attribute would fix nothing: refresh the object itself.
    (BP33 rehearsal finding: the launcher's step-6 verify and start_program's
    ingest sweep ran against pilot defaults on a healthy deploy.)

    A MISSING .ENV IS A NO-OP, NOT A RESET (2026-08-03). This used to call
    `Settings()` unconditionally, so calling it from a directory with no .env
    silently reverted EVERY field to its class default — which for a deployed
    process means the PILOT credentials and `localhost`. "Reload from .env"
    with no .env to read is nothing to do, not a reset nobody asked for.

    Found via the test suite, where it was doing real damage: three tests
    restored themselves with `reload_settings()` while still chdir'd into a
    temp home, so from that point on the whole process was on `localhost`
    instead of the `.env`'s pinned `127.0.0.1`. Every later test then paid
    Docker Desktop's dual-stack stall on each fresh connection (~10s), which
    is what made a full run take hours. Their restore assertions passed
    throughout because they checked `s3_access_key`, whose .env value is
    identical to its class default — the one field that could not reveal the
    problem. Pass `env_file` explicitly to restore from a known file rather
    than relying on CWD.
    """
    path = Path(env_file) if env_file is not None else Path(DEFAULT_ENV_FILE)
    if not path.is_file():
        logger.warning(
            "reload_settings(): no %s found (cwd=%s) — leaving the current "
            "configuration in place. Reverting to class defaults here would "
            "silently swap a deployment's config for the pilot ones.",
            path, Path.cwd())
        return None
    fresh = Settings(_env_file=str(path))
    for name in Settings.model_fields:
        setattr(settings, name, getattr(fresh, name))
    return path
