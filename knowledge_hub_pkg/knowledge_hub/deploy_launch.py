"""khctl launch — the SSD's front door: guided, stateful, gate-respecting
(§8.9, Build Prompt 18).

The launcher ORCHESTRATES the existing khctl subcommands — every deploy step
is a `deploy_cli.main([...])` call (verify-kit / probe / plan / apply /
verify / ingest), so the launcher's output IS the subcommands' output and
the two can never drift. It reimplements none of them.

Two places, one contract (written into DEPLOY_NOTES.md):

  KIT   (the SSD, read-only)   what make-kit built and signed. Verified on
                               arrival, never written to — the SSD stays
                               re-verifiable and reusable at the next site.
  WORK  (the target box)       the deployment home (~/knowledge-hub unless
                               KH_WORK_DIR/--work-dir says otherwise): the
                               engagement record (probe_report.json,
                               deploy_plan.json, .env.deploy), the installed
                               .env, and the bundle files apply reads
                               (compose, schema, migrations) — seeded from
                               the kit. Engagement artifacts live with the
                               deployment, never in the carry-kit.

Stateful: the launcher detects where this box is in the deploy lifecycle
and offers the right action —

  fresh / probed / planned  ->  the guided deploy: verify-kit -> probe
      (report shown) -> THE ADOPTION GATE -> plan -> THE PLAN PAUSE ->
      apply -> verify. Human-in-the-loop at every gate; never a black-box
      auto-deploy.
  deployed                  ->  start the Data Ingestion program: services
      up, serving started, ingestion sweep over registered sources, status
      + where-to-watch printed.

THE ADOPTION GATE (§8.9 Addendum 4 made operational): when the probe found
an existing Postgres / object store on a seam the profile would let us
adopt, the launcher says so LOUDLY and demands an explicit operator choice.
The default — a plain Enter — is ALWAYS the self-contained stack; client
infrastructure is never adopted silently (probe recommends, operator
confirms). Declining to choose stops the launcher, changes nothing.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from knowledge_hub.deploy_profiles import (
    DeployPlan,
    Profiles,
    qualify_candidate,
)

# Deployment lifecycle states (classify_state is pure and unit-tested).
STATE_FRESH = "fresh"          # nothing on this box yet
STATE_PROBED = "probed"        # probe report exists, no plan
STATE_PLANNED = "planned"      # plan + .env.deploy exist, stack not live
STATE_DEPLOYED = "deployed"    # plan + installed .env + live stack

DEFAULT_WORK_DIR = Path.home() / "knowledge-hub"
SERVING_HEALTH_TIMEOUT_S = 15


# ---------------------------------------------------------------------------
# State detection — pure classification over gathered signals
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class StateSignals:
    has_probe: bool
    has_plan: bool          # deploy_plan.json AND .env.deploy
    has_env: bool           # apply's phase_env installed .env
    stack_live: bool        # postgres from .env answers and carries schema
    # F14: the apply ledger says the last wet apply died mid-phase — the
    # stack can look "live" from phase 4 onward while models/credentials
    # never landed. Defaults False so a pre-ledger home stays classifiable.
    apply_incomplete: bool = False


def classify_state(signals: StateSignals) -> str:
    """The lifecycle ladder. 'deployed' requires all three durable artifacts
    AND a live stack AND no half-finished apply on record — a plan alone, a
    dead stack, or an apply that died at phase 6 resumes the guided flow
    instead of pretending the program can start (F14)."""
    if signals.has_plan and signals.has_env and signals.stack_live \
            and not signals.apply_incomplete:
        return STATE_DEPLOYED
    if signals.has_plan:
        return STATE_PLANNED
    if signals.has_probe:
        return STATE_PROBED
    return STATE_FRESH


def read_apply_progress(work_dir: Path) -> Optional[dict]:
    """The F14 phase ledger run_apply writes; None when absent/unreadable
    (a pre-ledger deploy keeps working — absence means 'assume complete')."""
    from knowledge_hub.deploy_apply import APPLY_PROGRESS_FILE
    path = work_dir / APPLY_PROGRESS_FILE
    if not path.exists():
        return None
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def stack_alive(env_file: Path) -> bool:
    """Live signal: the Postgres the installed .env points at answers within
    2s and has our schema_migrations ledger. Any failure = not live (the
    guided flow is idempotent, so a false negative only re-walks it)."""
    try:
        import psycopg

        from knowledge_hub.deploy_apply import dsn_from_env, parse_env_file
        dsn = dsn_from_env(parse_env_file(env_file))
        with psycopg.connect(dsn, connect_timeout=2) as conn:
            n = conn.execute(
                "SELECT count(*) FROM pg_tables WHERE schemaname='public'"
                " AND tablename='schema_migrations'").fetchone()[0]
        return bool(n)
    except Exception:
        return False


def gather_signals(work_dir: Path,
                   stack_check: Callable[[Path], bool] = stack_alive
                   ) -> StateSignals:
    env = work_dir / ".env"
    progress = read_apply_progress(work_dir)
    return StateSignals(
        has_probe=(work_dir / "probe_report.json").exists(),
        has_plan=((work_dir / "deploy_plan.json").exists()
                  and (work_dir / ".env.deploy").exists()),
        has_env=env.exists(),
        stack_live=env.exists() and stack_check(env),
        apply_incomplete=(progress is not None
                          and not progress.get("completed", False)))


# ---------------------------------------------------------------------------
# The adoption gate — detected client infrastructure is an operator
# conversation, not a footnote
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AdoptionCandidate:
    seam: str            # postgres | object_store
    display: str         # redacted DSN / endpoint (safe to print)
    qualified: bool      # True = `plan` would refuse-to-guess over it
    adoptable: bool      # preset allows ours|theirs -> operator must choose


def adoption_candidates(probe, profiles: Profiles,
                        profile_name: str) -> list[AdoptionCandidate]:
    """Every REACHABLE client-side Postgres / object store the probe saw.

    adoptable=True (preset says 'ours|theirs'): the operator MUST choose —
    qualified candidates are exactly what resolve_plan refuses to guess
    over, unqualified ones it would silently pass by; the launcher gates
    both. adoptable=False (seam pinned 'ours'): no choice exists, but 'we
    saw your Postgres and will not touch it' still gets said out loud —
    the §8.9 Addendum 4 notice is a conversation, not a footnote."""
    preset = profiles.presets.get(profile_name)
    if preset is None:
        return []  # plan itself refuses unknown profiles loudly
    pools = {"postgres": probe.postgres, "object_store": probe.object_store}
    found: list[AdoptionCandidate] = []
    for seam, candidates in pools.items():
        allowed = set(preset.seams.get(seam, "").split("|"))
        if "ours" not in allowed:
            continue  # theirs-only seams already demand an explicit --use
        adoptable = allowed == {"ours", "theirs"}
        rules = preset.qualify.get(seam, [])
        for candidate in candidates:
            if not candidate.reachable:
                continue
            qualified = all(r.passed for r in
                            qualify_candidate(seam, rules, candidate))
            found.append(AdoptionCandidate(
                seam=seam,
                display=(getattr(candidate, "dsn_redacted", None)
                         or candidate.endpoint),
                qualified=qualified, adoptable=adoptable))
    return found


def run_adoption_gate(candidates: list[AdoptionCandidate],
                      ask: Callable[[str], str],
                      say: Callable[[str], None]) -> Optional[list[str]]:
    """The gate itself: one explicit choice per detected seam. Returns the
    `--use` flags for plan, or None when the operator stops the launcher.
    Plain Enter = the self-contained stack, ALWAYS."""
    flags: list[str] = []
    for seam in ("postgres", "object_store"):
        seam_candidates = [c for c in candidates if c.seam == seam]
        if not seam_candidates:
            continue
        label = seam.upper().replace("_", " ")
        if not any(c.adoptable for c in seam_candidates):
            # Seam is pinned 'ours' by the profile: nothing to decide, but
            # the detection is still said out loud — non-disruption is a
            # promise we make explicitly, not silently.
            say("")
            say(f"NOTE: existing {label} detected "
                f"({', '.join(c.display for c in seam_candidates)}).")
            say(f"      This profile deploys its own {seam}; the detected "
                f"service will NOT be touched.")
            continue
        say("")
        say("=" * 70)
        say(f"!!  EXISTING {label} DETECTED — OPERATOR DECISION REQUIRED  !!")
        for c in seam_candidates:
            tag = ("[QUALIFIED — plan would offer adoption]" if c.qualified
                   else "[not qualified — informational]")
            say(f"      {c.display}   {tag}")
        say("")
        say("  Adopting client infrastructure is a commitment, not a default")
        say("  (probe recommends, operator confirms — §8.9 Addendum 4). The")
        say("  detected service will NOT be touched unless you adopt it here.")
        say("")
        say("    [Enter]  deploy the self-contained stack (ours) — DEFAULT")
        say("    a        adopt the detected service (you supply the endpoint)")
        say("    q        stop the launcher (nothing has been changed)")
        say("=" * 70)
        while True:
            answer = ask(f"{seam} — your call [Enter=ours / a / q]: ").strip().lower()
            if answer in ("", "ours"):
                flags += ["--use", f"{seam}=ours"]
                say(f"   -> self-contained {seam} (recorded as an explicit "
                    f"operator choice)")
                break
            if answer == "a":
                endpoint = ask(f"{seam} — full endpoint (DSN/URL, incl. "
                               f"credentials): ").strip()
                if not endpoint:
                    say("   empty endpoint — try again (or q)")
                    continue
                # BP34: adoption is the ONE branch that writes into client
                # infrastructure — the stakes are typed back before they
                # are accepted, so a mis-key can never adopt.
                say("")
                say(f"  ADOPTING MEANS WRITES: apply will install the "
                    f"Knowledge Hub schema, replay every")
                say(f"  migration, and CREATE EXTENSIONs INSIDE that "
                    f"{seam.replace('_', ' ')}. This cannot be")
                say(f"  a default or an accident — it is the commitment "
                    f"(§8.9 Addendum 4).")
                confirm = ask(f"  type ADOPT to confirm (anything else = "
                              f"back to the choice): ").strip()
                if confirm != "ADOPT":
                    say("   not confirmed — nothing adopted, back to the "
                        "choice")
                    continue
                flags += ["--use", f"{seam}=theirs:{endpoint}"]
                say(f"   -> adopting client {seam} (recorded as an operator "
                    f"override; verify must prove it live)")
                break
            if answer == "q":
                return None
            say("   unrecognized — press Enter, or type 'a' or 'q'")
    return flags


OURS_PG_DEFAULT_PORT = 5432


def _host_port_free(port: int) -> bool:
    """True when nothing on this box answers 127.0.0.1:<port> (same
    reachability idiom as deploy_probe.probe_host, inverted)."""
    import socket
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return False
    except OSError:
        return True


def choose_ours_postgres_port(deployed_port: Optional[int],
                              default_port_busy: bool,
                              ask: Callable[[str], str],
                              say: Callable[[str], None],
                              port_free: Callable[[int], bool]
                              = _host_port_free) -> Optional[int]:
    """BP34 — the port half of the non-disruption guarantee. Decides the
    HOST port OUR Postgres binds, independent of the adoption gate: a
    co-resident client Postgres holds 5432 whether or not the probe could
    log into it (no credentials = no gate, but the port is just as taken).

    deployed_port set  -> a LIVE deployment already answers on that port;
                          keep it (repair must never migrate a live stack).
    5432 free          -> the default stands, nothing to decide.
    5432 busy          -> the operator picks a free port, recorded in the
                          plan as --use postgres=ours:<port>. The holder of
                          5432 is never contested or touched.
    Returns the port to record, or None when the default (5432) is right.
    """
    if deployed_port is not None:
        return deployed_port if deployed_port != OURS_PG_DEFAULT_PORT \
            else None
    if not default_port_busy:
        return None
    suggested = next((p for p in range(5433, 5533) if port_free(p)), None)
    say("")
    say(f"NOTE: host port {OURS_PG_DEFAULT_PORT} is already in use on this "
        f"box (an existing service —")
    say(f"      typically the client's own Postgres). The self-contained "
        f"stack binds its")
    say(f"      OWN loopback port instead; the existing service is never "
        f"contested or touched.")
    while True:
        answer = ask(f"host port for OUR Postgres "
                     f"[Enter={suggested}]: ").strip()
        if not answer:
            if suggested is None:
                say("   no free port found in 5433-5532 — type one")
                continue
            return suggested
        if answer.isdigit() and 1024 <= int(answer) <= 65535:
            port = int(answer)
            if not port_free(port):
                say(f"   port {port} is busy on this box too — pick a "
                    f"free one")
                continue
            return port
        say("   not a port — enter a number 1024-65535, or press Enter")


def run_plan_pause(ask: Callable[[str], str], say: Callable[[str], None],
                   forced_dry: bool) -> Optional[bool]:
    """THE PLAN PAUSE — the load-bearing stop between 'a plan exists' and
    'the box changes'. Returns the apply dry_run flag, or None to stop.
    In a --dry-run launch session even 'deploy' rehearses."""
    say("")
    say("-" * 70)
    say("PLAN GATE — nothing has been changed yet. The plan above is the")
    say("contract; review it before anything executes.")
    say("  deploy    execute the plan now (khctl apply)"
        + ("   [--dry-run session: this rehearses]" if forced_dry else ""))
    say("  rehearse  walk every phase, change nothing (apply --dry-run)")
    say("  q         stop here (the plan + .env.deploy stay for review)")
    say("-" * 70)
    while True:
        answer = ask("plan gate> ").strip().lower()
        if answer == "deploy":
            return forced_dry
        if answer == "rehearse":
            return True
        if answer == "q":
            return None
        say("   type 'deploy', 'rehearse', or 'q'")


# ---------------------------------------------------------------------------
# Work-dir seeding — the kit is the source, the work dir is the deployment
# ---------------------------------------------------------------------------
def seed_work_dir(kit_dir: Path, work_dir: Path) -> list[str]:
    """Copy the bundle files apply/plan read (compose, schema, migrations,
    profiles) plus the kit's runtime dirs (tokenizer) from the kit into
    the deployment home. The lists are deploy_kit's own — producer/consumer
    symmetry, never a parallel set. A file already PRESENT in the home is
    left in place (BP28 #11: a launch.sh re-run must never revert a field
    repair — delete the deployed copy to re-seed it); new filenames (e.g.
    a new migration) still land. Engagement artifacts (.env*, plan, probe
    report) are never touched."""
    import shutil

    from knowledge_hub.deploy_kit import (BUNDLE_DIRS, BUNDLE_FILES,
                                          KIT_RUNTIME_DIRS)
    work_dir.mkdir(parents=True, exist_ok=True)
    copied = kept = 0

    def _seed(src: Path, dst: Path) -> None:
        nonlocal copied, kept
        if dst.exists():
            if dst.read_bytes() != src.read_bytes():
                kept += 1
            return
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied += 1

    for rel in BUNDLE_FILES:
        src = kit_dir / rel
        if not src.exists():
            continue  # verify-kit already judged the kit; skip = dev bundle
        _seed(src, work_dir / rel)
    for rel in BUNDLE_DIRS + KIT_RUNTIME_DIRS:
        src = kit_dir / rel
        if not src.exists():
            continue
        for p in sorted(src.rglob("*")):
            if p.is_file():
                _seed(p, work_dir / rel / p.relative_to(src))
    lines = [f"{copied} bundle item(s) seeded from the kit "
             f"(engagement artifacts never overwritten)"]
    if kept:
        lines.append(f"{kept} deployed file(s) left in place (differ from "
                     f"the kit — a repair is never clobbered; delete a "
                     f"file to re-seed it)")
    return lines


# ---------------------------------------------------------------------------
# The launcher
# ---------------------------------------------------------------------------
def _default_runner(argv: list[str]) -> int:
    from knowledge_hub import deploy_cli
    return deploy_cli.main(argv)


@dataclass
class LaunchConfig:
    kit_dir: Path
    work_dir: Path = field(default_factory=lambda: DEFAULT_WORK_DIR)
    profile: str = "appliance"
    tenants: Optional[str] = None        # comma-separated; prompted if None
    custody: Optional[str] = None
    allow_gated_tier: bool = False
    # BP46 Fix 2: the operator has confirmed the box has no GPU, so `plan`
    # may treat a GPU-less probe as fact instead of stopping on a possible
    # detection miss. Never defaulted on.
    confirm_no_gpu: bool = False
    dry_run: bool = False                # rehearsal session: apply never mutates
    # Seams for tests: the subcommand runner and both sides of the console.
    runner: Callable[[list[str]], int] = _default_runner
    input_fn: Callable[[str], str] = input
    print_fn: Callable[[str], None] = print
    stack_check: Callable[[Path], bool] = stack_alive


def _step(n: int, total: int, title: str) -> str:
    return f"\n== step {n}/{total} — {title} " + "=" * max(0, 44 - len(title))


def run_launch(cfg: LaunchConfig) -> int:
    say, ask = cfg.print_fn, cfg.input_fn
    kit = cfg.kit_dir.resolve()
    work = cfg.work_dir.resolve()
    work.mkdir(parents=True, exist_ok=True)
    # The work dir is the deployment: .env, engagement record, logs. Settings
    # (pydantic env_file='.env') resolve relative to CWD — but the singleton
    # bound at process start, in whatever directory khctl was invoked from
    # (deploy_cli's module imports pull knowledge_hub.config). Pin CWD and
    # refresh the singleton so a deployed home's .env governs this session
    # (BP33: verify and ingest ran on pilot defaults without this).
    os.chdir(work)
    from knowledge_hub.config import reload_settings
    reload_settings()

    signals = gather_signals(work, cfg.stack_check)
    state = classify_state(signals)
    say("Knowledge Hub — launcher"
        + (" (DRY-RUN SESSION: nothing will be changed)" if cfg.dry_run else ""))
    say(f"  kit:   {kit}")
    say(f"  home:  {work}")
    say(f"  state: {state}")

    if state == STATE_DEPLOYED:
        return _deployed_menu(cfg, kit, work)
    if signals.apply_incomplete:
        progress = read_apply_progress(work) or {}
        say(f"  NOTE: the last apply did NOT finish — it stopped at phase "
            f"{progress.get('failed_phase')!r}. This box is NOT fully "
            f"deployed (models/credentials may be missing); the guided "
            f"flow below resumes it — phases are idempotent.")
    if state != STATE_FRESH:
        say(f"  (resuming: earlier artifacts found for state {state!r} — the "
            f"guided flow is idempotent and will reuse/refresh them)")
    return guided_deploy(cfg, kit, work)


# ------------------------------------------------------------ guided deploy --
def guided_deploy(cfg: LaunchConfig, kit: Path, work: Path) -> int:
    say, ask, run = cfg.print_fn, cfg.input_fn, cfg.runner
    total = 6

    say(_step(1, total, "kit arrival gate (khctl verify-kit)"))
    if run(["verify-kit", "--kit", str(kit)]) != 0:
        say("launch stopped: the kit failed its arrival gate — do not "
            "deploy from it.")
        return 1

    say(_step(2, total, "seed the deployment home from the kit"))
    for line in seed_work_dir(kit, work):
        say(f"   {line}")

    say(_step(3, total, "environment probe (read-only)"))
    profiles_path = work / "profiles.toml"
    if run(["--profiles", str(profiles_path), "probe",
            "--out", str(work / "probe_report.json")]) != 0:
        return 1
    answer = ask("\nprobe report above — [Enter] continues to the plan "
                 "gate, q stops: ").strip().lower()
    if answer == "q":
        say("stopped after the probe — nothing changed.")
        return 0

    say(_step(4, total, "the adoption gate + plan"))
    from knowledge_hub.deploy_probe import ProbeReport
    from knowledge_hub.deploy_profiles import load_profiles
    probe = ProbeReport.from_json(
        (work / "probe_report.json").read_text(encoding="utf-8"))
    profiles = load_profiles(profiles_path)
    candidates = adoption_candidates(probe, profiles, cfg.profile)
    if candidates:
        use_flags = run_adoption_gate(candidates, ask, say)
        if use_flags is None:
            say("stopped at the adoption gate — nothing changed.")
            return 0
    else:
        use_flags = []
        say("   no adoptable client infrastructure detected — "
            "self-contained stack.")

    # BP34 — the port step. Runs whether or not the gate fired: a client
    # Postgres the probe could NOT log into raises no gate, but its grip on
    # 5432 is just as real. A live deployed home keeps its port (repair
    # continuity, judged by the stack actually answering — a half-applied
    # .env pointing at a foreign 5432 must NOT read as "ours").
    if not any(f.startswith("postgres=theirs:") for f in use_flags):
        deployed_port: Optional[int] = None
        env_path = work / ".env"
        if env_path.exists() and cfg.stack_check(env_path):
            from knowledge_hub.deploy_apply import parse_env_file
            raw = parse_env_file(env_path).get(
                "POSTGRES_PORT", str(OURS_PG_DEFAULT_PORT))
            deployed_port = (int(raw) if raw.isdigit()
                             else OURS_PG_DEFAULT_PORT)
        port = choose_ours_postgres_port(
            deployed_port,
            bool(probe.host.ports_listening.get(OURS_PG_DEFAULT_PORT)),
            ask, say)
        if port is not None:
            use_flags += ["--use", f"postgres=ours:{port}"]
            say(f"   -> our Postgres will bind 127.0.0.1:{port} (recorded "
                f"in the plan; whatever holds "
                f"{OURS_PG_DEFAULT_PORT} stays untouched)")

    tenants = cfg.tenants
    if tenants is None:
        # L2: a deploy with ZERO tenants mints no credentials — the console
        # can never be logged into. Plain Enter must not slide into that;
        # deploying tenant-less takes an explicit 'none'.
        while True:
            tenants = ask("tenant id(s) to bootstrap, comma-separated "
                          "(or 'none'): ").strip()
            if tenants.lower() == "none":
                tenants = ""
                break
            if tenants:
                break
            say("   Empty input — a deploy with NO tenants mints NO "
                "credentials, so the operator console cannot be logged "
                "into afterwards.")
            say("   Enter at least one tenant id (e.g. the company name), "
                "or type 'none' to deploy without tenants anyway.")
            say("   (Recovery if you do: re-run this launcher, choose 'd' "
                "repair, and enter the tenant at this prompt — phases are "
                "idempotent and the deployed vault token is preserved.)")
    plan_argv = ["--profiles", str(profiles_path), "plan",
                 "--profile", cfg.profile,
                 "--probe", str(work / "probe_report.json"),
                 "--out-dir", str(work), *use_flags]
    if tenants:
        plan_argv += ["--tenants", tenants]
    if cfg.custody:
        plan_argv += ["--custody", cfg.custody]
    if cfg.allow_gated_tier:
        plan_argv += ["--allow-gated-tier"]
    if cfg.confirm_no_gpu:
        plan_argv += ["--confirm-no-gpu"]
    if run(plan_argv) != 0:
        say("launch stopped: plan refused — the message above names the "
            "flag or fork that resolves it.")
        return 1

    say(_step(5, total, "the plan pause -> apply"))
    while True:
        dry = run_plan_pause(ask, say, cfg.dry_run)
        if dry is None:
            say("stopped at the plan gate — nothing changed. Re-run the "
                "launcher to resume from here.")
            return 0
        apply_argv = ["apply", "--plan", str(work / "deploy_plan.json"),
                      "--env-file", str(work / ".env.deploy"),
                      "--infra-dir", str(work), "--kit", str(kit)]
        if dry:
            apply_argv.append("--dry-run")
        rc = run(apply_argv)
        if rc != 0:
            return rc
        if not dry:
            # Apply just WROTE the deployment's .env (minted S3 credentials,
            # the real vault token). The settings singleton bound before any
            # of that existed — refresh it or step 6's verify (and anything
            # after) judges the deploy against pilot defaults (BP33).
            from knowledge_hub.config import reload_settings
            reload_settings()
            break
        if cfg.dry_run:
            say("\nrehearsal complete — this was a --dry-run session, "
                "stopping here. Re-run without --dry-run to deploy.")
            return 0
        say("\nrehearsal complete — back to the plan gate.")

    say(_step(6, total, "prove the plan's claims live (khctl verify)"))
    rc = run(["verify", "--plan", str(work / "deploy_plan.json")])
    if rc == 0:
        say("\ndeploy complete + verified. Run the launcher again to start "
            "the Data Ingestion program.")
    return rc


# ------------------------------------------------------- deployed -> start --
def _deployed_menu(cfg: LaunchConfig, kit: Path, work: Path) -> int:
    say, ask = cfg.print_fn, cfg.input_fn
    say("")
    say("A deployment is live on this box. What now?")
    say("  [Enter]  start the Data Ingestion program — DEFAULT")
    say("  v        verify the deployment (khctl verify)")
    say("  d        re-run the guided deploy (repair; phases are idempotent)")
    say("  q        quit")
    while True:
        answer = ask("launch> ").strip().lower()
        if answer in ("", "s", "start"):
            return start_program(cfg, kit, work)
        if answer == "v":
            return cfg.runner(["verify", "--plan",
                               str(work / "deploy_plan.json")])
        if answer == "d":
            return guided_deploy(cfg, kit, work)
        if answer == "q":
            return 0
        say("   press Enter, or type 'v', 'd', or 'q'")


def khctl_hint() -> str:
    """The exact runnable path to khctl (L1): printed hints must work as
    typed, and khctl is not on PATH by default. The launcher runs from the
    deployment venv, so its own interpreter locates the console script;
    fall back to the bare name (the SSD bootstrap also symlinks
    ~/.local/bin/khctl)."""
    exe = Path(sys.executable).with_name(
        "khctl.exe" if os.name == "nt" else "khctl")
    return str(exe) if exe.exists() else "khctl"


def serving_healthy(host: str, port: str | int) -> bool:
    try:
        with urllib.request.urlopen(
                f"http://{host}:{port}/v1/health", timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


def ensure_serving(work: Path, env: dict[str, str],
                   say: Callable[[str], None]) -> bool:
    """Start the S5 serving service if it isn't already answering. The
    service is `python -m knowledge_hub.service_http` exactly as SERVICE_
    NOTES documents; the launcher only supervises the start."""
    host = env.get("SERVING_HOST", "127.0.0.1")
    port = env.get("SERVING_PORT", "8080")
    if serving_healthy(host, port):
        say(f"   serving already answering at http://{host}:{port}")
        return True
    log_path = work / "serving.log"
    with log_path.open("ab") as fh:
        kwargs: dict = dict(cwd=str(work), stdout=fh,
                            stderr=subprocess.STDOUT)
        if os.name == "posix":
            kwargs["start_new_session"] = True
        else:  # detached on Windows (the dev bench)
            kwargs["creationflags"] = 0x00000208
        proc = subprocess.Popen(
            [sys.executable, "-m", "knowledge_hub.service_http"], **kwargs)
    deadline = time.monotonic() + SERVING_HEALTH_TIMEOUT_S
    while time.monotonic() < deadline:
        if serving_healthy(host, port):
            say(f"   serving started (pid {proc.pid}) at "
                f"http://{host}:{port} — log: {log_path}")
            return True
        if proc.poll() is not None:
            break
        time.sleep(0.5)
    say(f"   [WARN] serving did not come healthy in "
        f"{SERVING_HEALTH_TIMEOUT_S}s — see {log_path}")
    return False


def operator_healthy(host: str, port: str | int) -> bool:
    try:
        with urllib.request.urlopen(
                f"http://{host}:{port}/v1/health", timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


def ensure_operator(work: Path, env: dict[str, str],
                    say: Callable[[str], None]) -> bool:
    """Start the operator write API + console UI (BP19/BP20) if it isn't
    already answering — `python -m knowledge_hub.operator_http`, exactly as
    OPERATOR_API_NOTES documents. On-site the operator WATCHES ingestion and
    RESOLVES reviews through this console, so the deployed-state launch is
    not complete until /ui/ is reachable."""
    host = env.get("OPERATOR_HOST", "127.0.0.1")
    port = env.get("OPERATOR_PORT", "8081")
    if operator_healthy(host, port):
        say(f"   operator console already answering at http://{host}:{port}")
        return True
    log_path = work / "operator.log"
    with log_path.open("ab") as fh:
        kwargs: dict = dict(cwd=str(work), stdout=fh,
                            stderr=subprocess.STDOUT)
        if os.name == "posix":
            kwargs["start_new_session"] = True
        else:  # detached on Windows (the dev bench)
            kwargs["creationflags"] = 0x00000208
        proc = subprocess.Popen(
            [sys.executable, "-m", "knowledge_hub.operator_http"], **kwargs)
    deadline = time.monotonic() + SERVING_HEALTH_TIMEOUT_S
    while time.monotonic() < deadline:
        if operator_healthy(host, port):
            say(f"   operator console started (pid {proc.pid}) at "
                f"http://{host}:{port}/ui/ — log: {log_path}")
            return True
        if proc.poll() is not None:
            break
        time.sleep(0.5)
    say(f"   [WARN] operator console did not come healthy in "
        f"{SERVING_HEALTH_TIMEOUT_S}s — see {log_path} (is migration 010 "
        f"applied?)")
    return False


def start_program(cfg: LaunchConfig, kit: Path, work: Path) -> int:
    """The deployed-state action: bring the stack up, start serving AND the
    operator console, run one ingestion sweep, and say where to watch.
    Every moving part is an existing component — the launcher only
    sequences them."""
    say, run = cfg.print_fn, cfg.runner
    from knowledge_hub.deploy_apply import (
        ApplyContext,
        _compose,
        parse_env_file,
    )
    plan = DeployPlan.from_json(
        (work / "deploy_plan.json").read_text(encoding="utf-8"))
    env = parse_env_file(work / ".env")
    say("\nstarting the Data Ingestion program "
        f"(profile={plan.profile}, shape={plan.shape})")

    services = plan.compose_services()
    ctx = ApplyContext(plan=plan, infra_dir=work, kit_dir=kit,
                       env_file=work / ".env")
    if services:
        out = _compose(ctx, "up", "-d", *services)
        if out.returncode != 0:
            say(f"[FAIL] compose up: {out.stderr.strip()[-500:]}")
            return 1
        say(f"   services up: {', '.join(services)}")

    if not cfg.stack_check(work / ".env"):
        say("[FAIL] postgres did not answer after compose up — check "
            "`docker compose ps` in the deployment home")
        return 1
    say("   postgres answering")

    if "secrets" in plan.seams:
        addr = env.get("BAO_ADDR", "http://localhost:8200")
        # compose up may have RECREATED the vault container (observed once
        # per fresh deploy when .env gained the real root token after
        # bootstrap): give it the same readiness window the apply phases
        # get, or a normally-starting vault reads as a connection reset
        # (BP33 — the honest SEALED message below never got its turn).
        from knowledge_hub.deploy_apply import (ApplyError,
                                                _await_vault_ready)
        from knowledge_hub.deploy_probe import probe_secrets
        try:
            _await_vault_ready(ctx, addr)
        except ApplyError as e:
            say(f"[FAIL] {e}")
            return 1
        sealed = bool(probe_secrets(addr).sealed)
        if sealed:
            from knowledge_hub.deploy_apply import UNSEAL_COMMAND
            say(f"[FAIL] vault at {addr} is SEALED — normal after any "
                f"reboot, NOT a lost credential. Unseal with 3 of the 5 "
                f"custody shares (`{UNSEAL_COMMAND}`, run 3x), then re-run "
                f"the launcher. This is the custody model working, not an "
                f"error to bypass.")
            return 1
        say("   vault unsealed")

    inference = plan.seams.get("inference")
    # BP46 Fix 5: local-external gets the SAME liveness gate (the endpoint is
    # on this box and the program is dead without it) but never the same
    # diagnosis — we installed nothing there, so "the deploy did not finish
    # installing models" would be a false accusation.
    ours = inference is not None and inference.choice == "local"
    if inference and inference.choice in ("local", "local-external"):
        from knowledge_hub.deploy_probe import probe_ollama
        host = env.get("OLLAMA_HOST", "http://localhost:11434")
        report = probe_ollama(host)
        if not report.reachable:
            say(f"[FAIL] ollama unreachable at {host}: {report.error} — "
                + ("start it (systemd: `systemctl start ollama`), then re-run"
                   if ours else
                   "this is the OPERATOR-SUPPLIED local endpoint, not one we "
                   "installed — start their runtime, then re-run"))
            return 1
        # F14 live check: reachability is not enough — a deploy that died
        # before phase 'model store' leaves ollama up with NO models, and
        # the program cannot work. Same matching rule as phase_models.
        required = [m for m in {env.get("EMBEDDING_MODEL", "bge-m3"),
                                plan.extraction_model
                                or env.get("EXTRACTION_MODEL", "qwen3.6")}
                    if m]
        missing = [m for m in required
                   if not any(t == m or t.startswith(f"{m}:")
                              for t in report.models)]
        if missing:
            say(f"[FAIL] required model(s) not served at {host}: "
                f"{', '.join(missing)} — "
                + ("the deploy did not finish installing models. Re-run the "
                   "launcher and choose 'd' (repair); the 'model store' "
                   "phase is idempotent."
                   if ours else
                   "we did not install this endpoint's models. The operator "
                   "pulls them there, or re-pin EXTRACTION_MODEL / "
                   "EMBEDDING_MODEL to what it already serves."))
            return 1
        say(f"   ollama serving the required models "
            f"({', '.join(sorted(required))})"
            + ("" if ours else " (operator-supplied local endpoint; "
                               "on-premises, model not installed by us)"))

    serving_ok = ensure_serving(work, env, say)
    operator_ok = ensure_operator(work, env, say)
    if not (serving_ok and operator_ok):
        # F6: the supervisor failed — claiming success and pointing the
        # operator at a dead console is worse than stopping here.
        failed = [label for label, ok in
                  (("serving", serving_ok),
                   ("operator console", operator_ok)) if not ok]
        say("")
        say(f"[FAIL] the Data Ingestion program did NOT fully start — "
            f"{' and '.join(failed)} never came healthy.")
        say(f"       Diagnose: {work / 'serving.log'} and "
            f"{work / 'operator.log'},")
        say(f"       then `docker compose ps` in {work} and "
            f"`{khctl_hint()} verify --plan "
            f"{work / 'deploy_plan.json'}`.")
        return 1

    tenants = plan.tenants or [
        t.strip() for t in env.get("SERVING_TENANTS", "").split(",")
        if t.strip()]
    if tenants:
        say("")
        argv = ["ingest"]
        for tenant in tenants:
            argv += ["--tenant", tenant]
        run(argv)
    else:
        say(f"   no tenants in the plan — ingest manually: "
            f"{khctl_hint()} ingest --tenant <t> --add-source <ref>=<folder>")

    host = env.get("SERVING_HOST", "127.0.0.1")
    port = env.get("SERVING_PORT", "8080")
    op_host = env.get("OPERATOR_HOST", "127.0.0.1")
    op_port = env.get("OPERATOR_PORT", "8081")
    say("")
    say("Data Ingestion program is up. Where to watch:")
    say(f"  OPERATOR CONSOLE  http://{op_host}:{op_port}/ui/   (watch "
        f"ingestion, resolve reviews — log in with an operator credential)")
    say(f"  serving      http://{host}:{port}/v1/health   and   "
        f"/v1/metrics (p95 vs budget)")
    say(f"  serving log  {work / 'serving.log'}")
    say(f"  operator log {work / 'operator.log'}")
    say(f"  usage record {work / env.get('SERVING_USAGE_LOG', 'serving_usage.jsonl')}")
    say(f"  ingest more  {khctl_hint()} ingest --tenant <t>   "
        f"(--watch for continuous)")
    say(f"  failures     {khctl_hint()} alerts   (list · --retry · --ack)")
    say(f"  prove it     {khctl_hint()} verify --plan "
        f"{work / 'deploy_plan.json'}")
    return 0


# ---------------------------------------------------------------------------
# khctl ingest — the Data Ingestion program (capture -> process -> extract
# -> resolve over REGISTERED sources; wiring identical to the pilot's
# build_corpus.py, parameterized by the registry instead of a corpus tree)
# ---------------------------------------------------------------------------
def adapter_for(entry):
    """Adapter from a registry row, or (None, why-not). Only sources whose
    config carries what their adapter needs are runnable from the registry
    alone; the MS Graph family runs via its own credentialed runbook
    (CONNECTOR_NOTES) until a registry-driven factory is warranted."""
    if entry.source_system == "filesystem":
        root = entry.config.get("root")
        if not root:
            return None, ("filesystem source has no config.root — "
                          "re-register: khctl ingest --add-source "
                          f"{entry.source_ref}=<folder>")
        if not Path(root).is_dir():
            return None, f"config.root {root!r} is not a directory on this box"
        from knowledge_hub.sources_fs import FilesystemSourceAdapter
        return (FilesystemSourceAdapter(source_ref=entry.source_ref,
                                        root=root), None)
    return None, (f"source_system {entry.source_system!r} has no "
                  f"registry-driven adapter yet (runs via its own runbook)")


# Read-only status snapshot per tenant — every column verified against the
# baseline schema + migrations 002/004.
STATUS_QUERIES: list[tuple[str, str]] = [
    ("raw documents landed",
     "SELECT count(*) FROM raw_documents WHERE tenant_id=%s"),
    ("documents",
     "SELECT count(*) FROM documents WHERE tenant_id=%s"),
    ("embedded child chunks",
     "SELECT count(*) FROM chunks WHERE tenant_id=%s AND level='child'"
     " AND embedding IS NOT NULL"),
    ("facts (current)",
     "SELECT count(*) FROM facts WHERE tenant_id=%s AND valid_to IS NULL"),
    ("mentions awaiting resolution",
     "SELECT count(*) FROM entity_mentions WHERE tenant_id=%s"
     " AND resolution_status='pending'"),
    ("processing queue",
     "SELECT count(*) FROM dispatch_queue WHERE tenant_id=%s"
     " AND status='queued'"),
    ("extraction queue",
     "SELECT count(*) FROM extraction_queue WHERE tenant_id=%s"
     " AND status='queued'"),
]


def print_status(store, tenant: str,
                 say: Callable[[str], None] = print) -> None:
    with store.transaction(tenant) as conn:
        for label, query in STATUS_QUERIES:
            n = conn.execute(query, (tenant,)).fetchone()["count"]
            say(f"   {label:28s} {n}")


def _infra_failure(e: Exception) -> Optional[str]:
    """Classify an exception as a known infrastructure failure (F17) so
    `khctl ingest` answers in the launcher's [FAIL] language instead of a
    raw traceback. None = not infra; let it propagate (a real bug should
    stay loud)."""
    root = type(e).__module__.split(".")[0]
    if root.startswith("psycopg"):
        return (f"postgres went away ({type(e).__name__}) — start the "
                f"stack (`docker compose up -d` in the deployment home)")
    if root == "hvac":
        return (f"the vault refused or was unreachable "
                f"({type(e).__name__}) — is it up and unsealed?")
    if root in ("httpx", "httpcore", "requests", "urllib3", "botocore") \
            or isinstance(e, ConnectionError):
        return (f"a backing service dropped mid-sweep "
                f"({type(e).__name__}: {e}) — check ollama and the "
                f"docker services")
    return None


def ingest_preflight(say: Callable[[str], None] = print) -> bool:
    """F17: the same liveness checks start_program runs, as a pre-flight —
    a day-2 `khctl ingest` on a half-up stack gets an actionable [FAIL]
    up front, not a psycopg traceback mid-sweep."""
    from knowledge_hub.config import settings
    import psycopg
    try:
        psycopg.connect(settings.postgres_dsn, connect_timeout=2).close()
    except Exception as e:
        say(f"[FAIL] postgres is not answering at "
            f"{settings.postgres_host}:{settings.postgres_port} "
            f"({type(e).__name__}) — start the stack "
            f"(`docker compose up -d` in the deployment home), then re-run")
        return False
    from knowledge_hub.deploy_probe import probe_ollama
    report = probe_ollama(settings.ollama_host)
    if not report.reachable:
        say(f"[FAIL] ollama unreachable at {settings.ollama_host}: "
            f"{report.error} — start it (`systemctl start ollama`), "
            f"then re-run")
        return False
    return True


def run_ingest(tenants: list[str], add_sources: list[str],
               limit: int = 1000, watch: bool = False,
               interval: float = 30.0,
               say: Callable[[str], None] = print) -> int:
    """One sweep (or a --watch loop) of the full ingestion pipeline for the
    given tenants. Every stage is the existing service; a failure in one
    source degrades that source only (capture's containment), and the
    queue-driven stages skip poison messages rather than wedging."""
    if not tenants:
        say("no tenant given — khctl ingest --tenant <t> "
            "[--add-source <ref>=<folder>]")
        return 2
    if not ingest_preflight(say):
        return 1

    # Heavy imports deferred so `khctl ingest --help` stays instant.
    from knowledge_hub.capture import CaptureService
    from knowledge_hub.chunking import SectionChunker
    from knowledge_hub.dispatch_pg import PostgresDispatcher
    from knowledge_hub.embedding_ollama import OllamaEmbedder
    from knowledge_hub.extraction import ExtractionService
    from knowledge_hub.extraction_llm import LLMJointExtractionStrategy
    from knowledge_hub.extraction_structured import StructuredMapStrategy
    from knowledge_hub.factstore_pg import PostgresFactStore
    from knowledge_hub.grounding import SpanGrounder
    from knowledge_hub.ontology import PostgresOntologyBinding
    from knowledge_hub.parsing_docling import DoclingParser
    from knowledge_hub.pipeline import Pipeline
    from knowledge_hub.rawstore_s3 import S3RawStore
    from knowledge_hub.resolution import ResolutionService
    from knowledge_hub.scoring_tiered import TieredScorer
    from knowledge_hub.secrets_openbao import OpenBaoSecretsProvider

    store = PostgresFactStore()
    pipeline = Pipeline(store=store)
    raw_store = S3RawStore(store=store)
    dispatcher = PostgresDispatcher(store)
    ext_dispatcher = PostgresDispatcher(store, table="extraction_queue")
    embedder = OllamaEmbedder()
    capture = CaptureService(pipeline, raw_store, dispatcher,
                             secrets=OpenBaoSecretsProvider())
    from knowledge_hub.processing import ProcessingService
    processing = ProcessingService(pipeline, raw_store, DoclingParser(),
                                   SectionChunker(), embedder,
                                   dispatcher=dispatcher,
                                   extraction_dispatcher=ext_dispatcher)
    binding = PostgresOntologyBinding(store, version="baseline-0.1")
    extraction = ExtractionService(
        pipeline, raw_store, binding, LLMJointExtractionStrategy(binding),
        StructuredMapStrategy(binding), SpanGrounder(),
        dispatcher=ext_dispatcher)
    resolution = ResolutionService(pipeline, TieredScorer(store), embedder)

    try:
        return _ingest_run(tenants, add_sources, capture, processing,
                           extraction, resolution, store, limit, watch,
                           interval, say)
    except Exception as e:
        detail = _infra_failure(e)
        if detail is None:
            raise
        say(f"[FAIL] ingest stopped: {detail}, then re-run — sweeps are "
            f"idempotent")
        return 1


def _ingest_run(tenants, add_sources, capture, processing, extraction,
                resolution, store, limit, watch, interval,
                say: Callable[[str], None]) -> int:
    for spec in add_sources:
        ref, _, folder = spec.partition("=")
        if not ref or not folder:
            say(f"--add-source {spec!r}: expected <source_ref>=<folder>")
            return 2
        root = Path(folder).resolve()
        if not root.is_dir():
            say(f"--add-source {spec!r}: {root} is not a directory")
            return 2
        for tenant in tenants:
            existing = capture.registry.get(tenant, ref)
            config = dict(existing.config) if existing else {}
            config["root"] = str(root)
            capture.registry.register(tenant, ref, "filesystem", config)
            say(f"[{tenant}] source {ref!r} registered -> {root}")

    def sweep() -> None:
        for tenant in tenants:
            entries = capture.registry.list_for_tenant(tenant)
            if not entries:
                say(f"[{tenant}] no sources registered — "
                    f"khctl ingest --tenant {tenant} "
                    f"--add-source <ref>=<folder>")
            for entry in entries:
                if entry.status == "disabled":
                    say(f"[{tenant}] {entry.source_ref}: disabled — skipped")
                    continue
                adapter, why_not = adapter_for(entry)
                if adapter is None:
                    say(f"[{tenant}] {entry.source_ref}: skipped — {why_not}")
                    continue
                result = capture.run_source(tenant, adapter)
                line = (f"[{tenant}] capture {entry.source_ref}: "
                        f"landed={result.landed} replayed={result.replayed} "
                        f"tombstoned={result.tombstoned} status={result.status}")
                if result.reason:
                    line += f" ({result.reason})"
                say(line)
            processed = processing.consume(tenant, limit=limit)
            extracted = extraction.consume(tenant, limit=limit)
            summary = resolution.sweep(tenant, limit=limit)
            say(f"[{tenant}] processed={len(processed)} "
                f"extracted={len(extracted)} "
                f"resolved: {summary.model_dump(exclude={'tenant_id'})}")
            print_status(store, tenant, say)

    if not watch:
        sweep()
        return 0
    say(f"watching — one sweep every {interval:g}s (Ctrl-C stops)")
    try:
        while True:
            sweep()
            time.sleep(interval)
    except KeyboardInterrupt:
        say("watch stopped.")
    return 0
