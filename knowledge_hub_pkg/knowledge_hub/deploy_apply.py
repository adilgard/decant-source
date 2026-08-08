"""khctl apply — execute a deploy plan (install-ubuntu.sh, ported and
plan-driven).

Phases, in order (apply STOPS at the first failure — later phases depend on
earlier ones, unlike verify which always reports the full picture):

  0. kit          manifest hash verification (+ minisign signature when
                  present); a kit-less bundle folder is a DEV install and
                  says so honestly
  1. preflight    docker/compose present (when the plan has 'ours'
                  services), disk floor
  2. env          .env.deploy -> .env (the plan becomes THE config;
                  prior .env backed up once to .env.bak)
  3. services     docker load kit images, compose up ONLY the plan's
                  'ours' services (production OpenBao override when
                  secrets=ours), wait healthy
  4. schema       baseline + migrations replay over psycopg against the
                  plan's DSN — the same code path works for ours AND an
                  adopted client Postgres (the bash version could only
                  docker-exec into our own container)
  5. openbao      PRODUCTION bootstrap: init (5 shares / threshold 3) ->
                  custody ceremony (printed ONCE, never written to disk) ->
                  unseal -> KV v2 mount; idempotent when already
                  initialized+unsealed
  6. models       Ollama model store from the kit SSD (no `ollama pull` —
                  air-gap); without a kit store, verifies required models
                  are already present
  7. python       version integrity (installed == pyproject == __version__)
  8. tenants      per tenant: mint bearer token -> register serving
                  principal -> vault marker for idempotency; token printed
                  ONCE

`--dry-run` walks every phase and prints what it WOULD do with fully
resolved values — the walk-in rehearsal.
"""
from __future__ import annotations

import hashlib
import json
import secrets as pysecrets
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import quote

from knowledge_hub.config import settings
from knowledge_hub.deploy_profiles import DeployPlan

MIN_DISK_GB = 25
BAO_SHARES, BAO_THRESHOLD = 5, 3
PROD_BAO_COMPOSE = "docker-compose.openbao-prod.yml"

# The dev-mode vault literal render_env emits (deploy_cli.PILOT_ENV_DEFAULTS
# references THIS constant). A deployed home's .env never carries it — its
# real root token is minted by phase_openbao at init and must survive every
# re-plan (B3).
PILOT_PLACEHOLDER_TOKEN = "kh_pilot_root_token"

# F14: the apply phase ledger. run_apply records where a wet apply got to,
# so the launcher can refuse to call a half-applied home "deployed".
# An engagement artifact: lives in the deployment home, never in a kit
# (deploy_kit.FORBIDDEN_NAMES is the second net).
APPLY_PROGRESS_FILE = ".apply_progress.json"

# The one unseal command that actually exists on a deployed box — OpenBao
# runs INSIDE container kh-openbao; no `bao` binary is installed on the host.
# BAO_ADDR is NOT decoration (BP28 #17): the `bao` CLI defaults to HTTPS
# while the production listener is plain HTTP — without it the printed
# post-reboot recovery command fails with "server gave HTTP response to
# HTTPS client" in front of an operator whose stack is down.
UNSEAL_COMMAND = ("docker exec -it -e BAO_ADDR=http://127.0.0.1:8200 "
                  "kh-openbao bao operator unseal")


class ApplyError(Exception):
    """Phase cannot proceed — message tells the operator what to do."""


@dataclass
class ApplyContext:
    plan: DeployPlan
    infra_dir: Path                    # the bundle folder (compose, schema, migrations)
    kit_dir: Path                      # manifest.json, images/, ollama_models/
    env_file: Path                     # the plan-rendered .env.deploy
    dry_run: bool = False
    env: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Small pure helpers (unit-tested)
# ---------------------------------------------------------------------------
def parse_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


def dsn_from_env(env: dict[str, str]) -> str:
    return (f"postgresql://{quote(env.get('POSTGRES_USER', 'kh'))}:"
            f"{quote(env.get('POSTGRES_PASSWORD', ''))}@"
            f"{env.get('POSTGRES_HOST', 'localhost')}:"
            f"{env.get('POSTGRES_PORT', '5432')}/"
            f"{env.get('POSTGRES_DB', 'knowledge_hub')}")


def sha256_stream(path: Path) -> str:
    """Streaming sha256 — NEVER read_bytes() a kit artifact: the qwen model
    blob is 55GB and a whole-file read pages a 64GB box into the ground
    (found live at the first full-scale arrival gate)."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_kit_manifest(kit_dir: Path) -> list[str]:
    """Hash-verify every artifact the manifest lists. No manifest = dev
    install (allowed, but said out loud). A hash mismatch is NEVER ok.

    Hashes only — signature verification (ORDER OF TRUST step 1, against
    the verifier's own embedded keys) lives in deploy_kit and runs BEFORE
    this in both phase_kit and verify-kit; a manifest whose signature has
    not been established is attacker-controlled data."""
    manifest_path = kit_dir / "manifest.json"
    if not manifest_path.exists():
        return ["no kit manifest — DEV install from bundle folder "
                "(artifact hashes unverified)"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    lines = []
    for artifact in manifest.get("artifacts", []):
        target = kit_dir / artifact["path"]
        if not target.exists():
            raise ApplyError(f"kit artifact missing: {artifact['path']}")
        digest = sha256_stream(target)
        if digest != artifact["sha256"]:
            raise ApplyError(
                f"kit artifact HASH MISMATCH: {artifact['path']} — the kit "
                f"is corrupt or tampered; refuse and re-image")
        lines.append(f"verified {artifact['path']} ({digest[:12]}…)")
    return lines


CEREMONIES = {
    "operator": (
        "OPERATOR CUSTODY — record the {n} shares in OUR kit custody\n"
        "(password manager), NEVER on this box. Any restart of this vault\n"
        "needs {t} shares (remote API or site visit — we own that ceremony)."),
    "client": (
        "CLIENT CUSTODY CEREMONY — print the {n} shares NOW, seal them in\n"
        "envelopes, hand them to the client's IT/security officer, and have\n"
        "THEM perform a test seal/unseal before you leave. We retain ZERO\n"
        "copies. Losing {t}+ shares means NO recovery (re-init + re-enter\n"
        "every source credential) — the runbook says this too."),
}


def ceremony_text(custody: str,
                  shares: int = BAO_SHARES,
                  threshold: int = BAO_THRESHOLD) -> str:
    if custody == "auto":
        raise ApplyError(
            "custody=auto (KMS auto-unseal) is Shape-B hosted work — not "
            "supported by this installer yet (§8.9 items 2–5)")
    return CEREMONIES[custody].format(n=shares, t=threshold)


def confirm_recorded(what: str,
                     input_fn: Callable[[str], str] = input,
                     is_tty: Optional[bool] = None) -> None:
    """The print-once acknowledgment gate (B2): after secrets print, block
    until a human types RECORDED. Print-once values exist NOWHERE else —
    scrolling past them in a self-closing terminal is unrecoverable.
    Non-interactive runs (tests, pipes) skip the gate: there is no human to
    hold, and holding would hang automation.

    LOCAL POSTURE SKIPS IT (d.s Stage 2). This is the one function every
    print-once path funnels through — vault unseal shares, the operator console
    credential, the agent serving credential — so gating it here covers all
    three at one site instead of three.

    WHY IT IS RIGHT TO SKIP, stated carefully, because the obvious reason is
    wrong. It is NOT that the value is recoverable from the local store: that
    store keeps sha256 DIGESTS of credentials, exactly as the vault keeps them
    in its paths, so a token cannot be read back out in either posture.

    The real reason is that what the gate protects against is not losing a
    value, it is losing a value that is EXPENSIVE to replace. In deployed
    posture, losing the unseal shares means re-initializing the vault and
    re-entering every source credential, and losing an operator credential means
    a site visit or a custody ceremony to issue another. In local posture
    replacing anything is one command with no ceremony attached
    (`khctl provision-operator` / `provision-agent`), and the console does not
    need a credential at all — it logs itself in. A blocking prompt guarding a
    loss that costs one command is ceremony in the pure sense.

    What this does NOT do is stop the value from being PRINTED. The record of
    what was minted still goes to stdout and into operator.log, and the callers
    still say "record NOW". Skipping a human-attention prompt is not the same as
    dropping the audit line, and only the first is ceremony.
    """
    if settings.is_local:
        print(f"      (local posture — not holding for an acknowledgment: "
              f"the {what} above is NOT stored anywhere and cannot be "
              f"recovered, but re-issuing one is a single command with no "
              f"ceremony)")
        return
    if is_tty is None:
        try:
            is_tty = sys.stdin.isatty()
        except Exception:
            is_tty = False
    if not is_tty:
        return
    while True:
        try:
            answer = input_fn(f"  --> type RECORDED once you have saved the "
                              f"{what} above: ").strip().upper()
        except EOFError:
            # isatty() can lie (Windows null-device stdin still reports a
            # console handle) — nothing readable means nothing to hold for;
            # the values are printed above either way.
            print("      (stdin closed — cannot hold for confirmation; "
                  "the values above are your only copy)")
            return
        if answer == "RECORDED":
            return
        print(f"      not confirmed — the {what} above exist nowhere else; "
              f"record them, then type RECORDED")


# ---------------------------------------------------------------------------
# Phase implementations
# ---------------------------------------------------------------------------
def _compose(ctx: ApplyContext, *args: str) -> subprocess.CompletedProcess:
    files = ["-f", "docker-compose.yml"]
    if "openbao" in ctx.plan.compose_services():
        files += ["-f", PROD_BAO_COMPOSE]
    return subprocess.run(["docker", "compose", *files, *args],
                          cwd=ctx.infra_dir, capture_output=True, text=True,
                          timeout=600)


def phase_kit(ctx: ApplyContext) -> list[str]:
    lines = []
    manifest_path = ctx.kit_dir / "manifest.json"
    if manifest_path.exists():
        # Signature FIRST (deploy_kit owns the trust anchor; lazy import —
        # deploy_kit imports this module at top level). A bad/untrusted
        # signature raises; unsigned is tolerated at apply with a warning
        # (the hard gate is verify-kit / install.sh on arrival).
        from knowledge_hub.deploy_kit import verify_manifest_signature
        key_id = verify_manifest_signature(ctx.kit_dir)
        lines.append(
            f"manifest signature verified (trusted key {key_id!r})"
            if key_id else
            "[WARN] kit manifest is UNSIGNED — dev bench only; arrival "
            "gate (verify-kit) refuses this without --allow-unsigned")
        # L3: this phase re-hashes the whole kit — on a full ~60GB kit that
        # is minutes of silence, and silence reads as a hang. Printed (not
        # returned) so it lands BEFORE the hashing, like verify-kit's.
        total = sum(a.get("bytes", 0) for a in json.loads(
            manifest_path.read_text(encoding="utf-8")).get("artifacts", []))
        if total > 5e9:
            print(f"       (hashing {total / 1e9:.0f}GB of kit artifacts — "
                  f"a full kit takes a few minutes; silence here is work, "
                  f"not a hang)")
    return lines + verify_kit_manifest(ctx.kit_dir)


def phase_preflight(ctx: ApplyContext) -> list[str]:
    lines = []
    if ctx.plan.compose_services():
        for tool in (["docker", "version"], ["docker", "compose", "version"]):
            try:
                ok = subprocess.run(tool, capture_output=True,
                                    timeout=30).returncode == 0
            except Exception:
                ok = False
            if not ok:
                raise ApplyError(
                    f"{' '.join(tool[:2])} not available — Docker Engine + "
                    f"the compose plugin must be PRE-INSTALLED before going "
                    f"offline (see PREREQS.txt at the SSD root); this kit "
                    f"cannot install them")
        lines.append("docker + compose present")
    free_gb = shutil.disk_usage(ctx.infra_dir).free / 1024**3
    if free_gb < MIN_DISK_GB:
        raise ApplyError(f"only {free_gb:.0f}GB free (< {MIN_DISK_GB}GB "
                         f"floor) — models alone are tens of GB")
    lines.append(f"disk: {free_gb:.0f}GB free")
    return lines


def phase_env(ctx: ApplyContext) -> list[str]:
    ctx.env = parse_env_file(ctx.env_file)
    target = ctx.infra_dir / ".env"
    # B3 guard: render_env always emits the pilot placeholder token, but a
    # DEPLOYED home's .env carries the real root token phase_openbao minted
    # at init. A repair/re-plan must never copy the placeholder over it —
    # afterwards every vault call fails and the box presents as mass
    # credential revocation.
    preserved: Optional[str] = None
    preserved_s3: Optional[tuple[str, str]] = None
    preserved_roles: list[str] = []
    if target.exists():
        deployed = parse_env_file(target)
        existing = deployed.get("BAO_ROOT_TOKEN", "")
        if (existing and existing != PILOT_PLACEHOLDER_TOKEN
                and ctx.env.get("BAO_ROOT_TOKEN")
                == PILOT_PLACEHOLDER_TOKEN):
            preserved = existing
            ctx.env["BAO_ROOT_TOKEN"] = existing
        # BP28 #21, the B3 counterpart for S3: render_env mints a FRESH
        # pair on every plan, but a deployed box's SeaweedFS holds the
        # pair it started with (s3config.json is read at container start).
        # ANY deployed pair wins — rotation is a deliberate day-2 op,
        # never a re-plan side effect that strands a live object store.
        old_ak = deployed.get("S3_ACCESS_KEY", "")
        old_sk = deployed.get("S3_SECRET_KEY", "")
        if old_ak and old_sk and (
                (old_ak, old_sk) != (ctx.env.get("S3_ACCESS_KEY"),
                                     ctx.env.get("S3_SECRET_KEY"))):
            preserved_s3 = (old_ak, old_sk)
            ctx.env["S3_ACCESS_KEY"] = old_ak
            ctx.env["S3_SECRET_KEY"] = old_sk
        # §8.8, the same counterpart for the least-privilege role passwords:
        # render_env mints fresh ones every plan, but a live box has running
        # services holding open connections under those credentials.
        # phase_schema re-asserts whatever ends up here via ALTER ROLE, so
        # preserving is consistent — and rotating on a re-plan would drop
        # every serving and operator connection mid-flight.
        from knowledge_hub.roles import PASSWORD_ENV

        for var in PASSWORD_ENV.values():
            existing_pw = deployed.get(var, "")
            if existing_pw and existing_pw != ctx.env.get(var):
                preserved_roles.append(var)
                ctx.env[var] = existing_pw
    if ctx.dry_run:
        return [f"[dry-run] would install {ctx.env_file.name} -> .env "
                f"({len(ctx.env)} settings)"
                + (", preserving the deployed vault root token"
                   if preserved else "")
                + (", preserving the deployed S3 credential pair"
                   if preserved_s3 else "")
                + (f", preserving {len(preserved_roles)} deployed role "
                   f"password(s)" if preserved_roles else "")]
    lines = []
    if target.exists() and target.read_text(encoding="utf-8") != \
            ctx.env_file.read_text(encoding="utf-8"):
        backup = ctx.infra_dir / ".env.bak"
        if not backup.exists():
            shutil.copy2(target, backup)
            lines.append("existing .env backed up to .env.bak")
    shutil.copy2(ctx.env_file, target)
    if preserved is not None:
        content = [line for line in
                   target.read_text(encoding="utf-8").splitlines()
                   if not line.startswith("BAO_ROOT_TOKEN=")]
        content.append(f"BAO_ROOT_TOKEN={preserved}")
        target.write_text("\n".join(content) + "\n", encoding="utf-8")
        lines.append("deployed vault ROOT TOKEN preserved — the plan's "
                     "pilot placeholder never overwrites a live vault's "
                     "token")
    if preserved_s3 is not None:
        content = [line for line in
                   target.read_text(encoding="utf-8").splitlines()
                   if not line.startswith(("S3_ACCESS_KEY=",
                                           "S3_SECRET_KEY="))]
        content.append(f"S3_ACCESS_KEY={preserved_s3[0]}")
        content.append(f"S3_SECRET_KEY={preserved_s3[1]}")
        target.write_text("\n".join(content) + "\n", encoding="utf-8")
        lines.append("deployed S3 credentials preserved — a re-plan's "
                     "fresh mint never strands a live object store")
    if preserved_roles:
        prefixes = tuple(f"{var}=" for var in preserved_roles)
        content = [line for line in
                   target.read_text(encoding="utf-8").splitlines()
                   if not line.startswith(prefixes)]
        content += [f"{var}={ctx.env[var]}" for var in preserved_roles]
        target.write_text("\n".join(content) + "\n", encoding="utf-8")
        lines.append(f"deployed role password(s) preserved "
                     f"({', '.join(preserved_roles)}) — a re-plan's fresh "
                     f"mint never drops live serving/operator connections")
    lines.append(f".env installed from {ctx.env_file.name} "
                 f"({len(ctx.env)} settings)")
    return lines


# Docker's port-bind failure texts across engines/platforms (BP34): the
# collision must answer as the non-disruption guarantee refusing to fight
# for a port, never as a generic compose failure.
_BIND_CONFLICT_MARKERS = ("port is already allocated",
                          "address already in use",
                          "bind for 127.0.0.1", "bind: only one usage")


def _bind_conflict(stderr: str) -> bool:
    lowered = stderr.lower()
    return any(marker in lowered for marker in _BIND_CONFLICT_MARKERS)


# Compose service -> its fixed container_name in docker-compose.yml.
_SERVICE_CONTAINERS = {"postgres": "kh-postgres",
                       "seaweedfs": "kh-seaweedfs",
                       "openbao": "kh-openbao"}
SERVICE_READY_TIMEOUT_S = 60
# A container that restarted twice while we watched is looping, not warming
# up — fail NOW with the truth instead of waiting out the deadline.
_RESTART_LOOP_FLOOR = 2


def _docker_inspect(container: str) -> Optional[tuple[int, str]]:
    """(RestartCount, State.Status) for a container, or None when docker /
    the container cannot be inspected (the probe verdict then stands
    alone)."""
    try:
        out = subprocess.run(
            ["docker", "inspect", "-f",
             "{{.RestartCount}} {{.State.Status}}", container],
            capture_output=True, text=True, timeout=15)
        if out.returncode != 0:
            return None
        count_s, _, status = out.stdout.strip().partition(" ")
        return int(count_s), status
    except Exception:
        return None


def render_s3_config(ctx: ApplyContext) -> tuple[bool, list[str]]:
    """SeaweedFS reads its identities from seaweedfs/s3config.json — a file
    the kit deliberately does NOT ship (it is credential-bearing; see
    deploy_kit.FORBIDDEN_NAMES). Render it here from the same .env pair
    every S3 client uses, BEFORE compose up: with no real file at the
    bind-mount source Docker creates a DIRECTORY there and the S3 server
    fatals silently (BP28 #19). Returns (content_changed, lines)."""
    access = ctx.env.get("S3_ACCESS_KEY")
    secret = ctx.env.get("S3_SECRET_KEY")
    if not access or not secret:
        raise ApplyError(
            "S3_ACCESS_KEY/S3_SECRET_KEY missing from the plan's env — "
            "cannot render seaweedfs/s3config.json; re-run khctl plan")
    target = ctx.infra_dir / "seaweedfs" / "s3config.json"
    lines = []
    if target.is_dir():
        shutil.rmtree(target)
        lines.append("removed a DIRECTORY at seaweedfs/s3config.json (the "
                     "docker-created poison state) — a real file replaces it")
    content = json.dumps(
        {"identities": [{"name": "kh_admin",
                         "credentials": [{"accessKey": access,
                                          "secretKey": secret}],
                         "actions": ["Admin"]}]}, indent=2) + "\n"
    changed = (not target.exists()
               or target.read_text(encoding="utf-8") != content)
    if changed:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        lines.append("seaweedfs/s3config.json rendered from the .env "
                     "credential pair")
    return changed, lines


def _service_probe(ctx: ApplyContext, service: str) -> Optional[Callable]:
    """The real client-path probe for a compose service (deploy_probe's —
    reused, not reinvented). openbao gets reachability only: init/unseal
    is phase_openbao's job."""
    from knowledge_hub import deploy_probe as dp
    env = ctx.env
    if service == "postgres":
        return lambda: dp.probe_postgres(dsn_from_env(env))
    if service == "seaweedfs":
        return lambda: dp.probe_object_store(
            env.get("S3_ENDPOINT", "http://localhost:8333"),
            env.get("S3_ACCESS_KEY", ""), env.get("S3_SECRET_KEY", ""))
    if service == "openbao":
        return lambda: dp.probe_secrets(
            env.get("BAO_ADDR", "http://localhost:8200"))
    return None


def _await_service_ready(ctx: ApplyContext, service: str) -> str:
    """The readiness principle (BP28 #19): a phase VERIFIES its services
    actually answer instead of trusting `compose up`'s exit code —
    SeaweedFS restart-looped 120x through nine OK phases because nothing
    here ever asked it anything."""
    probe = _service_probe(ctx, service)
    container = _SERVICE_CONTAINERS.get(service, f"kh-{service}")
    if probe is None:
        return f"{service}: up (no readiness probe wired for this service)"
    deadline = time.monotonic() + SERVICE_READY_TIMEOUT_S
    last_error = "no probe attempt yet"
    while True:
        inspected = _docker_inspect(container)
        if inspected and inspected[0] >= _RESTART_LOOP_FLOOR:
            raise ApplyError(
                f"service '{service}' (container {container}) is "
                f"restart-looping (RestartCount={inspected[0]}, "
                f"status={inspected[1]}) — it starts and dies immediately; "
                f"last probe: {last_error} — inspect it: "
                f"docker logs {container} --tail 50")
        report = probe()
        if report.reachable:
            return f"{service} ready (answered its client-path probe)"
        last_error = report.error or "not reachable"
        if time.monotonic() >= deadline:
            raise ApplyError(
                f"service '{service}' (container {container}) is not ready "
                f"after {SERVICE_READY_TIMEOUT_S}s: {last_error} — "
                f"inspect it: docker logs {container} --tail 50")
        time.sleep(2)


def phase_services(ctx: ApplyContext) -> list[str]:
    services = ctx.plan.compose_services()
    if not services:
        return ["no 'ours' services in plan — all adopted client-side"]
    prod_bao = "openbao" in services
    if ctx.dry_run:
        return [f"[dry-run] would render seaweedfs/s3config.json from the "
                f"plan's env (seaweedfs ours), docker-load kit images from "
                f"{ctx.kit_dir / 'images'} (if present), compose up: "
                f"{', '.join(services)}"
                + (f" (with {PROD_BAO_COMPOSE} — production raft vault)"
                   if prod_bao else "")
                + ", then wait until every service ANSWERS its probe"]
    lines = []
    s3_changed = seaweed_was_running = False
    if "seaweedfs" in services:
        inspected = _docker_inspect(_SERVICE_CONTAINERS["seaweedfs"])
        seaweed_was_running = bool(inspected and inspected[1] == "running")
        s3_changed, s3_lines = render_s3_config(ctx)
        lines += s3_lines
    images = sorted((ctx.kit_dir / "images").glob("*.tar")) \
        if (ctx.kit_dir / "images").exists() else []
    for tar in images:
        out = subprocess.run(["docker", "load", "-i", str(tar)],
                             capture_output=True, text=True, timeout=600)
        if out.returncode != 0:
            raise ApplyError(f"docker load {tar.name}: {out.stderr.strip()}")
        lines.append(f"loaded image {tar.name}")
    if not images:
        lines.append("no kit images/ — compose will build/pull "
                     "(needs egress or cached layers)")
    # Never --build: the kit compose references LOADED images only (BP28
    # #10) — a clean box has no build contexts and no egress to pull with.
    out = _compose(ctx, "up", "-d", *services)
    if out.returncode != 0:
        err = out.stderr.strip()
        if _bind_conflict(err):
            raise ApplyError(
                f"compose up hit a PORT COLLISION — another service on this "
                f"box already holds a host port this stack binds (a "
                f"co-resident client Postgres on 5432 is the usual cause). "
                f"The existing service was NOT touched — this refusal IS "
                f"the non-disruption guarantee. Re-run the launcher and "
                f"pick a free port at its prompt (or re-plan with --use "
                f"postgres=ours:<port>). Docker said: …{err[-300:]}")
        raise ApplyError(f"compose up failed: {err[-500:]}")
    lines.append(f"compose up: {', '.join(services)}"
                 + (" (production raft vault)" if prod_bao else ""))
    if s3_changed and seaweed_was_running:
        _compose(ctx, "restart", "seaweedfs")
        lines.append("seaweedfs restarted — it reads s3config.json only "
                     "at container start")
    for service in services:
        lines.append(_await_service_ready(ctx, service))
    return lines


def phase_schema(ctx: ApplyContext) -> list[str]:
    dsn = dsn_from_env(ctx.env)
    schema_file = ctx.infra_dir / "knowledge_hub_baseline_schema.sql"
    migrations_dir = ctx.infra_dir / "migrations"
    migrations = sorted(migrations_dir.glob("*.sql"))
    if ctx.dry_run:
        # No connection on a dry run, by contract: the walk happens with the
        # services phase not yet run, so there may be no Postgres to ask. Real
        # ledger-vs-database state comes from `khctl migrations status`.
        return [f"[dry-run] would ensure baseline schema + replay "
                f"{len(migrations)} migration(s) against "
                f"{ctx.env.get('POSTGRES_HOST', 'localhost')}:"
                f"{ctx.env.get('POSTGRES_PORT', '5432')}",
                "[dry-run] (ledger drift is checked wet, not here — "
                "`khctl migrations status` reports it read-only)"]
    import psycopg

    from knowledge_hub import migrations as mig

    lines = []
    # connect_timeout: same dual-stack localhost black-hole class as
    # factstore_pg._conn — bounded fall-through beats an infinite wedge
    # inside the self-closing deploy window (SANITY_CHECK_FINDINGS §apply).
    with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
        tables = conn.execute(
            "SELECT count(*) FROM pg_tables WHERE schemaname='public'"
        ).fetchone()[0]
        if tables < 5:
            # ag_catalog must be ON the search_path for create_graph() to
            # resolve, and LAST so new objects land in public (NOTES.md).
            conn.execute("SET search_path = public, ag_catalog;")
            conn.execute(schema_file.read_text(encoding="utf-8"))
            lines.append("baseline schema applied")
        else:
            lines.append(f"schema already applied ({tables} public tables)")
        mig.ensure_ledger(conn)
        # THE GATE (added after the 2026-08-03 pilot finding): classify every
        # file against the ledger AND the live objects BEFORE replaying
        # anything. Previously this loop trusted the ledger alone, so a
        # migration whose DDL had arrived out-of-band was replayed and died on
        # a raw Postgres DuplicateTable, aborting the phase with 012/013 never
        # reached. Drift is a state a human reconciles, not one apply patches.
        statuses = mig.status(conn, migrations_dir, schema_file)
        if mig.broken(statuses):
            raise ApplyError(mig.drift_message(statuses))
        applied_n = 0
        for status in mig.pending(statuses):
            conn.execute("SET search_path = public, ag_catalog;")
            conn.execute(status.file.path.read_text(encoding="utf-8"))
            conn.execute(
                "INSERT INTO schema_migrations (filename) VALUES (%s)",
                (status.filename,))
            applied_n += 1
        lines.append(f"migrations: {applied_n} applied, "
                     f"{len(migrations) - applied_n} already present")
        # Roles LAST, and every run — not once. They must be granted over
        # the tables that exist AFTER this apply's migrations, or the newest
        # table is the one kh_serving cannot read. Idempotent by
        # construction; see roles.py on why this is not a numbered file.
        from knowledge_hub import roles as kh_roles

        lines.extend(kh_roles.ensure_serving_roles(
            conn, kh_roles.passwords_from_env(ctx.env)))
    return lines


# BP28 #12/#16: the vault gets the same readiness discipline Postgres always
# had. 60s reachability (a raft vault reads its store and stands up a
# listener — slower than dev mode), 60s for raft to elect a leader (writes
# before that answer HTTP 500).
VAULT_READY_TIMEOUT_S = 60
VAULT_LEADER_TIMEOUT_S = 60
_VAULT_POLL_INTERVAL_S = 2.0


def _await_vault_ready(ctx: ApplyContext, addr: str) -> str:
    """Poll the vault's unauthenticated health endpoint until it answers
    (BP28 #12: Postgres got a 60s wait, the vault got none — three bare
    tracebacks on a normally-starting raft vault). Reuses the phase_services
    tripwire: a crash-looping container fails NOW with the truth instead of
    waiting out the deadline."""
    from knowledge_hub.deploy_probe import probe_secrets
    container = _SERVICE_CONTAINERS["openbao"]
    deadline = time.monotonic() + VAULT_READY_TIMEOUT_S
    announced = False
    while True:
        inspected = _docker_inspect(container)
        if inspected and inspected[0] >= _RESTART_LOOP_FLOOR:
            raise ApplyError(
                f"the vault container {container} is restart-looping "
                f"(RestartCount={inspected[0]}, status={inspected[1]}) — "
                f"it starts and dies immediately; inspect it: "
                f"docker logs {container} --tail 50")
        report = probe_secrets(addr)
        if report.reachable:
            return (f"vault answering at {addr} "
                    f"(initialized={report.initialized}, "
                    f"sealed={report.sealed})")
        if time.monotonic() >= deadline:
            raise ApplyError(
                f"vault at {addr} not answering after "
                f"{VAULT_READY_TIMEOUT_S}s: {report.error} — inspect it: "
                f"docker logs {container} --tail 50, then re-run")
        if not announced:
            print(f"       (waiting for vault at {addr} — up to "
                  f"{VAULT_READY_TIMEOUT_S}s; a raft vault takes a few "
                  f"seconds to stand up)")
            announced = True
        time.sleep(_VAULT_POLL_INTERVAL_S)


def _await_vault_leader(client) -> str:
    """Block until raft has elected a leader (BP28 #16: phase_openbao wrote
    to the vault the instant it was unsealed and got HTTP 500 — on
    single-node raft the election takes a beat). sys/leader is
    unauthenticated; ANY error while polling counts as not-ready and is
    retried, because this runs between 'vault answers' and 'vault is
    writable' where 500s and dropped connections are the normal weather."""
    deadline = time.monotonic() + VAULT_LEADER_TIMEOUT_S
    last_error = "no leader-status answer yet"
    while True:
        try:
            status = client.sys.read_leader_status()
            if status.get("is_self") or status.get("leader_address"):
                return "raft leader elected — vault is writable"
            last_error = "leader election still in progress"
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
        if time.monotonic() >= deadline:
            raise ApplyError(
                f"raft has not elected a leader after "
                f"{VAULT_LEADER_TIMEOUT_S}s ({last_error}) — the vault is "
                f"up but not yet writable. Wait a moment and re-run; if it "
                f"persists: docker logs kh-openbao --tail 50. This is NOT "
                f"a token problem — do not change BAO_ROOT_TOKEN.")
        time.sleep(_VAULT_POLL_INTERVAL_S)


def phase_openbao(ctx: ApplyContext) -> list[str]:
    if "secrets" not in ctx.plan.seams:
        return ["no client-side secrets seam in plan (hosted footprint)"]
    custody = ctx.plan.secrets_custody
    addr = ctx.env.get("BAO_ADDR", "http://localhost:8200")
    if ctx.dry_run:
        ceremony_text(custody)  # raises early on custody=auto
        return [f"[dry-run] would init {addr} ({BAO_SHARES} shares / "
                f"threshold {BAO_THRESHOLD}) if uninitialized, run the "
                f"'{custody}' custody ceremony, unseal, mount KV v2"]
    import hvac
    client = hvac.Client(url=addr)
    # BP28 #12: wait for the vault to ANSWER before asking it anything —
    # is_initialized() on a still-starting vault raised a raw
    # requests.ConnectionError past every friendly handler.
    lines = [_await_vault_ready(ctx, addr)]
    if not client.sys.is_initialized():
        # d.s Stage 2: the CUSTODY CEREMONY is the skippable part, not the
        # init. Initializing and unsealing are mechanical — the vault does not
        # work without them. What is ceremony is the custody script: print five
        # shares, seal them in envelopes, hand them to someone, have them
        # test-unseal before you leave. That whole apparatus answers a question
        # local posture does not ask — who besides us can bring this back up —
        # because the answer is "the one person on this box".
        #
        # Note this branch is dormant on the bench anyway: dev-mode OpenBao
        # (`server -dev`, docker-compose.yml) comes up already initialized and
        # unsealed, so is_initialized() is True and none of this runs. The gate
        # is here for the case that made it worth writing — someone running the
        # prod raft compose locally, who would otherwise be walked through a
        # client-custody ceremony for their own laptop.
        ceremony = (ceremony_text(custody) if settings.is_deployed else None)
        result = client.sys.initialize(secret_shares=BAO_SHARES,
                                       secret_threshold=BAO_THRESHOLD)
        # Shares + root token go to STDOUT ONCE and are never written to
        # disk — writing them anywhere durable defeats the custody model.
        print("\n" + "=" * 70)
        if ceremony:
            print(ceremony)
        else:
            print("VAULT INITIALIZED — custody ceremony skipped (local "
                  "posture).\nSingle-user internal box: no share handoff, no "
                  "envelopes, no witness.\nThe shares below are still your "
                  "only way to unseal after a restart.")
        print("-" * 70)
        for i, key in enumerate(result["keys_base64"], 1):
            print(f"  unseal share {i}/{BAO_SHARES}: {key}")
        print(f"  root token (bootstrap only): {result['root_token']}")
        print("=" * 70 + "\n")
        # B2: the most losable-forever secrets in the system never scroll
        # away unacknowledged.
        confirm_recorded(f"{BAO_SHARES} unseal shares + root token")
        for key in result["keys_base64"][:BAO_THRESHOLD]:
            client.sys.submit_unseal_key(key)
        # The stack needs the token to operate; day-2 hardening (scoped
        # admin token + root revocation) is bookmarked in DEPLOY_NOTES.
        ctx.env["BAO_ROOT_TOKEN"] = result["root_token"]
        env_path = ctx.infra_dir / ".env"
        content = env_path.read_text(encoding="utf-8").splitlines()
        content = [line for line in content
                   if not line.startswith("BAO_ROOT_TOKEN=")]
        content.append(f"BAO_ROOT_TOKEN={result['root_token']}")
        env_path.write_text("\n".join(content) + "\n", encoding="utf-8")
        lines.append(f"initialized ({BAO_SHARES} shares / threshold "
                     f"{BAO_THRESHOLD}), custody={custody}, unsealed, "
                     f".env token updated")
    else:
        if client.sys.is_sealed():
            raise ApplyError(
                f"vault at {addr} is SEALED — unseal with {BAO_THRESHOLD} "
                f"of the {BAO_SHARES} custody shares "
                f"(`{UNSEAL_COMMAND}`, run {BAO_THRESHOLD}x), then re-run")
        lines.append("already initialized + unsealed (idempotent skip)")
    # BP28 #16: unsealed is NOT writable — raft must elect a leader first,
    # or the KV-mount write below answers HTTP 500.
    lines.append(_await_vault_leader(client))
    token = ctx.env.get("BAO_ROOT_TOKEN", "")
    authed = hvac.Client(url=addr, token=token)
    mounts = authed.sys.list_mounted_secrets_engines()
    mount = ctx.env.get("BAO_KV_MOUNT", "secret")
    if f"{mount}/" not in mounts:
        authed.sys.enable_secrets_engine(
            "kv", path=mount, options={"version": "2"})
        lines.append(f"KV v2 mounted at {mount}/")
    else:
        lines.append(f"KV v2 mount {mount}/ present")
    return lines


def _ollama_store_target() -> tuple[Path, bool]:
    """Where THIS box's ollama reads its model store, and whether writing
    there needs root. The PREREQS install path on Ubuntu creates a systemd
    service running as user `ollama`, which reads
    /usr/share/ollama/.ollama/models — a store copied to $HOME is invisible
    to it (BP28 #18). A box without that service (dev bench, user-level
    ollama) uses the user store. Known limit: a unit overriding
    OLLAMA_MODELS in its Environment is not honored — acceptable for the
    appliance profile (DEPLOY_NOTES)."""
    for verb in ("is-active", "is-enabled"):
        try:
            if subprocess.run(["systemctl", verb, "ollama"],
                              capture_output=True,
                              timeout=15).returncode == 0:
                return Path("/usr/share/ollama/.ollama/models"), True
        except Exception:
            break  # no systemctl at all — nothing more to probe
    return Path.home() / ".ollama" / "models", False


def _store_complete(kit_store: Path, target: Path) -> bool:
    """Same relative path + same size = same bytes (the store is
    content-addressed; stage_models hash-proved every blob at build time).
    True only when EVERY kit file is already in the target store — the
    signal that lets a retry skip the ~57GB copy (BP28 #18)."""
    try:
        for src in kit_store.rglob("*"):
            if not src.is_file():
                continue
            dst = target / src.relative_to(kit_store)
            if not dst.exists() or dst.stat().st_size != src.stat().st_size:
                return False
    except OSError:
        return False
    return True


def _ensure_root_session() -> bool:
    """Cache a sudo credential for the system-store install. `sudo -n true`
    first (cached / passwordless); otherwise `sudo -v` WITHOUT capture so
    the password prompt reaches the operator's terminal (launch.sh runs
    interactively — the BP28 lesson about prompts eating pasted blocks)."""
    try:
        if subprocess.run(["sudo", "-n", "true"], capture_output=True,
                          timeout=15).returncode == 0:
            return True
        print("       (root is needed once — the system ollama's store is "
              "root-owned; sudo prompts now)")
        return subprocess.run(["sudo", "-v"], timeout=300).returncode == 0
    except Exception:
        return False


def _install_store_as_root(kit_store: Path, target: Path) -> None:
    """Install the kit store into the system ollama's store as root.
    `cp -a -n`: archive copy that skips files already present. NEVER
    `cp -al`: hard links share inodes, so the chown below would silently
    rewrite the kit copy's ownership and break the next apply (runbook
    §5b lesson)."""
    manual = (f"sudo systemctl stop ollama\n"
              f"    sudo mkdir -p {target}\n"
              f"    sudo cp -a {kit_store}/. {target}/\n"
              f"    sudo chown -R ollama:ollama {target.parent}\n"
              f"    sudo systemctl start ollama")
    if not _ensure_root_session():
        raise ApplyError(
            f"the system ollama reads {target} (root-owned) and sudo is "
            f"not available to this session — install the store manually:\n"
            f"    {manual}\nthen re-run")
    for argv in (["mkdir", "-p", str(target)],
                 ["cp", "-a", "-n", f"{kit_store}/.", f"{target}/"],
                 ["chown", "-R", "ollama:ollama", str(target.parent)]):
        out = subprocess.run(["sudo", "-n", *argv], capture_output=True,
                             text=True, timeout=7200)
        if out.returncode != 0:
            raise ApplyError(
                f"sudo {' '.join(argv[:2])} … failed: "
                f"{(out.stderr or '').strip()[-300:]} — finish manually:\n"
                f"    {manual}\nthen re-run")


def _restart_ollama() -> bool:
    """Best-effort ollama restart after a model-store copy (F13): freshly
    copied manifests are only visible after a reload. Tries plain systemctl
    first, then a passwordless sudo; a box where neither works gets the
    honest restart-and-re-run message from phase_models instead."""
    for argv in (["systemctl", "restart", "ollama"],
                 ["sudo", "-n", "systemctl", "restart", "ollama"]):
        try:
            if subprocess.run(argv, capture_output=True,
                              timeout=120).returncode == 0:
                return True
        except Exception:
            continue
    return False


def phase_models(ctx: ApplyContext) -> list[str]:
    inference = ctx.plan.seams.get("inference")
    if inference and inference.choice == "local-external":
        # BP46 Fix 5: on-premises, but not ours to install. Say both halves —
        # "no models to install" alone would read like the remote case and
        # lose the fact that nothing leaves the box.
        return [f"local-external inference at {inference.endpoint}: the "
                f"operator supplied this endpoint and its models are NOT "
                f"installed by us (text still never leaves this box)"]
    if not inference or inference.choice != "local":
        return ["remote inference — no local models to install"]
    required = [m for m in {ctx.env.get("EMBEDDING_MODEL", "bge-m3"),
                            ctx.env.get("EXTRACTION_MODEL", "qwen3.6")} if m]
    kit_store = ctx.kit_dir / "ollama_models"
    host = ctx.env.get("OLLAMA_HOST", "http://localhost:11434")
    if ctx.dry_run:
        if kit_store.exists():
            target, needs_root = _ollama_store_target()
            source = (f"copy the kit store {kit_store} -> {target}"
                      + (" (as root — the system ollama owns it)"
                         if needs_root else "")
                      + " unless already present, restart ollama, then ")
        else:
            source = "(no kit ollama_models/) "
        return [f"[dry-run] would {source}confirm {required} "
                f"served at {host}"]
    from knowledge_hub.deploy_probe import probe_ollama
    lines = []
    store_in_place = False
    if kit_store.exists():
        target, needs_root = _ollama_store_target()
        if _store_complete(kit_store, target):
            # BP28 #18: a retry after a failed later phase must never
            # re-copy the ~57GB store.
            store_in_place = True
            lines.append(f"model store already present at {target} — "
                         f"copy skipped")
        else:
            size_gb = sum(p.stat().st_size for p in kit_store.rglob("*")
                          if p.is_file()) / 1e9
            # L3: printed (not returned) so it lands BEFORE the long copy.
            print(f"       (copying the {size_gb:.0f}GB model store from "
                  f"the kit — this takes a while; silence here is work, "
                  f"not a hang)")
            if needs_root:
                # BP28 #18: a systemd ollama reads /usr/share/ollama, not
                # $HOME — copy where it actually looks, owned by its user.
                _install_store_as_root(kit_store, target)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(kit_store, target, dirs_exist_ok=True)
            store_in_place = True
            # F13: ollama only sees the new manifests after a reload —
            # restart it HERE instead of failing the probe below with a
            # wrong message.
            if _restart_ollama():
                for _ in range(15):
                    if probe_ollama(host).reachable:
                        break
                    time.sleep(2)
                lines.append(f"model store copied from kit -> {target}; "
                             f"ollama restarted to pick up the new "
                             f"manifests")
            else:
                lines.append(f"model store copied from kit -> {target} "
                             f"(could not restart ollama automatically — "
                             f"if the check below fails: `sudo systemctl "
                             f"restart ollama`, then re-run)")
    report = probe_ollama(host)
    if not report.reachable:
        raise ApplyError(
            f"ollama unreachable at {host}: {report.error} — ollama must be "
            f"pre-installed and running BEFORE going offline (PREREQS.txt); "
            f"start it (`sudo systemctl start ollama`), then re-run")
    missing = [m for m in required
               if not any(t == m or t.startswith(f"{m}:")
                          for t in report.models)]
    if missing:
        if store_in_place:
            raise ApplyError(
                f"model(s) {missing} still not served although the kit "
                f"store is in place — ollama has not reloaded its "
                f"manifests; restart it (`sudo systemctl restart ollama`) "
                f"and re-run this phase")
        raise ApplyError(
            f"required model(s) missing: {missing} and this kit carries no "
            f"ollama_models/ store — pre-load the models before going "
            f"offline, or (with egress) `ollama pull {' '.join(missing)}`")
    lines.append(f"models present at {host}: {', '.join(required)}")
    return lines


def phase_python(ctx: ApplyContext) -> list[str]:
    from knowledge_hub.checks import check_version
    return [check_version()]


# The console roles the provisioning path may mint (mirrors the operator
# service's write roles — reviewer resolves reviews, operator also controls
# ingestion + sources).
OPERATOR_CREDENTIAL_ROLES = ("operator", "reviewer")


def provision_operator_credential(client, mount: str, tenant: str, role: str,
                                  actor: str) -> tuple[str, str]:
    """Mint + register ONE console credential (BP23). Returns
    (token, principal_id) — the caller owns the print-once ceremony.

    The token exists exactly twice, ever: hashed (sha256) as the registry
    path in the vault, and on the caller's terminal. The registry RECORD
    carries the identity triple the resolver reads plus attribution
    (who provisioned, when) — never the token value. Nothing here writes
    to disk or logs."""
    if role not in OPERATOR_CREDENTIAL_ROLES:
        raise ApplyError(f"operator credential role must be one of "
                         f"{OPERATOR_CREDENTIAL_ROLES}, got {role!r}")
    from datetime import datetime, timezone

    from knowledge_hub.choke_point import OpenBaoCredentialResolver

    token = f"kh-{role}-{tenant}-{pysecrets.token_hex(16)}"
    principal_id = f"{tenant}-{role}-{pysecrets.token_hex(3)}"
    client.secrets.kv.v2.create_or_update_secret(
        mount_point=mount,
        path=OpenBaoCredentialResolver.path_for(token),
        secret={"tenant_id": tenant, "principal_id": principal_id,
                "roles": [role],
                # Attribution rides the record; the resolver reads only the
                # identity triple above and ignores these.
                "provisioned_by": actor,
                "provisioned_at": datetime.now(timezone.utc).isoformat()})
    return token, principal_id


def provision_agent_credential(client, mount: str, tenant: str, actor: str,
                               principal_id: Optional[str] = None
                               ) -> tuple[str, str]:
    """Mint + register ONE agent SERVING credential — the same registry
    write phase_tenants bootstraps with, callable day-2 as the re-mint path
    (F16: the credential external agents need must be re-issuable without
    hand-rolled hvac). Roles are empty by design: agent principals read
    through the serving boundary and can perform no operator write. Same
    custody truths as the operator path: the value exists only hashed in
    the vault and on the caller's terminal."""
    from knowledge_hub.choke_point import OpenBaoCredentialResolver

    token = f"kh-{tenant}-{pysecrets.token_hex(16)}"
    pid = principal_id or f"{tenant}-agent-{pysecrets.token_hex(3)}"
    client.secrets.kv.v2.create_or_update_secret(
        mount_point=mount,
        path=OpenBaoCredentialResolver.path_for(token),
        secret={"tenant_id": tenant, "principal_id": pid, "roles": [],
                "provisioned_by": actor,
                "provisioned_at":
                    datetime.now(timezone.utc).isoformat()})
    return token, pid


def _print_once_credential(kind: str, tenant: str, token: str,
                           principal_id: str) -> None:
    # Same ceremony as the vault unseal shares: the value goes to the
    # HUMAN's terminal exactly once, never to disk, logs, or the kit.
    print(f"\n  {kind} for tenant {tenant!r} (record NOW, shown once, "
          f"never stored):\n    {token}\n"
          f"    principal: {principal_id}\n")
    # B2: hold until the human confirms the value is saved (tty only).
    confirm_recorded("credential value")


def phase_tenants(ctx: ApplyContext) -> list[str]:
    if not ctx.plan.tenants:
        return ["no tenants in plan — bootstrap later with a re-plan"]
    if ctx.dry_run:
        return [f"[dry-run] would mint serving principals + the print-once "
                f"OPERATOR CONSOLE credential for: "
                f"{', '.join(ctx.plan.tenants)} (vault-markered, idempotent)"]
    import getpass

    import hvac

    addr = ctx.env.get("BAO_ADDR", "http://localhost:8200")
    mount = ctx.env.get("BAO_KV_MOUNT", "secret")
    client = hvac.Client(url=addr, token=ctx.env.get("BAO_ROOT_TOKEN", ""))
    actor = getpass.getuser()
    lines = []
    for tenant in ctx.plan.tenants:
        marker = f"kh/bootstrap/tenants/{tenant}"
        try:
            client.secrets.kv.v2.read_secret_version(
                path=marker, mount_point=mount)
            lines.append(f"tenant {tenant!r}: already bootstrapped (marker)")
        except Exception:
            # no marker -> bootstrap the agent serving principal now (the
            # same registry write `khctl provision-agent` re-mints day-2).
            token, agent_pid = provision_agent_credential(
                client, mount, tenant, actor,
                principal_id=f"{tenant}-default")
            client.secrets.kv.v2.create_or_update_secret(
                path=marker, mount_point=mount,
                secret={"principal_id": agent_pid})
            # The bearer credential exists exactly twice: hashed in the
            # vault registry, and on this terminal for the operator.
            _print_once_credential(
                "agent serving credential (re-mint: `khctl "
                "provision-agent`)", tenant, token, agent_pid)
            lines.append(f"tenant {tenant!r}: principal registered, "
                         f"marker set")

        # The FIRST OPERATOR CREDENTIAL (BP23): what the human logs into
        # the console with on-site. Own marker, own print-once; NEVER
        # silently re-minted.
        op_marker = f"kh/bootstrap/operators/{tenant}"
        try:
            client.secrets.kv.v2.read_secret_version(
                path=op_marker, mount_point=mount)
            lines.append(f"tenant {tenant!r}: operator console credential "
                         f"already provisioned — issue more with "
                         f"`khctl provision-operator`")
            continue
        except Exception:
            pass
        op_token, op_pid = provision_operator_credential(
            client, mount, tenant, "operator", actor)
        client.secrets.kv.v2.create_or_update_secret(
            path=op_marker, mount_point=mount,
            secret={"principal_id": op_pid, "provisioned_by": actor})
        _print_once_credential(
            "OPERATOR CONSOLE credential (log in at :8081/ui/)", tenant,
            op_token, op_pid)
        lines.append(f"tenant {tenant!r}: operator console credential "
                     f"minted (principal {op_pid}) — printed once above, "
                     f"provisioned by {actor}")
    return lines


PHASES: list[tuple[str, Callable[[ApplyContext], list[str]]]] = [
    ("kit verification", phase_kit),
    ("preflight", phase_preflight),
    ("env install", phase_env),
    ("services", phase_services),
    ("schema + migrations", phase_schema),
    ("openbao bootstrap", phase_openbao),
    ("model store", phase_models),
    ("python env", phase_python),
    ("tenant bootstrap", phase_tenants),
]


def _write_progress(ctx: ApplyContext, completed: bool,
                    failed_phase: Optional[str]) -> None:
    """F14: record how far a WET apply got, so the launcher can route a
    half-applied home back to repair instead of offering 'start the
    program'. Advisory — never fails an apply; dry runs write nothing."""
    if ctx.dry_run:
        return
    try:
        (ctx.infra_dir / APPLY_PROGRESS_FILE).write_text(json.dumps({
            "completed": completed,
            "failed_phase": failed_phase,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }), encoding="utf-8")
    except OSError:
        pass


# BP28 #15 — the most dangerous rehearsal finding: the old hvac catch-all
# reported EVERY vault error as a token mismatch and advised restoring
# BAO_ROOT_TOKEN from .env.bak. On a mere raft leader-election 500 that
# advice replaces a freshly-minted root token with the pilot placeholder —
# converting a wait-and-retry into an UNRECOVERABLE vault. Failure classes
# get their own truth; token guidance exists ONLY for a genuinely-rejected
# authenticated token, and even then verifies .env.bak before naming it.
def classify_vault_error(e: Exception) -> Optional[str]:
    """'connection' | 'sealed_or_down' | 'not_ready' | 'auth' |
    'vault_other' | None (not a vault/transport error — stays loud)."""
    root = type(e).__module__.split(".")[0]
    if isinstance(e, ConnectionError) or root in ("requests", "urllib3"):
        # requests.exceptions.ConnectionError is NOT an hvac type — it
        # sailed past the old handler into three bare tracebacks (#12).
        return "connection"
    if root != "hvac":
        return None
    import hvac.exceptions as he
    if isinstance(e, (he.Forbidden, he.Unauthorized)):
        return "auth"
    if isinstance(e, he.VaultDown):        # HTTP 503: sealed or standby
        return "sealed_or_down"
    if isinstance(e, he.InternalServerError):   # HTTP 500: no raft leader
        return "not_ready"
    return "vault_other"


def vault_failure_advice(e: Exception,
                         env_bak: Optional[Path] = None) -> Optional[str]:
    """Operator-facing guidance for a vault failure, honest per error class.
    INVARIANT (BP28 #15): no advice below may destroy a working root token —
    transient classes explicitly forbid touching it, and the auth class
    checks what .env.bak actually holds before pointing at it."""
    kind = classify_vault_error(e)
    if kind is None:
        return None
    name = type(e).__name__
    if kind == "connection":
        return (f"the vault is not answering ({name}) — it is down or "
                f"still starting. Check `docker compose ps` and "
                f"`docker logs kh-openbao --tail 50`, wait a moment, and "
                f"re-run. This is NOT a token problem — do not change "
                f"BAO_ROOT_TOKEN.")
    if kind == "sealed_or_down":
        return (f"the vault answered 503 ({name}) — it is SEALED (normal "
                f"after any restart, the custody model working) or still "
                f"starting. Unseal with {BAO_THRESHOLD} of the "
                f"{BAO_SHARES} custody shares (`{UNSEAL_COMMAND}`, run "
                f"{BAO_THRESHOLD}x), then re-run. This is NOT a token "
                f"problem — do not change BAO_ROOT_TOKEN.")
    if kind == "not_ready":
        return (f"the vault answered 500 ({name}) — raft is still electing "
                f"a leader (normal in the first seconds after start or "
                f"unseal). Wait a moment and re-run. This is NOT a token "
                f"problem — do not change BAO_ROOT_TOKEN.")
    if kind == "vault_other":
        return (f"the vault call failed ({name}: {e}) — inspect "
                f"`docker logs kh-openbao --tail 50` and re-run. Do NOT "
                f"change BAO_ROOT_TOKEN for this: only a permission-denied "
                f"answer means the token is wrong.")
    # kind == "auth": the vault is up, unsealed, and REJECTED the token —
    # the one class where token guidance belongs. Look at what .env.bak
    # actually holds before advising anything.
    bak_token = None
    if env_bak is not None and env_bak.exists():
        bak_token = parse_env_file(env_bak).get("BAO_ROOT_TOKEN")
    advice = (f"the vault REJECTED the configured token ({name}) — .env's "
              f"BAO_ROOT_TOKEN does not match the deployed vault. ")
    if bak_token and bak_token != PILOT_PLACEHOLDER_TOKEN:
        return advice + (
            "The pre-apply .env.bak holds a different, non-placeholder "
            "token; VERIFY it against custody records before restoring — "
            "never overwrite a token that still works elsewhere.")
    if bak_token == PILOT_PLACEHOLDER_TOKEN:
        return advice + (
            "Do NOT restore .env.bak — it holds only the pilot "
            "placeholder, which no deployed vault accepts; recover the "
            "real token from custody records (it printed once at init).")
    return advice + (
        "Recover the real token from custody records (it printed once at "
        "init); there is no usable .env.bak backup on this box.")


def run_apply(ctx: ApplyContext) -> int:
    mode = " (DRY RUN)" if ctx.dry_run else ""
    print(f"Knowledge Hub — apply{mode}: profile={ctx.plan.profile} "
          f"shape={ctx.plan.shape} custody={ctx.plan.secrets_custody}\n"
          + "-" * 44)
    # env must be parsed even when phase_env is dry — later phases read it.
    ctx.env = parse_env_file(ctx.env_file)
    for name, fn in PHASES:
        try:
            for line in fn(ctx):
                print(f"[ OK ] {name}: {line}")
        except ApplyError as e:
            _write_progress(ctx, False, name)
            print(f"[FAIL] {name}: {e}")
            print("-" * 44)
            print(f"[FAIL] apply stopped at {name!r} — fix and re-run "
                  f"(phases are idempotent)")
            return 1
        except Exception as e:
            _write_progress(ctx, False, name)
            # BP28 #15/#12: vault + transport failures answer with guidance
            # matched to the error CLASS — sealed/leader-election/connection
            # get wait-or-unseal, and only a genuinely-rejected token may
            # even mention token recovery. Everything else stays loud.
            advice = vault_failure_advice(e, ctx.infra_dir / ".env.bak")
            if advice:
                print(f"[FAIL] {name}: {advice}")
                print("-" * 44)
                print(f"[FAIL] apply stopped at {name!r} — fix and re-run "
                      f"(phases are idempotent)")
                return 1
            raise
    _write_progress(ctx, True, None)
    print("-" * 44)
    if ctx.dry_run:
        print("[ OK ] dry run complete — re-run without --dry-run to execute")
    else:
        print("[ OK ] apply complete — prove it: khctl verify --plan "
              "deploy_plan.json")
    return 0
