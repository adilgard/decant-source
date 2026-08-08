"""khctl — the profile-driven installer's front door (§8.9, net-new item 1).

Terraform-shaped on purpose:

  khctl probe    read-only environment sweep      -> probe_report.json
  khctl plan     preset x probe x operator choice -> deploy_plan.json + .env.deploy
  khctl apply    execute the plan                 (deploy_apply phases;
                 --dry-run = the walk-in rehearsal)
  khctl verify   prove the plan's claims live     (knowledge_hub.checks,
                 plan-selected — same primitives as check_stack.py)

Schema state (migrations.py — the ledger's own front door):

  khctl migrations status        READ-ONLY: what the ledger claims vs what
                                the database actually has, per migration
  khctl migrations mark-applied  record DDL that reached the database
                                outside apply (note REQUIRED)

Kit lifecycle (deploy_kit.py — the producer to apply's consumer):

  khctl make-kit    assemble the signed, air-gap-capable kit onto the SSD
  khctl verify-kit  the arrival gate: hashes + signature + no unlisted
                    files + no secret-shaped artifacts

The SSD front door (deploy_launch.py — Build Prompt 18):

  khctl launch      guided + stateful: clean box -> the gated deploy flow;
                    deployed box -> start the Data Ingestion program
  khctl ingest      the Data Ingestion program itself (capture -> process
                    -> extract -> resolve over registered sources)
  khctl make-ssd    write the SSD-root shortcuts next to kit/ (launcher +
                    Open Console)

Operator access (Build Prompt 23 — the console door + its keys):

  khctl console             ensure the operator service is up, open the
                            browser at :8081/ui/ (dev/pilot context: also
                            mint + print a throwaway dev credential; a
                            DEPLOYED context NEVER mints here)
  khctl provision-operator  mint + register + print ONCE an additional
                            operator/reviewer console credential (the
                            issue-more path; vault custody is the gate)
  khctl provision-agent     mint + register + print ONCE a new AGENT
                            serving credential (BP25/F16 — the re-mint
                            path for the token agents present at :8080)
  khctl alerts              list open failure alerts; --retry / --ack act
                            on them (BP25/F5 — the day-2 errors surface
                            over the existing /v1/alerts endpoints)

probe_report.json + deploy_plan.json + .env.deploy ARE the engagement
record: what we found, what we decided, what we wired. Nothing here invents
configuration — plan only renders decisions the probe evidences or the
operator states.

stdlib argparse, no CLI framework — same discipline as service_http.py.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from knowledge_hub import deploy_probe, deploy_profiles
from knowledge_hub.deploy_apply import PILOT_PLACEHOLDER_TOKEN, UNSEAL_COMMAND
from knowledge_hub.deploy_probe import ProbeReport, run_probe, summarize
from knowledge_hub.deploy_profiles import (
    DeployPlan,
    PlanError,
    load_profiles,
    render_env,
    resolve_plan,
)

# Pilot compose defaults: the .env contract install-ubuntu.sh replays. The
# "ours" stack renders these; theirs/remote seams override per plan.
# BAO_ROOT_TOKEN is the dev placeholder — phase_env NEVER copies it over a
# deployed home's real token (B3; deploy_apply owns the constant).
PILOT_ENV_DEFAULTS = {
    "POSTGRES_USER": "kh",
    "POSTGRES_PASSWORD": "kh_pilot_pw",
    "POSTGRES_DB": "knowledge_hub",
    "S3_ENDPOINT": "http://localhost:8333",
    "S3_ACCESS_KEY": "kh_s3_admin",
    "S3_SECRET_KEY": "kh_s3_secret_pw",
    "S3_RAW_BUCKET": "kh-raw",
    "BAO_ADDR": "http://localhost:8200",
    "BAO_ROOT_TOKEN": PILOT_PLACEHOLDER_TOKEN,
}

# Discover-without-flags: the pilot-default local endpoints every sweep
# tries, so a box already running pieces of the stack reports them.
LOCAL_CANDIDATES = {
    "postgres": "postgresql://kh:kh_pilot_pw@localhost:5432/knowledge_hub",
    "s3": ("http://localhost:8333", "kh_s3_admin", "kh_s3_secret_pw", "kh-raw"),
    "vault": "http://localhost:8200",
    "ollama": "http://localhost:11434",
}


# ---------------------------------------------------------------------------
# Posture banner verbosity (d.s Stage 2)
# ---------------------------------------------------------------------------
# Which commands get the FULL four-line banner and which get one line. The rule
# is one question: can this command change state on this box, mint a credential,
# or produce an artifact that LEAVES this box? If yes, the posture changes what
# it does and the operator reads the whole thing. If it only reports, the
# one-line form still makes the posture impossible to mistake, which is the
# actual safety property — the four-line form is for where it costs a decision.
#
# Names are the argparse command strings; `migrations` is keyed on its
# subcommand ("migrations mark-applied") because its two halves land on
# opposite sides of that question. Both sets are enumerated rather than one
# being "everything else": a subcommand added later is UNCLASSIFIED and
# test_posture.py fails until someone decides which side it is on. A default
# would have quietly answered that for them.
FULL_BANNER_COMMANDS = frozenset({
    "plan",                     # writes deploy_plan.json + .env.deploy
    "apply",                    # changes the box
    "make-kit",                 # artifact for ANOTHER machine (the hard gate)
    "launch",                   # deploys, or starts the ingestion program
    "ingest",                   # writes documents, facts, raw objects
    "make-ssd",                 # writes the SSD root
    "console",                  # starts a service; may mint a credential
    "provision-operator",       # mints a credential
    "provision-agent",          # mints a credential
    "migrations mark-applied",  # writes the ledger
})

BRIEF_BANNER_COMMANDS = frozenset({
    "probe",                    # read-only sweep -> a local report
    "verify",                   # read-only proof of a deployed plan
    "verify-kit",               # read-only arrival gate
    "alerts",                   # lists; --retry/--ack are small acts on it
    "migrations status",        # explicitly READ-ONLY
})


def banner_key(args: argparse.Namespace) -> str:
    """The name to classify this invocation under: the command, plus the
    subcommand where one exists (only `migrations` has one today)."""
    sub = getattr(args, "migrations_command", None)
    return f"{args.command} {sub}" if sub else args.command


def wants_full_banner(args: argparse.Namespace) -> bool:
    """True for the full banner. An UNCLASSIFIED command gets the full one:
    if nobody has decided yet, the verbose side is the safe side, and the test
    that requires a decision fails on the next run either way."""
    return banner_key(args) not in BRIEF_BANNER_COMMANDS


def _cmd_probe(args: argparse.Namespace) -> int:
    profiles_path = Path(args.profiles)
    egress = []
    if profiles_path.exists():
        egress = load_profiles(profiles_path).kit.egress_targets
    s3_candidates = [LOCAL_CANDIDATES["s3"]]
    pg_candidates = [LOCAL_CANDIDATES["postgres"]]
    env_path = Path(".env")
    if env_path.exists():
        # A deployed home's .env carries the MINTED S3 pair (BP28 #21) —
        # with only the static pilot candidate, a re-probe over a live box
        # would report its own object store as dead.
        from knowledge_hub.deploy_apply import dsn_from_env, parse_env_file
        deployed = parse_env_file(env_path)
        if deployed.get("S3_ACCESS_KEY") and deployed.get("S3_SECRET_KEY"):
            s3_candidates.insert(0, (
                deployed.get("S3_ENDPOINT", LOCAL_CANDIDATES["s3"][0]),
                deployed["S3_ACCESS_KEY"], deployed["S3_SECRET_KEY"],
                deployed.get("S3_RAW_BUCKET")))
        # BP34's counterpart for Postgres: a deploy that moved off 5432
        # (declined client Postgres holding it) must still find ITSELF on
        # a re-probe — the static 5432 candidate now points at THEIRS.
        deployed_dsn = dsn_from_env(deployed)
        if deployed_dsn != LOCAL_CANDIDATES["postgres"]:
            pg_candidates.insert(0, deployed_dsn)
    for spec in args.s3_endpoint or []:
        # endpoint,access_key,secret_key[,bucket]
        parts = spec.split(",")
        if len(parts) < 3:
            print(f"--s3-endpoint {spec!r}: expected "
                  f"endpoint,access_key,secret_key[,bucket]", file=sys.stderr)
            return 2
        s3_candidates.append((parts[0], parts[1], parts[2],
                              parts[3] if len(parts) > 3 else None))
    report = run_probe(
        postgres_dsns=[*pg_candidates, *(args.postgres_dsn or [])],
        s3_endpoints=s3_candidates,
        vault_addrs=[LOCAL_CANDIDATES["vault"], *(args.vault_addr or [])],
        ollama_hosts=[LOCAL_CANDIDATES["ollama"], *(args.ollama_host or [])],
        egress_targets=egress)
    out = Path(args.out)
    out.write_text(report.to_json(), encoding="utf-8")
    print(summarize(report))
    print("-" * 44)
    print(f"report written: {out}")
    return 0


def _cmd_plan(args: argparse.Namespace) -> int:
    probe_path = Path(args.probe)
    if not probe_path.exists():
        print(f"probe report not found: {probe_path} — run `khctl probe` "
              f"first", file=sys.stderr)
        return 2
    probe = ProbeReport.from_json(probe_path.read_text(encoding="utf-8"))
    profiles = load_profiles(Path(args.profiles))
    try:
        plan = resolve_plan(
            profiles, args.profile, probe,
            use=args.use, tenants=(args.tenants.split(",") if args.tenants
                                   else []),
            allow_gated_tier=args.allow_gated_tier,
            custody=args.custody,
            probe_file=str(probe_path),
            confirm_no_gpu=args.confirm_no_gpu)
    except PlanError as e:
        print(f"plan refused:\n{e}", file=sys.stderr)
        return 1
    out_dir = Path(args.out_dir)
    plan_path = out_dir / "deploy_plan.json"
    env_path = out_dir / ".env.deploy"
    plan_path.write_text(plan.to_json(), encoding="utf-8")
    env_path.write_text(render_env(plan, PILOT_ENV_DEFAULTS),
                        encoding="utf-8")
    print(f"profile {plan.profile} (shape {plan.shape}, {plan.placement})")
    for seam, d in plan.seams.items():
        detail = d.endpoint or d.compose_service or ""
        flag = " [operator override]" if d.operator_override else ""
        failed = [r.rule for r in d.qualification if not r.passed]
        qual = f" qualified" if d.qualification and not failed else \
               (f" UNPROVEN({', '.join(failed)})" if failed else "")
        print(f"  {seam:13s} -> {d.choice:6s} {detail}{qual}{flag}")
    if plan.extraction_tier:
        print(f"  extraction    -> {plan.extraction_tier} "
              f"({plan.extraction_model})")
    print(f"  custody       -> {plan.secrets_custody}"
          f"{' [operator override]' if plan.custody_overridden else ''}")
    # BP46 Fix 5: the locality claim is printed from the seam value, in the
    # plan output the operator reads out loud on site — the same sentence the
    # rendered .env carries (INFERENCE_SEAM) and any audit answer must give.
    if "inference" in plan.seams:
        print(f"  data locality -> {plan.data_locality()}")
    print(f"plan written: {plan_path}\nenv written:  {env_path} "
          f"(apply copies it to .env)")
    return 0


def _cmd_apply(args: argparse.Namespace) -> int:
    from knowledge_hub.deploy_apply import ApplyContext, run_apply

    plan = DeployPlan.from_json(
        Path(args.plan).read_text(encoding="utf-8"))
    env_file = Path(args.env_file)
    if not env_file.exists():
        print(f"{env_file} not found — run `khctl plan` first "
              f"(apply executes only what a plan rendered)", file=sys.stderr)
        return 2
    return run_apply(ApplyContext(
        plan=plan,
        infra_dir=Path(args.infra_dir).resolve(),
        kit_dir=Path(args.kit).resolve(),
        env_file=env_file.resolve(),
        dry_run=args.dry_run))


def verify_checks_for(plan: DeployPlan) -> list[tuple[str, object]]:
    """Map a deploy plan to the checks that prove its claims — the same
    primitives check_stack.py runs, selected and targeted by the plan.
    Targets default to settings because post-apply the rendered .env IS the
    plan's config; explicit endpoints (theirs/remote) come from the plan.

    Selection rules:
      - version integrity always, FIRST (drift discipline).
      - the DB-backed stack (postgres/store/vault/benchmark/serving) only
        when the plan has a postgres seam — a hosted connector-agent
        footprint has no client-side stack to prove.
      - local inference -> the model-dependent checks (ollama, processing,
        extraction, resolution); remote -> endpoint reachability + models
        instead (auth/TLS hardening lands with §8.9 item 2);
        local-external -> the same endpoint check as remote but proving the
        endpoint is ON this box, because we installed no model there and the
        deploy is on-premises (BP46 Fix 5).
      - the side-door audit (§8.8 rider) on EVERY visit that has a DB.
    """
    from knowledge_hub import checks
    from knowledge_hub.config import settings

    selected: list[tuple[str, object]] = [
        ("version integrity", checks.check_version),
        # Unconditional, like version integrity: it depends on no seam and
        # no service, and the invariant it guards is true of the package
        # itself rather than of anything this deployment happens to run.
        ("core boundary (corpus-agnostic)", checks.check_core_boundary)]
    has_db = "postgres" in plan.seams
    inference = plan.seams.get("inference")

    if has_db:
        selected.append(("postgres", checks.check_postgres))
        # Straight after postgres: the schema being PRESENT and the ledger
        # being HONEST about it are two different claims, and only the first
        # one was ever verified (2026-08-03 pilot finding).
        selected.append(("migration ledger", checks.check_migrations))
    if "object_store" in plan.seams:
        selected.append(("seaweedfs (s3)", checks.check_s3_worm))
    if "secrets" in plan.seams:
        # d.s Stage 3: prove the seam THIS posture actually uses. A local run
        # has no vault to authenticate against, so asking for one would fail a
        # healthy box; the credential claim — store it, inject it, never leak
        # it — is proven either way, against whichever backend is live.
        selected.append(
            ("credential seam", checks.check_credential_seam)
            if settings.is_local else ("openbao", checks.check_openbao))
    if inference and inference.choice == "local":
        selected.append(("ollama", checks.check_ollama))
        selected.append(("processing (parse·chunk·embed)",
                         checks.check_processing))
        if has_db:
            selected.append(("extraction (ontology·llm·ground)",
                             checks.check_extraction))
            selected.append(("resolution (policy·splink·adjudicate)",
                             checks.check_resolution))
    elif inference and inference.choice == "local-external":
        # BP46 Fix 5: an endpoint we did not provision, so no model-install
        # claim to prove — but it IS on this box, and the check says so
        # instead of borrowing the off-premises wording.
        endpoint = inference.endpoint
        selected.append(("local-external inference (operator-supplied)",
                         lambda: checks.check_local_external_inference(
                             endpoint, extraction_model=plan.extraction_model)))
    elif inference and inference.choice == "remote":
        endpoint = inference.endpoint
        selected.append(("remote inference",
                         lambda: checks.check_remote_inference(
                             endpoint, extraction_model=plan.extraction_model)))
    if has_db:
        selected.append(("benchmark harness", checks.check_benchmark))
        selected.append(("serving service (S5)", checks.check_serving))
        if "secrets" in plan.seams:
            selected.append(("operator write API (BP19)",
                             checks.check_operator))
        # The §8.8 property needs BOTH halves on every visit. side doors is
        # the negative (nothing is connected that shouldn't be); usage
        # attribution is the positive (a served read is traceable to the
        # principal who made it). Either alone is a half-answer that reads
        # like a whole one.
        selected.append(("side doors (§8.8 negative)", checks.check_side_doors))
        selected.append(("usage attribution (§8.8 positive)",
                         checks.check_usage_attribution))
    return selected


def _cmd_verify(args: argparse.Namespace) -> int:
    from knowledge_hub import checks

    plan = DeployPlan.from_json(Path(args.plan).read_text(encoding="utf-8"))
    selected = verify_checks_for(plan)
    print(f"Knowledge Hub — plan verification "
          f"(profile={plan.profile}, shape={plan.shape})\n" + "-" * 44)
    failures: list[str] = []
    for name, fn in selected:
        result = checks.run_check(name, fn)
        if result.passed:
            print(f"[ OK ] {result.detail}")
        else:
            failures.append(name)
            print(f"[FAIL] {name}: {result.detail}")
    print("-" * 44)
    if failures:
        print(f"[FAIL] {len(failures)} failing: {', '.join(failures)}")
        return 1
    print(f"[ OK ] plan verified — {len(selected)} checks green")
    return 0


def _cmd_make_kit(args: argparse.Namespace) -> int:
    from knowledge_hub.config import settings
    from knowledge_hub.deploy_apply import ApplyError
    from knowledge_hub.deploy_kit import (KitContext, default_kit_models,
                                          run_make_kit)

    # THE HARD GATE (d.s Stage 2) — the load-bearing item of the whole posture
    # build, and the reason internal-by-default is safe rather than merely
    # convenient. Every other ceremony can be skipped locally because nothing
    # leaves this box. A kit is the one artifact that DOES: it is built here and
    # run somewhere else, on hardware we do not watch, by someone who will trust
    # whatever we handed them. So the softness that is correct for a single-user
    # internal tool becomes a defect the moment it is packaged for another
    # machine — and the way that defect would actually happen is not a bad
    # decision, it is FORGETTING. Nobody chooses to ship unsigned; they build a
    # kit on a Tuesday having never set KH_POSTURE and hand over a drive.
    #
    # This makes forgetting impossible, because the build itself refuses. It is
    # a REFUSAL, not a warning: a warning is something you scroll past, and
    # there is no --force. Hardening is a deliberate act with a name.
    #
    # FIRST in the function, before default_kit_models() reads profiles.toml
    # and long before any staging: a local-posture build must abort in
    # milliseconds, having touched neither the SSD nor a minute of hashing.
    if settings.is_local:
        print(f"[FAIL] make-kit REFUSES to run in the {settings.posture} "
              f"posture.")
        print("")
        print("       A kit is built HERE and run on ANOTHER machine, so it "
              "must carry the")
        print("       hardened posture: signed manifest, arrival gate, real "
              "credential custody.")
        print("       Local posture skips exactly those, which is right for "
              "internal use and")
        print("       wrong for anything that leaves this box.")
        print("")
        print("       Build a kit by hardening on purpose:")
        print("")
        print("           $env:KH_POSTURE = \"deployed\"        # PowerShell, "
              "this session")
        print("           KH_POSTURE=deployed khctl make-kit …  # bash, this "
              "command")
        print("")
        print("       There is deliberately no override flag. Shipping soft "
              "should cost a")
        print("       decision, not a keystroke.")
        return 1

    infra_dir = Path(args.infra_dir).resolve()
    if args.models:
        models = [m.strip() for m in args.models.split(",") if m.strip()]
    else:
        # The default comes from KIT DATA (profiles.toml tier pins), never
        # from pilot settings — settings.extraction_model is a runtime name
        # (:latest), and defaulting to it once pinned the wrong model (BP33).
        try:
            models = default_kit_models(infra_dir)
        except ApplyError as e:
            print(f"[FAIL] make-kit: {e}")
            return 1
        print(f"--models defaulted from profiles.toml tier pins + the "
              f"embedding pin: {', '.join(models)}")
    return run_make_kit(KitContext(
        infra_dir=infra_dir,
        out_dir=Path(args.out).resolve(),
        models=models,
        skip=set(args.skip),
        sign_key=Path(args.sign_key) if args.sign_key else None,
        allow_unsigned=args.allow_unsigned,
        ollama_store=Path(args.ollama_dir) if args.ollama_dir else None,
        ollama_host=settings.ollama_host))


def _cmd_verify_kit(args: argparse.Namespace) -> int:
    from knowledge_hub.config import settings
    from knowledge_hub.deploy_apply import ApplyError
    from knowledge_hub.deploy_kit import verify_kit_strict

    # d.s Stage 2: the arrival gate is a DEPLOYED-posture concern — it exists to
    # prove a kit survived the trip to a site. Running d.s locally never needs
    # it, so local skips with a one-line notice.
    #
    # A skip, not a refusal, and the difference is deliberate: make-kit PRODUCES
    # something dangerous, so it refuses. verify-kit only READS, so refusing
    # would be theater — and worse, it would block the one legitimate local use,
    # which is checking a kit somebody handed you. Hence --anyway: no state
    # changes, no artifact leaves, so an operator who explicitly asks for the
    # gate gets it in either posture.
    if settings.is_local and not args.anyway:
        print(f"[SKIP] kit arrival gate — skipped (local posture). This gate "
              f"proves a kit")
        print(f"       survived the trip to another machine; running d.s here "
              f"does not need it.")
        print(f"       Run it anyway with --anyway, or set KH_POSTURE=deployed.")
        return 0

    kit_dir = Path(args.kit).resolve()
    print(f"Knowledge Hub — kit arrival gate: {kit_dir}\n" + "-" * 44)
    manifest_path = kit_dir / "manifest.json"
    if manifest_path.exists():
        import json
        total = sum(a["bytes"] for a in json.loads(
            manifest_path.read_text(encoding="utf-8")).get("artifacts", []))
        if total > 5e9:
            print(f"(hashing {total / 1e9:.0f}GB of artifacts — a full kit "
                  f"takes a few minutes; silence here is work, not a hang)")
    try:
        for line in verify_kit_strict(kit_dir,
                                      allow_unsigned=args.allow_unsigned):
            print(f"[ OK ] {line}" if not line.startswith("[WARN]")
                  else line)
    except ApplyError as e:
        print(f"[FAIL] {e}")
        print("-" * 44)
        print("[FAIL] kit REFUSED — do not deploy from it")
        return 1
    print("-" * 44)
    print("[ OK ] kit verified — safe to hand to khctl apply")
    return 0


def _migrations_target(args: argparse.Namespace) -> tuple[Path, Path, str, str]:
    """(migrations_dir, baseline, dsn, where-the-dsn-came-from) for the
    migrations subcommands. Same inputs apply uses — --infra-dir for the
    bundle, --env-file for the config — so status reports on exactly the
    database the deployed processes read, not a guess."""
    from knowledge_hub.deploy_apply import dsn_from_env, parse_env_file
    from knowledge_hub.migrations import BASELINE_SCHEMA

    infra_dir = Path(args.infra_dir).resolve()
    env_file = Path(args.env_file)
    if env_file.exists():
        env = parse_env_file(env_file)
        return (infra_dir / "migrations", infra_dir / BASELINE_SCHEMA,
                dsn_from_env(env),
                f"{env_file} -> {env.get('POSTGRES_HOST', 'localhost')}:"
                f"{env.get('POSTGRES_PORT', '5432')}/"
                f"{env.get('POSTGRES_DB', 'knowledge_hub')}")
    from knowledge_hub.config import settings
    return (infra_dir / "migrations", infra_dir / BASELINE_SCHEMA,
            settings.postgres_dsn,
            f"config defaults ({env_file} absent) -> "
            f"{settings.postgres_host}:{settings.postgres_port}/"
            f"{settings.postgres_db}")


def _cmd_migrations_status(args: argparse.Namespace) -> int:
    """READ-ONLY: what the ledger claims vs what the database actually has.

    The question khctl could not answer before 2026-08-03, which is how two
    progress docs came to disagree about whether 011-013 were applied with no
    way to settle it. Connects with default_transaction_read_only=on, so this
    is safe to run against a client's production box: a write would error
    rather than land.
    """
    import psycopg

    from knowledge_hub import migrations as mig

    migrations_dir, baseline, dsn, origin = _migrations_target(args)
    if not migrations_dir.is_dir():
        print(f"no migrations/ under {migrations_dir.parent} — point "
              f"--infra-dir at the bundle folder (the kit, or the repo root)",
              file=sys.stderr)
        return 2
    print(f"Knowledge Hub — migration ledger vs database\n"
          f"  target: {origin}\n"
          f"  files : {migrations_dir}\n" + "-" * 44)
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10,
                             options="-c default_transaction_read_only=on"
                             ) as conn:
            if not mig.ledger_exists(conn):
                print(f"[WARN] no {mig.LEDGER_TABLE} table — this database has "
                      f"never been through `khctl apply`")
            statuses = mig.status(conn, migrations_dir, baseline)
    except Exception as e:
        print(f"[FAIL] cannot read the database: {type(e).__name__}: {e}")
        return 1
    for line in mig.format_report(statuses):
        print(line)
    print("-" * 44)
    bad = mig.broken(statuses)
    todo = mig.pending(statuses)
    if bad:
        print(f"[FAIL] {len(bad)} migration(s) BROKEN — ledger and database "
              f"disagree:")
        for s in bad:
            print(f"       {s.filename}  [{s.state}]")
        print("       Reconcile deliberately before applying or ingesting. "
              "For DDL that reached the database outside khctl apply, "
              "`khctl migrations mark-applied` records it (a note is "
              "REQUIRED, so a backfilled row never looks like a replayed "
              "one).")
        return 1
    print(f"[ OK ] ledger agrees with the database — "
          f"{len(statuses) - len(todo)} applied, {len(todo)} pending")
    return 0


def _cmd_migrations_mark_applied(args: argparse.Namespace) -> int:
    """Record migrations as applied WITHOUT running them.

    Only legal for a file whose objects VERIFIABLY already exist (state
    BROKEN:objects-without-ledger). Anything else is refused: a pending
    migration must be replayed by apply, and a half-applied one
    (BROKEN:partial) is a schema repair no ledger row can stand in for.
    """
    import psycopg

    from knowledge_hub import migrations as mig

    migrations_dir, baseline, dsn, origin = _migrations_target(args)
    observed = None
    if args.observed_at:
        from datetime import datetime
        try:
            observed = datetime.fromisoformat(args.observed_at)
        except ValueError:
            print(f"--observed-at is not ISO-8601: {args.observed_at!r}",
                  file=sys.stderr)
            return 2
    print(f"Knowledge Hub — ledger backfill\n  target: {origin}\n" + "-" * 44)
    with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
        statuses = {s.filename: s
                    for s in mig.status(conn, migrations_dir, baseline)}
        planned = []
        for name in args.file:
            s = statuses.get(name)
            if s is None:
                print(f"[FAIL] {name} is not a bundled migration file "
                      f"(known: {', '.join(sorted(statuses)) or 'none'})")
                return 1
            if s.state == mig.OBJECTS_NO_LEDGER:
                planned.append(s)
                continue
            print(f"[FAIL] {name} is {s.state}, not "
                  f"{mig.OBJECTS_NO_LEDGER} — mark-applied records DDL that "
                  f"is already present, and this file's objects are not "
                  f"verifiably all there. Refusing.")
            return 1
        if not planned:
            print("[FAIL] nothing to do — pass --file for each migration to "
                  "record")
            return 1
        for s in planned:
            print(f"  {s.filename}")
            print(f"    objects verified present: {', '.join(s.present)}")
        print(f"  recorded on every row above:")
        print(f"    applied_at: "
              f"{observed.isoformat() if observed else 'now()'}")
        print(f"    note      : {args.note}")
        if not args.yes:
            try:
                answer = input(
                    "\nWrite these ledger rows? [y/N] ").strip().lower()
            except EOFError:
                # Non-interactive and no --yes: refuse rather than traceback.
                # Writing on an unanswerable prompt would be the wrong default.
                print("no console to confirm on and --yes not given — "
                      "nothing written")
                return 1
            if answer not in ("y", "yes"):
                print("aborted — nothing written")
                return 1
        print("-" * 44)
        for s in planned:
            wrote = mig.mark_applied(conn, s.filename, args.note, observed)
            print(f"[ OK ] {s.filename} recorded" if wrote
                  else f"[WARN] {s.filename} already had a row — left alone")
    print("-" * 44)
    print("[ OK ] backfill complete — re-run `khctl migrations status` to "
          "confirm the ledger now agrees with the database")
    return 0


def _vault_status(addr: str) -> str:
    """'ok' | 'sealed' | 'unreachable' — the transport-level truth the F1
    messages branch on. A sealed vault refuses EVERY credential; reporting
    that as 'bad token' sends the operator down a re-issue spiral."""
    import hvac
    try:
        return "sealed" if hvac.Client(url=addr).sys.is_sealed() else "ok"
    except Exception:
        return "unreachable"


def _refuse_sealed(addr: str) -> None:
    print(f"[FAIL] the vault at {addr} is SEALED — normal after any "
          f"reboot, NOT a credential problem. Every login and every "
          f"provisioning call will be refused until it is unsealed.")
    print(f"       Unseal with 3 of the 5 custody shares: "
          f"`{UNSEAL_COMMAND}`   (run 3x, one share each)")


def _resolve_console_work_dir(explicit: str | None) -> Path:
    """Where the console runs from: an explicit --work-dir, else a DEPLOYED
    home if one exists ($KH_WORK_DIR or ~/knowledge-hub with a plan), else
    the current directory (the dev/pilot bench)."""
    import os

    if explicit:
        return Path(explicit).resolve()
    for candidate in (os.environ.get("KH_WORK_DIR"),
                      str(Path.home() / "knowledge-hub")):
        if candidate and (Path(candidate) / "deploy_plan.json").exists():
            return Path(candidate).resolve()
    return Path.cwd()


def _cmd_console(args: argparse.Namespace) -> int:
    """The door (BP23): guarantee the operator service is up (REUSES the
    launcher's ensure_operator — no second start path), then open the
    browser at /ui/. Context decides the key handling:

      dev/pilot (no deploy_plan.json)  -> mint + print a THROWAWAY dev
        credential so there is a key to type — the pilot vault is dev-mode
        (ephemeral), so dev keys die with it.
      deployed (deploy_plan.json)      -> NEVER mints. The operator pastes
        the print-once credential from deploy bootstrap or
        `khctl provision-operator`.
    """
    import webbrowser

    from knowledge_hub.deploy_apply import parse_env_file
    from knowledge_hub.deploy_launch import ensure_operator

    work = _resolve_console_work_dir(args.work_dir)
    env = (parse_env_file(work / ".env")
           if (work / ".env").exists() else {})
    # Context gate: DEPLOYED needs BOTH the engagement record AND a
    # non-dev vault. The pilot bench legitimately accumulates test
    # deploy_plan.json artifacts, but only the bench carries the dev-mode
    # vault literal in .env — a real deployment's .env carries the unique
    # root token phase_openbao minted at init (and phase_env preserves it
    # across re-plans, B3), never the dev literal. So dev-mint can never
    # fire on a real deploy.
    deployed = ((work / "deploy_plan.json").exists()
                and env.get("BAO_ROOT_TOKEN", "") != PILOT_PLACEHOLDER_TOKEN)
    host = env.get("OPERATOR_HOST", "127.0.0.1")
    port = env.get("OPERATOR_PORT", "8081")
    url = f"http://{host}:{port}/ui/"

    print(f"decant.Source — operator console "
          f"({'DEPLOYED' if deployed else 'dev/pilot'} context, "
          f"home: {work})")

    # F1: a sealed vault refuses every login as "credential not
    # recognized" — say the real cause BEFORE opening a console nobody
    # can log into. Local posture has no vault, so there is no seal to
    # diagnose and this whole class of failure does not exist (d.s Stage 3).
    from knowledge_hub.config import settings
    if settings.is_deployed:
        if _vault_status(env.get("BAO_ADDR", settings.bao_addr)) == "sealed":
            _refuse_sealed(env.get("BAO_ADDR", settings.bao_addr))
            return 1

    # F6: honor the supervisor — a dead service must not get a browser
    # opened onto connection-refused under a success message.
    if not ensure_operator(work, env, print):
        print("")
        print(f"[FAIL] the operator console service did not come up — NOT "
              f"opening a browser onto a dead page.")
        print(f"       Diagnose: {work / 'operator.log'}, then `docker "
              f"compose ps` in {work}, then `khctl verify`.")
        return 1

    if settings.is_local:
        # d.s Stage 3: nothing to mint, print, record, or paste. The console
        # asks the service for this box's own identity over /ui/local-session
        # and logs itself in. No credential is printed HERE precisely because
        # printing one would put a human back in the loop for no benefit —
        # the browser and the service are both on this machine, and the
        # credential file is readable by both.
        print("\nLocal posture: the console logs itself in with this box's "
              "own identity.\nNothing to record or paste. (Connector "
              "credentials are the one thing you\nstill enter — those are "
              "real third-party secrets.)")
    elif deployed:
        print("\nDeployed context: no credential is minted here (by "
              "design). Log in with the print-once operator credential "
              "from deploy bootstrap, or issue one with "
              "`khctl provision-operator --tenant <t> --role operator`.")
    elif args.no_mint:
        print("\n--no-mint: log in with an existing credential.")
    else:
        import getpass

        import hvac

        from knowledge_hub.config import settings
        from knowledge_hub.deploy_apply import (
            _print_once_credential,
            provision_operator_credential,
        )
        client = hvac.Client(url=settings.bao_addr,
                             token=settings.bao_root_token)
        if not client.is_authenticated():
            print("[FAIL] pilot vault not answering — `docker compose up "
                  "-d` in the bundle folder, then re-run")
            return 1
        tenant = args.tenant or "default"
        token, pid = provision_operator_credential(
            client, settings.bao_kv_mount, tenant, "operator",
            f"dev:{getpass.getuser()}")
        print("\n  DEV-ONLY console credential — pilot vault is dev-mode "
              "(ephemeral); this key dies with it. Never use on a "
              "deployed box.")
        _print_once_credential("dev operator credential", tenant, token, pid)

    if not args.no_browser:
        webbrowser.open(url)
        print(f"\nbrowser opened at {url}")
    else:
        print(f"\nconsole: {url}")
    return 0


def _provision_locally(tenant: str, roles: tuple[str, ...], label: str,
                       actor: str) -> int:
    """Mint one credential into the local store (d.s Stage 3).

    Both provision-* commands land here in local posture. They survive rather
    than being switched off because handing a token to an EXTERNAL agent is a
    real integration need, not ceremony — something outside this process has to
    be given a credential somehow. What does not survive is the custody gate
    around it: in deployed posture the vault refusing your token IS the gate,
    and locally the equivalent gate is being able to write the file at all,
    which is the same boundary the .env already relies on.

    Still printed once, because the value genuinely has to reach a human here —
    it is going into some other program's config. confirm_recorded skips its
    "type RECORDED" hold in local posture (Stage 2), so there is a record on
    screen without a prompt to answer: the token is not recoverable, but
    re-issuing one costs a single command with no ceremony, which is the
    difference that made the hold worth skipping.
    """
    from knowledge_hub.config import settings
    from knowledge_hub.deploy_apply import _print_once_credential
    from knowledge_hub.secrets_local import provision_local_credential

    token, pid = provision_local_credential(tenant, roles, actor, label)
    _print_once_credential(label, tenant, token, pid)
    reissue = ("provision-agent" if not roles else "provision-operator")
    print(f"registered (digest only) in {settings.local_secrets_file}; "
          f"attributed to {actor!r}. Local posture: no vault, no custody "
          f"ceremony. The store holds digests, so the value above cannot be "
          f"read back — but `khctl {reissue} --tenant {tenant}` issues another "
          f"whenever you need one.")
    return 0


def _cmd_provision_operator(args: argparse.Namespace) -> int:
    """Issue-more (BP23): mint + register + print ONCE an additional
    console credential. Vault custody IS the gate — this only works where
    the vault answers to your token (the deploy context / the bench)."""
    import getpass

    import hvac

    from knowledge_hub.deploy_apply import (
        _print_once_credential,
        parse_env_file,
        provision_operator_credential,
    )

    work = _resolve_console_work_dir(args.work_dir)
    env = (parse_env_file(work / ".env")
           if (work / ".env").exists() else {})
    from knowledge_hub.config import settings
    if settings.is_local:
        return _provision_locally(
            args.tenant, (args.role,),
            f"{args.role.upper()} console credential", getpass.getuser())
    addr = env.get("BAO_ADDR", settings.bao_addr)
    mount = env.get("BAO_KV_MOUNT", settings.bao_kv_mount)
    bao_token = env.get("BAO_ROOT_TOKEN", settings.bao_root_token)
    # F1: sealed ≠ custody refusal — a sealed vault refuses EVERY token,
    # and telling the operator "custody gate" sends them re-issuing keys
    # that were never the problem.
    if _vault_status(addr) == "sealed":
        _refuse_sealed(addr)
        return 1
    client = hvac.Client(url=addr, token=bao_token)
    if not client.is_authenticated():
        print(f"[FAIL] vault at {addr} did not accept the token — "
              f"provisioning requires vault custody (that refusal is the "
              f"gate working)")
        return 1
    actor = getpass.getuser()
    token, pid = provision_operator_credential(
        client, mount, args.tenant, args.role, actor)
    _print_once_credential(
        f"{args.role.upper()} console credential", args.tenant, token, pid)
    print(f"registered (digest only) in the principal registry; "
          f"attributed to {actor!r}. Hand the value to its holder over a "
          f"safe channel — it cannot be recovered, only re-issued.")
    return 0


def _cmd_provision_agent(args: argparse.Namespace) -> int:
    """F16: re-mint the AGENT SERVING credential — the token external
    agents present at the read boundary (:8080). phase_tenants mints the
    first one; losing it used to mean hand-rolled hvac on-site. Same
    custody gate + print-once ceremony as provision-operator."""
    import getpass

    import hvac

    from knowledge_hub.deploy_apply import (
        _print_once_credential,
        parse_env_file,
        provision_agent_credential,
    )

    work = _resolve_console_work_dir(args.work_dir)
    env = (parse_env_file(work / ".env")
           if (work / ".env").exists() else {})
    from knowledge_hub.config import settings
    if settings.is_local:
        return _provision_locally(
            args.tenant, (),
            "AGENT SERVING credential (for the read boundary, :8080)",
            getpass.getuser())
    addr = env.get("BAO_ADDR", settings.bao_addr)
    mount = env.get("BAO_KV_MOUNT", settings.bao_kv_mount)
    bao_token = env.get("BAO_ROOT_TOKEN", settings.bao_root_token)
    if _vault_status(addr) == "sealed":
        _refuse_sealed(addr)
        return 1
    client = hvac.Client(url=addr, token=bao_token)
    if not client.is_authenticated():
        print(f"[FAIL] vault at {addr} did not accept the token — "
              f"provisioning requires vault custody (that refusal is the "
              f"gate working)")
        return 1
    actor = getpass.getuser()
    token, pid = provision_agent_credential(client, mount, args.tenant,
                                            actor)
    _print_once_credential(
        "AGENT SERVING credential (for the read boundary, :8080)",
        args.tenant, token, pid)
    print(f"registered (digest only) in the principal registry; attributed "
          f"to {actor!r}. This credential has NO console role — agents "
          f"read through the serving API only. Any previously issued agent "
          f"credential keeps working until revoked.")
    return 0


def _parse_alert_spec(spec: str) -> tuple[str, int] | None:
    queue, _, raw = spec.partition(":")
    if queue in ("dispatch", "extraction") and raw.isdigit():
        return queue, int(raw)
    return None


def _cmd_alerts(args: argparse.Namespace) -> int:
    """F5: the failure surface — list open alerts, retry or acknowledge
    them, over the operator API's EXISTING /v1/alerts + retry_failed_item +
    acknowledge_alert (this CLI is their first consumer). Auth is an
    operator credential: KH_OPERATOR_TOKEN or a hidden prompt — the same
    key that opens the console."""
    import getpass
    import json as _json
    import os
    import urllib.error
    import urllib.request

    from knowledge_hub.deploy_apply import parse_env_file

    work = _resolve_console_work_dir(args.work_dir)
    env = (parse_env_file(work / ".env")
           if (work / ".env").exists() else {})
    base = args.url or (f"http://{env.get('OPERATOR_HOST', '127.0.0.1')}:"
                        f"{env.get('OPERATOR_PORT', '8081')}")
    token = os.environ.get("KH_OPERATOR_TOKEN", "").strip()
    if not token:
        token = getpass.getpass(
            "operator credential (hidden; or set KH_OPERATOR_TOKEN): ").strip()

    def call(method: str, path: str, body: dict | None = None):
        request = urllib.request.Request(
            base + path, method=method,
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
            data=(_json.dumps(body).encode("utf-8")
                  if body is not None else None))
        try:
            with urllib.request.urlopen(request, timeout=10) as resp:
                return resp.status, _json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, _json.loads(e.read() or b"{}")
            except Exception:
                return e.code, {}
        except Exception as e:
            return None, {"error": f"{type(e).__name__}: {e}"}

    def gate(status, body) -> int | None:
        """Map transport/auth failures to an honest [FAIL]; None = proceed."""
        if status is None:
            print(f"[FAIL] operator service unreachable at {base} "
                  f"({body.get('error')}) — start it: `khctl console` "
                  f"(or the launcher)")
            return 1
        if status == 401:
            # F1 discipline: don't blame the token while the vault is the
            # problem — the health surface knows.
            h_status, health = call("GET", "/v1/health")
            if h_status and health.get("vault_status") == "sealed":
                _refuse_sealed("the deployment's vault")
            else:
                print("[FAIL] credential not recognized — use the OPERATOR "
                      "CONSOLE credential (issue one: khctl "
                      "provision-operator)")
            return 1
        if status == 403:
            print("[FAIL] this credential has no console role — it is an "
                  "agent serving token; use the OPERATOR credential")
            return 1
        return None

    failures = 0
    for spec in args.retry:
        parsed = _parse_alert_spec(spec)
        if parsed is None:
            print(f"[FAIL] --retry {spec!r}: expected "
                  f"dispatch:<id> or extraction:<id>")
            return 2
        status, body = call("POST", "/v1/actions/retry_failed_item",
                            {"queue": parsed[0], "item_id": parsed[1]})
        rc = gate(status, body)
        if rc is not None:
            return rc
        if status == 200:
            print(f"[ OK ] {spec}: requeued (ack cleared) — the next sweep "
                  f"picks it up")
        else:
            failures += 1
            print(f"[FAIL] {spec}: {body.get('detail') or body.get('error')}")
    for spec in args.ack:
        parsed = _parse_alert_spec(spec)
        if parsed is None:
            print(f"[FAIL] --ack {spec!r}: expected "
                  f"dispatch:<id> or extraction:<id>")
            return 2
        status, body = call("POST", "/v1/actions/acknowledge_alert",
                            {"kind": parsed[0], "item_id": parsed[1]})
        rc = gate(status, body)
        if rc is not None:
            return rc
        if status == 200:
            print(f"[ OK ] {spec}: acknowledged — it leaves the alert list "
                  f"without being retried")
        else:
            failures += 1
            print(f"[FAIL] {spec}: {body.get('detail') or body.get('error')}")

    status, body = call("GET", "/v1/alerts")
    rc = gate(status, body)
    if rc is not None:
        return rc
    if status != 200:
        print(f"[FAIL] /v1/alerts answered {status}: {body}")
        return 1
    alerts = body.get("alerts", [])
    print(f"decant.Source — open alerts for tenant "
          f"{body.get('tenant_id')!r}\n" + "-" * 44)
    if not alerts:
        print("[ OK ] no open alerts — nothing failed, nothing degraded")
        return 1 if failures else 0
    for a in alerts:
        when = (a.get("created_at") or "")[:19]
        print(f"  {a['kind']:<11} #{a['ref_id']:<6} {when:<19}  "
              f"{a.get('detail') or ''}")
    print("-" * 44)
    print(f"{len(alerts)} open. Act on queue items: khctl alerts "
          f"--retry <kind>:<id>   |   --ack <kind>:<id>")
    print("(a degraded source clears by being FIXED — resume it from the "
          "console or fix its credential/root)")
    return 1 if failures else 0


def _cmd_launch(args: argparse.Namespace) -> int:
    from knowledge_hub.deploy_launch import (
        DEFAULT_WORK_DIR,
        LaunchConfig,
        run_launch,
    )

    import os
    work = (Path(args.work_dir) if args.work_dir
            else Path(os.environ.get("KH_WORK_DIR", str(DEFAULT_WORK_DIR))))
    return run_launch(LaunchConfig(
        kit_dir=Path(args.kit).resolve(),
        work_dir=work,
        profile=args.profile,
        tenants=args.tenants,
        custody=args.custody,
        allow_gated_tier=args.allow_gated_tier,
        confirm_no_gpu=args.confirm_no_gpu,
        dry_run=args.dry_run))


def _cmd_ingest(args: argparse.Namespace) -> int:
    from knowledge_hub.deploy_launch import run_ingest

    return run_ingest(
        tenants=args.tenant or [],
        add_sources=args.add_source,
        limit=args.limit,
        watch=args.watch,
        interval=args.interval)


def _cmd_make_ssd(args: argparse.Namespace) -> int:
    from knowledge_hub.deploy_kit import NESTED_DIR_DEFAULT, write_ssd_root

    for line in write_ssd_root(Path(args.root).resolve(),
                               kit_subdir=args.kit_subdir,
                               nested_dir=(None if args.flat
                                           else NESTED_DIR_DEFAULT)):
        print(f"[ OK ] {line}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="khctl",
        description="Knowledge Hub deployment: probe -> plan -> apply -> verify")
    parser.add_argument("--profiles", default="profiles.toml",
                        help="profile presets file (kit data)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("probe", help="read-only environment sweep")
    p.add_argument("--postgres-dsn", action="append",
                   help="candidate client Postgres DSN (repeatable)")
    p.add_argument("--s3-endpoint", action="append",
                   help="candidate store: endpoint,access_key,secret_key[,bucket]")
    p.add_argument("--vault-addr", action="append",
                   help="candidate Vault/OpenBao address (repeatable)")
    p.add_argument("--ollama-host", action="append",
                   help="candidate Ollama host (repeatable)")
    p.add_argument("--out", default="probe_report.json")
    p.set_defaults(fn=_cmd_probe)

    p = sub.add_parser("plan", help="resolve profile x probe -> deploy plan")
    p.add_argument("--profile", required=True,
                   help="offering: appliance | client-gpu | hosted")
    p.add_argument("--probe", default="probe_report.json")
    p.add_argument("--use", action="append",
                   help="seam override: seam=choice[:endpoint], e.g. "
                        "postgres=theirs:postgresql://u:p@host/db")
    p.add_argument("--tenants", help="comma-separated tenant/domain ids")
    p.add_argument("--allow-gated-tier", action="store_true",
                   help="permit quantized tiers whose quality is unmeasured "
                        "(Axis D pending)")
    p.add_argument("--confirm-no-gpu", action="store_true",
                   help="the operator has CONFIRMED this box has no GPU, so "
                        "a probe that found none is a fact and not a "
                        "detection miss — only then may planning surface the "
                        "Scenario-2 commercial fork (BP46 Fix 2)")
    p.add_argument("--custody", choices=["operator", "client", "auto"],
                   help="unseal-key custody override; default = the "
                        "profile's per-offering custody (DEPLOY_NOTES.md)")
    p.add_argument("--out-dir", default=".")
    p.set_defaults(fn=_cmd_plan)

    p = sub.add_parser("apply", help="execute a deploy plan")
    p.add_argument("--plan", default="deploy_plan.json")
    p.add_argument("--env-file", default=".env.deploy",
                   help="the plan-rendered env (from khctl plan)")
    p.add_argument("--infra-dir", default=".",
                   help="bundle folder: compose files, schema, migrations")
    p.add_argument("--kit", default=".",
                   help="kit dir: manifest.json, images/, ollama_models/")
    p.add_argument("--dry-run", action="store_true",
                   help="walk every phase, print resolved actions, "
                        "change nothing")
    p.set_defaults(fn=_cmd_apply)

    p = sub.add_parser("verify", help="prove a deployed plan's claims live")
    p.add_argument("--plan", default="deploy_plan.json")
    p.set_defaults(fn=_cmd_verify)

    p = sub.add_parser("migrations",
                       help="what is ACTUALLY applied: ledger vs database")
    msub = p.add_subparsers(dest="migrations_command", required=True)

    mp = msub.add_parser(
        "status",
        help="READ-ONLY: compare the ledger against the live objects, "
             "per migration (exit 1 on drift)")
    mp.add_argument("--infra-dir", default=".",
                    help="bundle folder with migrations/ + the baseline "
                         "schema (the kit, or the repo root)")
    mp.add_argument("--env-file", default=".env",
                    help="the config the deployed processes read — status "
                         "reports on THAT database, not a guess")
    mp.set_defaults(fn=_cmd_migrations_status)

    mp = msub.add_parser(
        "mark-applied",
        help="record migrations whose DDL reached the database outside "
             "khctl apply; refuses unless the objects verifiably exist")
    mp.add_argument("--file", action="append", required=True,
                    metavar="NNN_name.sql",
                    help="migration filename to record (repeatable)")
    mp.add_argument("--note", required=True,
                    help="REQUIRED: why this row is written without a "
                         "replay — a backfilled row must never be "
                         "indistinguishable from a replayed one")
    mp.add_argument("--observed-at",
                    help="ISO-8601 timestamp the DDL actually ran, when the "
                         "database can evidence one (else now() is recorded)")
    mp.add_argument("--yes", action="store_true",
                    help="skip the confirmation prompt (scripted repair)")
    mp.add_argument("--infra-dir", default=".")
    mp.add_argument("--env-file", default=".env")
    mp.set_defaults(fn=_cmd_migrations_mark_applied)

    p = sub.add_parser("make-kit",
                       help="assemble the deployment kit (SSD image)")
    p.add_argument("--out", required=True, help="kit target dir (the SSD)")
    p.add_argument("--infra-dir", default=".",
                   help="build folder: compose, schema, migrations, package")
    p.add_argument("--models",
                   help="comma-separated models to carry (default: the "
                        "embedding pin + the default-status tier pins from "
                        "profiles.toml — NEVER the bench's runtime "
                        "extraction setting; a gated tier ships only via "
                        "this flag)")
    p.add_argument("--skip", action="append", default=[],
                   choices=["wheelhouse", "python", "images", "models",
                            "tokenizer"],
                   help="omit a component (recorded in the manifest; "
                        "repeatable)")
    p.add_argument("--sign-key",
                   help="minisign SECRET key path (or KH_SIGN_KEY env) — "
                        "human-held, never committed or kitted; signing "
                        "is REQUIRED unless --allow-unsigned")
    p.add_argument("--allow-unsigned", action="store_true",
                   help="dev bench ONLY: build without a signature "
                        "(recorded in the manifest); never a client kit")
    p.add_argument("--ollama-dir",
                   help="ollama model store to copy from "
                        "(default ~/.ollama/models)")
    p.set_defaults(fn=_cmd_make_kit)

    p = sub.add_parser("verify-kit",
                       help="arrival gate: signature first, then hashes + "
                            "tree audit")
    p.add_argument("--kit", default=".", help="kit dir to verify")
    p.add_argument("--allow-unsigned", action="store_true",
                   help="dev bench ONLY: accept an unsigned kit "
                        "(the acceptance is printed as a recorded override)")
    p.add_argument("--anyway", action="store_true",
                   help="run the gate even in local posture, where it is "
                        "skipped by default (it only reads — use this to "
                        "check a kit someone handed you)")
    p.set_defaults(fn=_cmd_verify_kit)

    p = sub.add_parser("launch",
                       help="the SSD front door: guided deploy on a clean "
                            "box, start the Data Ingestion program on a "
                            "deployed one (stateful, gate-respecting)")
    p.add_argument("--kit", default=".", help="kit dir (the SSD's kit/)")
    p.add_argument("--work-dir",
                   help="deployment home on this box (default: "
                        "$KH_WORK_DIR or ~/knowledge-hub)")
    p.add_argument("--profile", default="appliance",
                   help="offering for the guided deploy (default appliance)")
    p.add_argument("--tenants",
                   help="comma-separated tenant ids (prompted if omitted)")
    p.add_argument("--custody", choices=["operator", "client", "auto"],
                   help="unseal-key custody override (passed to plan)")
    p.add_argument("--allow-gated-tier", action="store_true",
                   help="passed through to plan")
    p.add_argument("--confirm-no-gpu", action="store_true",
                   help="passed through to plan: the operator has confirmed "
                        "this box has no GPU (BP46 Fix 2)")
    p.add_argument("--dry-run", action="store_true",
                   help="rehearsal session: apply never mutates")
    p.set_defaults(fn=_cmd_launch)

    p = sub.add_parser("ingest",
                       help="the Data Ingestion program: capture -> process "
                            "-> extract -> resolve over registered sources")
    p.add_argument("--tenant", action="append",
                   help="tenant to sweep (repeatable)")
    p.add_argument("--add-source", action="append", default=[],
                   help="register a filesystem source first: "
                        "<source_ref>=<folder> (repeatable)")
    p.add_argument("--limit", type=int, default=1000,
                   help="max queue items per stage per sweep")
    p.add_argument("--watch", action="store_true",
                   help="keep sweeping every --interval seconds")
    p.add_argument("--interval", type=float, default=30.0)
    p.set_defaults(fn=_cmd_ingest)

    p = sub.add_parser("make-ssd",
                       help="write the SSD root: one launch shortcut + one "
                            "folder holding launch.sh, kit/, the console "
                            "pair, PREREQS.txt (trust anchor rendered in)")
    p.add_argument("--root", required=True, help="the SSD root directory")
    p.add_argument("--kit-subdir", default="kit",
                   help="kit folder name next to launch.sh (default: kit)")
    p.add_argument("--flat", action="store_true",
                   help="pre-BP27 layout: everything at the root instead "
                        "of the two-things nested layout")
    p.set_defaults(fn=_cmd_make_ssd)

    p = sub.add_parser("console",
                       help="ensure the operator service is up and open the "
                            "browser at /ui/ (dev context mints + prints a "
                            "throwaway key; deployed context never mints)")
    p.add_argument("--tenant",
                   help="dev-mint tenant (dev context only; default "
                        "'default')")
    p.add_argument("--work-dir",
                   help="deployment home (default: detected — $KH_WORK_DIR "
                        "or ~/knowledge-hub if deployed, else cwd)")
    p.add_argument("--no-browser", action="store_true",
                   help="print the URL instead of opening a browser")
    p.add_argument("--no-mint", action="store_true",
                   help="dev context: skip the dev credential mint")
    p.set_defaults(fn=_cmd_console)

    p = sub.add_parser("provision-operator",
                       help="mint + register + print ONCE an additional "
                            "console credential (vault custody is the gate)")
    p.add_argument("--tenant", required=True)
    p.add_argument("--role", choices=["operator", "reviewer"],
                   default="operator")
    p.add_argument("--work-dir",
                   help="deployment home whose .env names the vault "
                        "(default: detected)")
    p.set_defaults(fn=_cmd_provision_operator)

    p = sub.add_parser("provision-agent",
                       help="mint + register + print ONCE a new AGENT "
                            "serving credential (the re-mint path for the "
                            "token agents present at :8080; vault custody "
                            "is the gate)")
    p.add_argument("--tenant", required=True)
    p.add_argument("--work-dir",
                   help="deployment home whose .env names the vault "
                        "(default: detected)")
    p.set_defaults(fn=_cmd_provision_agent)

    p = sub.add_parser("alerts",
                       help="list open failure alerts (failed queue items, "
                            "degraded sources); --retry/--ack act on them "
                            "— the day-2 errors surface")
    p.add_argument("--retry", action="append", default=[],
                   metavar="QUEUE:ID",
                   help="requeue a failed item: dispatch:<id> or "
                        "extraction:<id> (repeatable)")
    p.add_argument("--ack", action="append", default=[],
                   metavar="QUEUE:ID",
                   help="acknowledge an alert without retrying (repeatable)")
    p.add_argument("--url",
                   help="operator API base (default from the deployment "
                        ".env, e.g. http://127.0.0.1:8081)")
    p.add_argument("--work-dir",
                   help="deployment home (default: detected)")
    p.set_defaults(fn=_cmd_alerts)

    args = parser.parse_args(argv)
    # d.s Stage 1: EVERY khctl invocation announces its posture, before the
    # subcommand runs. argparse has already exited for --help/-h and for a bad
    # command line, so this prints exactly when real work is about to happen.
    # Full banner where the posture changes what the command DOES, one line
    # where it only colors a report (see FULL_BANNER_COMMANDS).
    from knowledge_hub.config import print_posture_banner
    print_posture_banner(brief=not wants_full_banner(args))
    print("")
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
