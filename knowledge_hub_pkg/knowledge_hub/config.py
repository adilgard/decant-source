"""Runtime configuration, read from environment / .env.

Defaults match the pilot docker-compose + Ollama setup, so this works out of the
box locally and is overridable by environment variables on the Ubuntu boxes.

POSTURE (d.s Stage 1) is the one field that changes what the SYSTEM IS rather
than where it points:

    KH_POSTURE=local      (DEFAULT, and what an unset/blank value resolves to)
        Internal, single-user. Deployment/product ceremony is OFF: no kit
        signing, no arrival gate, no unseal-share custody ceremony. Credentials
        come from a local gitignored file, so OpenBao is not required to start,
        run, or ingest.
    KH_POSTURE=deployed
        Today's full hardened behavior, unchanged in every particular.

This is a SWITCH, not a deletion — the hardened path stays intact and is one
setting away. Deliberately NOT called a "profile": `--profile` /
`DeployPlan.profile` / profiles.toml already mean the COMMERCIAL OFFERING
(appliance | client-gpu | hosted), and one word cannot carry both meanings in
the same CLI.

Why a KH_ prefix on our own knobs: POSTGRES_*, S3_*, BAO_* configure THIRD-PARTY
services and keep their conventional names; KH_POSTURE joins KH_WORK_DIR and
KH_SIGN_KEY as a decant.Source knob. `POSTURE` alone is too generic a name to
claim in a shared environment, and the direction that mistake fails in is the
bad one: a stray `POSTURE=local` inherited from some unrelated tool would soften
a REAL deploy silently, where `POSTURE=deployed` on a laptop would at least fail
loudly. KH_POSTURE is therefore the ONLY name recognized.

Consequence worth knowing: because posture carries a validation_alias, the env
var / .env key is the only way in. `Settings(posture="deployed")` is ignored
(model_config sets extra="ignore", which .env files need) — construct with
`Settings(KH_POSTURE="deployed")` instead. `populate_by_name=True` would fix the
keyword form but also re-open bare `POSTURE` as an env var, which is the exact
thing above. test_posture.py pins this so it is documented, not discovered.

Out of scope here, named so it is not silently assumed: posture never gates a
provenance, correctness, or boundary check. check_migrations, check_side_doors,
check_core_boundary, the ontology allowlist, span grounding, and adjudication
quarantine run identically in both postures and read nothing from this field.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# The one filename both the class config below and reload_settings() honour —
# they must never disagree about what "the .env" means.
DEFAULT_ENV_FILE = ".env"

# --- Posture values (d.s Stage 1) ------------------------------------------
# Module constants, not string literals at call sites: every consumer compares
# against these, so a typo is an ImportError instead of a silently-false branch
# that would leave ceremony ON where it should be OFF (or worse, OFF where it
# should be ON).
POSTURE_LOCAL = "local"
POSTURE_DEPLOYED = "deployed"
POSTURES = (POSTURE_LOCAL, POSTURE_DEPLOYED)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=DEFAULT_ENV_FILE,
                                      env_file_encoding="utf-8",
                                      extra="ignore")

    # --- Deployment posture (d.s Stage 1) ---
    # Absent, unset, or blank resolves to `local`: an internal tool must not
    # need a setting present to behave like an internal tool. Going the other
    # way — defaulting to `deployed` — would make every forgotten .env demand
    # a vault and a signing key to open a folder of your own documents.
    posture: str = Field(POSTURE_LOCAL, validation_alias="KH_POSTURE")

    # Where local-posture credentials live: source credentials AND the
    # serving/operator principal registry, in one gitignored file. Relative
    # paths resolve against the working directory — same convention as
    # bge_m3_tokenizer_json and ontology_dir below. Read only in local
    # posture; deployed posture never opens it.
    local_secrets_file: str = Field(".secrets.local.json",
                                    validation_alias="KH_LOCAL_SECRETS_FILE")

    # --- Postgres (host connection; the DB runs in Docker, exposed on localhost) ---
    # postgres_user is the BOOTSTRAP account: it owns the schema, runs
    # migrations, and creates the least-privilege roles below. Runtime
    # consumers should NOT use it — see the per-consumer DSNs further down.
    postgres_user: str = "kh"
    postgres_password: str = "kh_pilot_pw"
    postgres_db: str = "knowledge_hub"
    postgres_host: str = "localhost"
    postgres_port: int = 5432

    # --- Least-privilege role passwords (roles.py) --------------------------
    # Empty means "not provisioned": the role is created NOLOGIN and the
    # consumer falls back to the bootstrap account, LOUDLY (see
    # role_dsn below). Absent rather than defaulted on purpose — a default
    # password here would be a guessable credential on every deployment.
    kh_pg_pipeline_password: str = ""
    kh_pg_serving_password: str = ""
    kh_pg_operator_password: str = ""
    kh_pg_report_password: str = ""

    # --- SeaweedFS S3 gateway ---
    s3_endpoint: str = "http://localhost:8333"
    s3_access_key: str = "kh_s3_admin"
    s3_secret_key: str = "kh_s3_secret_pw"
    s3_raw_bucket: str = "kh-raw"
    # Object-lock (WORM) retention stamped on every landed object. COMPLIANCE
    # mode: not removable via the S3 API until it expires — long by design.
    s3_raw_retention_days: int = 3650

    # --- OpenBao (dev mode locally; same paths/auth seam in production) ---
    # Read in DEPLOYED posture. In local posture the credential seams resolve
    # against local_secrets_file instead and these are never contacted.
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
    # NOTE: unrelated to `posture`. This says where INFERENCE runs; posture
    # says whether product ceremony applies. A deployed box can run local
    # inference, and a local-posture bench can point at a remote endpoint.
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
    # Where EnvelopeUsage records go. 'postgres' (default, migration 019) is
    # the production sink: the §8.8 verification half asks "did principal X
    # read through ops", which is a query, and a file cannot answer it
    # without something parsing the whole file. 'jsonl' remains as a
    # deliberate opt-out for a box with no usage table — chosen, never
    # fallen into (PostgresUsageRecorder refuses to construct if the table
    # is absent rather than dropping rows quietly).
    serving_usage_sink: str = "postgres"
    # The 'jsonl' sink's path. Still the strip-later evidence, same records.
    serving_usage_log: str = "serving_usage.jsonl"

    @field_validator("serving_usage_sink")
    @classmethod
    def _check_usage_sink(cls, value: str) -> str:
        text = (value or "").strip().lower() or "postgres"
        if text not in ("postgres", "jsonl"):
            raise ValueError(
                f"unknown SERVING_USAGE_SINK {value!r} — must be 'postgres' "
                f"or 'jsonl'. An unrecognized value is an error, not a "
                f"fallback: reading it as 'jsonl' would silently drop the "
                f"attribution the deployment believed it was recording.")
        return text

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

    # ------------------------------------------------------------- validation
    @field_validator("posture", mode="before")
    @classmethod
    def _resolve_posture(cls, value: object) -> str:
        """Absent/blank -> local; anything else must be an exact known value.

        An unrecognized posture RAISES rather than falling back, because both
        fallback directions are wrong in a way nobody would notice: silently
        reading KH_POSTURE=prod as `local` ships a soft build, and silently
        reading it as `deployed` demands a vault on a laptop. A typo is a
        typo — say so at startup, where it is cheap.
        """
        if value is None:
            return POSTURE_LOCAL
        text = str(value).strip().lower()
        if not text:
            return POSTURE_LOCAL
        if text not in POSTURES:
            raise ValueError(
                f"unknown KH_POSTURE {text!r} — must be one of "
                f"{', '.join(POSTURES)} (unset means {POSTURE_LOCAL})")
        return text

    # ------------------------------------------------------------- accessors
    @property
    def is_local(self) -> bool:
        """Internal posture: ceremony skipped, credentials from a local file."""
        return self.posture == POSTURE_LOCAL

    @property
    def is_deployed(self) -> bool:
        """Hardened posture: today's behavior, in full."""
        return self.posture == POSTURE_DEPLOYED

    @property
    def postgres_dsn(self) -> str:
        """The BOOTSTRAP connection: schema owner, migration runner, role
        creator. Correct for apply and for the test harness that makes and
        drops databases. Wrong for a runtime consumer — use role_dsn."""
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    def role_dsn(self, role: str) -> str:
        """The DSN for one least-privilege role, or the bootstrap DSN with a
        WARNING if that role has no password provisioned yet.

        The fallback is deliberate and it is deliberately loud. Hard-failing
        would brick every deployment that upgrades before running apply,
        which is the modify-don't-extend failure this codebase avoids on
        purpose. Silently falling back would re-open the exact side door the
        roles exist to close. So: it degrades, it says so every time, and
        `check_side_doors` fails on the resulting connection — config
        reports the drift, the check refuses it.
        """
        from knowledge_hub import roles as _roles

        password = {
            _roles.PIPELINE_ROLE: self.kh_pg_pipeline_password,
            _roles.SERVING_ROLE: self.kh_pg_serving_password,
            _roles.OPERATOR_ROLE: self.kh_pg_operator_password,
            _roles.REPORT_ROLE: self.kh_pg_report_password,
        }.get(role, "").strip()
        if not password:
            logger.warning(
                "role %s has no password provisioned (%s unset) — falling "
                "back to the bootstrap account %r. Isolation is NOT in "
                "force on this connection and check_side_doors will fail "
                "it. Run `khctl apply` to provision the roles.",
                role, _roles.PASSWORD_ENV.get(role, "?"), self.postgres_user)
            return self.postgres_dsn
        return (
            f"postgresql://{role}:{password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def pipeline_dsn(self) -> str:
        from knowledge_hub import roles as _roles
        return self.role_dsn(_roles.PIPELINE_ROLE)

    @property
    def serving_dsn(self) -> str:
        from knowledge_hub import roles as _roles
        return self.role_dsn(_roles.SERVING_ROLE)

    @property
    def operator_dsn(self) -> str:
        from knowledge_hub import roles as _roles
        return self.role_dsn(_roles.OPERATOR_ROLE)

    @property
    def report_dsn(self) -> str:
        from knowledge_hub import roles as _roles
        return self.role_dsn(_roles.REPORT_ROLE)


settings = Settings()


# ---------------------------------------------------------------------------
# The posture banner — "soft is never silent"
# ---------------------------------------------------------------------------
# Returns LINES and prints nothing itself: config stays free of I/O side
# effects, the four process entry points do the printing, and a test can
# assert on the text without capturing stdout.
def posture_banner(s: Optional[Settings] = None) -> list[str]:
    """The lines every run prints, naming the posture and what it changes.

    A local-posture run says so EVERY time. The whole safety argument for
    internal-by-default rests on the operator never being able to mistake a
    soft run for a hardened one, and a banner that only prints sometimes is
    exactly how that mistake happens.

    Both branches now describe behavior that is actually wired — the staged
    "not yet" wording is gone. Keep it that way: a banner that overclaims is
    the same class of defect it exists to prevent.
    """
    s = s or settings
    if s.is_deployed:
        return [
            "decant.Source — posture: DEPLOYED (hardened)",
            "  ceremony ON  — kit signing, arrival gate, unseal-share custody",
            f"  credentials  — OpenBao at {s.bao_addr}",
        ]
    return [
        "decant.Source — posture: LOCAL (internal, single-user)",
        "  ceremony OFF — no kit signing, no arrival gate, no custody "
        "ceremony; make-kit REFUSES",
        f"  credentials  — local file {s.local_secrets_file} (no vault needed)",
        "  set KH_POSTURE=deployed for the hardened path (it is intact, "
        "not removed)",
    ]


def posture_line(s: Optional[Settings] = None) -> str:
    """The one-line form, for read-only commands that report and exit.

    "Soft is never silent" does not require four lines every time. It requires
    that no run can be MISTAKEN for the other posture, and a single line naming
    it does that. The full banner earns its space where the posture changes what
    the command DOES — anything that writes to this box, mints a credential, or
    produces an artifact that leaves it; see FULL_BANNER_COMMANDS in
    deploy_cli.py. A `khctl migrations status` does not need the essay.

    Kept parallel to posture_banner()'s first line on purpose: the two forms
    must read as the same statement, so moving between them teaches nothing new.
    """
    s = s or settings
    return posture_banner(s)[0]


def print_posture_banner(emit: Callable[[str], None] = print,
                         s: Optional[Settings] = None,
                         brief: bool = False) -> None:
    """Emit the banner through `emit` (default: print), full or one-line.

    The seam exists because the four entry points print differently — khctl
    and check_stack.py write to stdout, the two HTTP runners have a logger —
    and because tests drive it without touching either.
    """
    if brief:
        emit(posture_line(s))
        return
    for line in posture_banner(s):
        emit(line)


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

    A POSTURE CHANGE IS LOGGED AT WARNING (d.s Stage 1). This is the one
    field whose reload can change what the process IS, not just where it
    points, and the reset bug above proves this function can move config
    under a running session without anyone noticing. The banner printed at
    startup would then be describing a posture the process is no longer on,
    so the change announces itself here too. A malformed KH_POSTURE in the
    file raises out of Settings() — deliberately unhandled, same as any
    other invalid value in a .env we were told to honor.
    """
    path = Path(env_file) if env_file is not None else Path(DEFAULT_ENV_FILE)
    if not path.is_file():
        logger.warning(
            "reload_settings(): no %s found (cwd=%s) — leaving the current "
            "configuration in place. Reverting to class defaults here would "
            "silently swap a deployment's config for the pilot ones.",
            path, Path.cwd())
        return None
    before = settings.posture
    fresh = Settings(_env_file=str(path))
    for name in Settings.model_fields:
        setattr(settings, name, getattr(fresh, name))
    if fresh.posture != before:
        logger.warning(
            "reload_settings(): POSTURE CHANGED %s -> %s (read from %s) — "
            "ceremony and the credential seam now follow the new posture; "
            "the banner printed at startup no longer describes this process.",
            before, fresh.posture, path)
    return path
