"""khctl launch — state detection, the adoption gate, the plan pause, and
the SSD wrapper (Build Prompt 18). Pure logic throughout: the launcher's
orchestration is exercised with a recording fake runner + scripted console,
so these tests prove the GATES (what runs, what pauses, what never runs)
without touching docker or a live stack. The wet path is the round-trip
rehearsal on the staged kit.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from knowledge_hub.deploy_kit import (
    TRUSTED_PUBKEYS,
    render_launch_sh,
    write_ssd_root,
)
from knowledge_hub.deploy_launch import (
    STATE_DEPLOYED,
    STATE_FRESH,
    STATE_PLANNED,
    STATE_PROBED,
    AdoptionCandidate,
    LaunchConfig,
    StateSignals,
    adapter_for,
    adoption_candidates,
    choose_ours_postgres_port,
    classify_state,
    gather_signals,
    run_adoption_gate,
    run_launch,
    run_plan_pause,
    seed_work_dir,
)
from knowledge_hub import migrations as mig
from knowledge_hub.deploy_profiles import load_profiles
from knowledge_hub.models import SourceRegistryEntry

from test_deploy_plan import make_probe, qualified_pg

INFRA_DIR = Path(__file__).resolve().parents[2]
PROFILES = load_profiles(INFRA_DIR / "profiles.toml")


@pytest.fixture(autouse=True)
def _restore_cwd():
    """run_launch pins CWD to the deployment home (by design); tests must
    not leak that into the rest of the suite."""
    import os
    before = os.getcwd()
    yield
    os.chdir(before)


# ---------------------------------------------------------------------------
# Console + runner fakes
# ---------------------------------------------------------------------------
class Console:
    """Scripted operator: answers come off the front of `answers`; running
    out of answers is a test bug we want loud."""

    def __init__(self, answers: list[str]):
        self.answers = list(answers)
        self.transcript: list[str] = []

    def ask(self, prompt: str) -> str:
        self.transcript.append(f"? {prompt}")
        if not self.answers:
            raise AssertionError(f"console exhausted at prompt: {prompt!r}")
        return self.answers.pop(0)

    def say(self, line: str) -> None:
        self.transcript.append(line)

    def text(self) -> str:
        return "\n".join(self.transcript)


KHCTL_COMMANDS = {"probe", "plan", "apply", "verify", "make-kit",
                  "verify-kit", "launch", "ingest", "make-ssd"}


def _command_of(argv: list[str]) -> str:
    return next(t for t in argv if t in KHCTL_COMMANDS)


class FakeRunner:
    """Records every subcommand argv; simulates the artifacts the flow needs
    (probe writes a report, plan writes plan + env) so the launcher's own
    file-driven steps proceed. `fail_on` forces a subcommand to return 1."""

    def __init__(self, work: Path, probe=None, fail_on: str | None = None,
                 tenants: list[str] | None = None):
        self.work = work
        self.calls: list[list[str]] = []
        self.probe = probe or make_probe()
        self.fail_on = fail_on
        self.tenants = tenants or []

    def commands(self) -> list[str]:
        return [_command_of(argv) for argv in self.calls]

    def __call__(self, argv: list[str]) -> int:
        self.calls.append(list(argv))
        command = _command_of(argv)
        if command == self.fail_on:
            return 1
        if command == "probe":
            out = Path(argv[argv.index("--out") + 1])
            out.write_text(self.probe.to_json(), encoding="utf-8")
        if command == "plan":
            plan = {
                "plan_version": "1", "profile": "appliance", "shape": "A",
                "placement": "single_box", "secrets_custody": "operator",
                "custody_overridden": False,
                "seams": {"postgres": {"seam": "postgres", "choice": "ours",
                                       "compose_service": "postgres"}},
                "extraction_tier": "fp16", "extraction_model": "qwen3.6",
                "tenants": self.tenants,
            }
            (self.work / "deploy_plan.json").write_text(
                json.dumps(plan), encoding="utf-8")
            (self.work / ".env.deploy").write_text(
                "POSTGRES_USER=kh\n", encoding="utf-8")
        return 0


def clean_ledger(state: str = mig.APPLIED, n: int = 3) -> list:
    """A ledger-check result with nothing wrong — the shape start_program's
    gate accepts. Built through the real classify() so a change to the state
    vocabulary breaks these tests instead of silently passing them."""
    files = [mig.MigrationFile(filename=f"{i:03d}_x.sql", path=Path("x"),
                               creates=(f"t{i}",), verifiable=(f"t{i}",),
                               unverified=())
             for i in range(1, n + 1)]
    live = {f"t{i}" for i in range(1, n + 1)}
    ledger = {f.filename: mig.LedgerRow(filename=f.filename, applied_at=None,
                                        note=None) for f in files}
    statuses = mig.classify(files, ledger, live)
    assert all(s.state == state for s in statuses)
    return statuses


def drifted_ledger() -> list:
    """The pilot's shape: objects present, no ledger row."""
    files = [mig.MigrationFile(filename="011_ontology_registry.sql",
                               path=Path("x"), creates=("ontology_active",),
                               verifiable=("ontology_active",),
                               unverified=())]
    statuses = mig.classify(files, {}, {"ontology_active"})
    assert statuses[0].state == mig.OBJECTS_NO_LEDGER
    return statuses


def launch_config(work: Path, kit: Path, console: Console,
                  runner: FakeRunner, **kw) -> LaunchConfig:
    kw.setdefault("tenants", "")          # skip the tenant prompt by default
    kw.setdefault("stack_check", lambda _env: False)
    # A clean ledger by default, so the pure-logic tests stay pure. The gate's
    # own refusals are proven explicitly in the two tests below.
    kw.setdefault("ledger_check", lambda _work, _env: clean_ledger())
    return LaunchConfig(kit_dir=kit, work_dir=work, runner=runner,
                        input_fn=console.ask, print_fn=console.say, **kw)


@pytest.fixture()
def kit_dir(tmp_path: Path) -> Path:
    kit = tmp_path / "kit"
    kit.mkdir()
    (kit / "manifest.json").write_text("{}", encoding="utf-8")
    (kit / "profiles.toml").write_text(
        (INFRA_DIR / "profiles.toml").read_text(encoding="utf-8"),
        encoding="utf-8")
    return kit


@pytest.fixture()
def work_dir(tmp_path: Path) -> Path:
    work = tmp_path / "home"
    work.mkdir()
    return work


# ---------------------------------------------------------------------------
# State detection
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("signals,expected", [
    (StateSignals(False, False, False, False), STATE_FRESH),
    (StateSignals(True, False, False, False), STATE_PROBED),
    (StateSignals(True, True, False, False), STATE_PLANNED),
    (StateSignals(True, True, True, False), STATE_PLANNED),   # dead stack
    (StateSignals(True, True, True, True), STATE_DEPLOYED),
    (StateSignals(False, True, True, True), STATE_DEPLOYED),  # probe pruned
])
def test_classify_state_ladder(signals, expected):
    assert classify_state(signals) == expected


def test_gather_signals_reads_artifacts_and_injected_stack_check(work_dir):
    checked: list[Path] = []

    def check(env: Path) -> bool:
        checked.append(env)
        return True

    sig = gather_signals(work_dir, check)
    assert sig == StateSignals(False, False, False, False)
    assert not checked  # no .env -> live check never attempted

    (work_dir / "probe_report.json").write_text("{}", encoding="utf-8")
    (work_dir / "deploy_plan.json").write_text("{}", encoding="utf-8")
    (work_dir / ".env.deploy").write_text("", encoding="utf-8")
    (work_dir / ".env").write_text("", encoding="utf-8")
    sig = gather_signals(work_dir, check)
    assert sig == StateSignals(True, True, True, True)
    assert checked == [work_dir / ".env"]


# ---------------------------------------------------------------------------
# The adoption gate — detection
# ---------------------------------------------------------------------------
def test_no_reachable_candidates_means_no_gate():
    probe = make_probe(postgres=[qualified_pg(reachable=False,
                                              error="refused")])
    assert adoption_candidates(probe, PROFILES, "appliance") == []


def test_qualified_postgres_is_gated_and_labeled():
    # client-gpu allows postgres = ours|theirs -> the operator must choose.
    probe = make_probe(postgres=[qualified_pg()])
    found = adoption_candidates(probe, PROFILES, "client-gpu")
    assert [(c.seam, c.qualified, c.adoptable) for c in found] == \
        [("postgres", True, True)]
    assert "pg.client.lan" in found[0].display


def test_unqualified_but_reachable_postgres_is_still_surfaced():
    # plan would pass it by silently; the launcher says it out loud.
    probe = make_probe(postgres=[qualified_pg(
        ext_available={"vector": False, "age": False, "pg_trgm": False})])
    found = adoption_candidates(probe, PROFILES, "client-gpu")
    assert [(c.seam, c.qualified, c.adoptable) for c in found] == \
        [("postgres", False, True)]


def test_appliance_pins_postgres_so_detection_is_notice_only():
    # appliance: postgres = ours (fixed). The detection is surfaced but
    # there is no adoption decision to force.
    probe = make_probe(postgres=[qualified_pg()])
    found = adoption_candidates(probe, PROFILES, "appliance")
    assert [(c.seam, c.adoptable) for c in found] == [("postgres", False)]
    console = Console([])   # no answers: the notice must not prompt
    flags = run_adoption_gate(found, console.ask, console.say)
    assert flags == []
    assert "NOT be touched" in console.text()


def test_hosted_profile_has_no_client_side_stack_to_gate():
    probe = make_probe(postgres=[qualified_pg()])
    assert adoption_candidates(probe, PROFILES, "hosted") == []


# ---------------------------------------------------------------------------
# The adoption gate — the conversation
# ---------------------------------------------------------------------------
GATED = [AdoptionCandidate("postgres",
                           "postgresql://svc:***@pg.client.lan:5432/kh",
                           qualified=True, adoptable=True)]


def test_gate_default_enter_is_self_contained():
    console = Console([""])
    flags = run_adoption_gate(GATED, console.ask, console.say)
    assert flags == ["--use", "postgres=ours"]
    assert "OPERATOR DECISION REQUIRED" in console.text()
    assert "NOT be touched" in console.text()


def test_gate_adopt_records_operator_supplied_endpoint():
    # BP34: adopting now takes a typed ADOPT after the stakes are stated.
    console = Console(["a", "postgresql://svc:pw@pg.client.lan:5432/kh",
                       "ADOPT"])
    flags = run_adoption_gate(GATED, console.ask, console.say)
    assert flags == ["--use",
                     "postgres=theirs:postgresql://svc:pw@pg.client.lan:5432/kh"]
    assert "ADOPTING MEANS WRITES" in console.text()


def test_gate_adopt_unconfirmed_falls_back_to_the_choice():
    # BP34: 'a' + endpoint + anything-but-ADOPT adopts NOTHING — a mis-key
    # can never write into client infrastructure. Enter then declines.
    console = Console(["a", "postgresql://svc:pw@pg.client.lan:5432/kh",
                       "yes", ""])
    flags = run_adoption_gate(GATED, console.ask, console.say)
    assert flags == ["--use", "postgres=ours"]
    assert "not confirmed" in console.text()


def test_gate_quit_returns_none_and_adopts_nothing():
    console = Console(["q"])
    assert run_adoption_gate(GATED, console.ask, console.say) is None


def test_gate_rejects_noise_then_accepts_default():
    console = Console(["yolo", ""])
    flags = run_adoption_gate(GATED, console.ask, console.say)
    assert flags == ["--use", "postgres=ours"]


# ---------------------------------------------------------------------------
# The port step (BP34) — a held 5432 is never contested, gate or no gate
# ---------------------------------------------------------------------------
def test_port_step_default_free_decides_nothing():
    console = Console([])   # no prompt may fire
    assert choose_ours_postgres_port(None, False, console.ask, console.say,
                                     port_free=lambda p: True) is None


def test_port_step_keeps_a_live_deployments_port_without_prompting():
    console = Console([])
    assert choose_ours_postgres_port(5433, True, console.ask, console.say,
                                     port_free=lambda p: True) == 5433
    # a live stack on the default port is equally left alone
    assert choose_ours_postgres_port(5432, True, console.ask, console.say,
                                     port_free=lambda p: True) is None


def test_port_step_busy_default_enter_takes_first_free_suggestion():
    console = Console([""])
    port = choose_ours_postgres_port(None, True, console.ask, console.say,
                                     port_free=lambda p: p != 5433)
    assert port == 5434                      # 5433 busy -> next free offered
    assert "never contested or touched" in console.text()


def test_port_step_rejects_busy_or_junk_typed_ports():
    console = Console(["5432", "club", "5500"])
    port = choose_ours_postgres_port(
        None, True, console.ask, console.say,
        port_free=lambda p: p not in (5432,))
    assert port == 5500
    assert "busy on this box too" in console.text()


def test_guided_flow_port_step_fires_even_without_a_gate(kit_dir, work_dir):
    # The on-site truth: a client Postgres the probe cannot log into raises
    # NO adoption gate (unreachable = invisible), but it still owns 5432 —
    # the port step must fire on the listener alone.
    runner = FakeRunner(work_dir,
                        probe=make_probe(ports_listening={5432: True}))
    console = Console(["",          # probe -> continue
                       "",          # port step: Enter = suggested free port
                       "deploy"])
    rc = run_launch(launch_config(work_dir, kit_dir, console, runner))
    assert rc == 0
    plan_argv = runner.calls[2]
    flags = [plan_argv[i + 1] for i, t in enumerate(plan_argv)
             if t == "--use"]
    assert len(flags) == 1 and flags[0].startswith("postgres=ours:")
    assert flags[0].split(":")[1].isdigit()
    assert "already in use" in console.text()


def test_guided_flow_decline_then_port_shift(kit_dir, work_dir):
    # The full on-site decline shape: gate fires (their PG reachable),
    # Enter declines, the port step moves OUR postgres off their 5432.
    runner = FakeRunner(work_dir,
                        probe=make_probe(postgres=[qualified_pg()],
                                         ports_listening={5432: True}))
    console = Console(["", "", "", "deploy"])
    rc = run_launch(launch_config(work_dir, kit_dir, console, runner,
                                  profile="client-gpu"))
    assert rc == 0
    plan_argv = runner.calls[2]
    flags = [plan_argv[i + 1] for i, t in enumerate(plan_argv)
             if t == "--use"]
    assert flags[0] == "postgres=ours"
    assert flags[1].startswith("postgres=ours:")   # later --use wins in plan


def test_guided_flow_adopting_skips_the_port_step(kit_dir, work_dir):
    # Adopting THEIR postgres brings up no postgres of ours — there is no
    # port to choose, and no prompt may fire.
    runner = FakeRunner(work_dir,
                        probe=make_probe(postgres=[qualified_pg()],
                                         ports_listening={5432: True}))
    console = Console(["",
                       "a", "postgresql://svc:pw@pg.client.lan:5432/kh",
                       "ADOPT",
                       "deploy"])
    rc = run_launch(launch_config(work_dir, kit_dir, console, runner,
                                  profile="client-gpu"))
    assert rc == 0
    plan_argv = runner.calls[2]
    flags = [plan_argv[i + 1] for i, t in enumerate(plan_argv)
             if t == "--use"]
    assert flags == ["postgres=theirs:postgresql://svc:pw@pg.client.lan"
                     ":5432/kh"]


def test_bind_conflict_stderr_is_classified():
    from knowledge_hub.deploy_apply import _bind_conflict
    assert _bind_conflict("Error response from daemon: driver failed "
                          "programming external connectivity on endpoint "
                          "kh-postgres: Bind for 127.0.0.1:5432 failed: "
                          "port is already allocated")
    assert _bind_conflict("listen tcp 127.0.0.1:5432: bind: address "
                          "already in use")
    assert not _bind_conflict("no such image: knowledge-hub/postgres")


# ---------------------------------------------------------------------------
# The plan pause
# ---------------------------------------------------------------------------
def test_plan_pause_deploy_is_wet_only_outside_dry_sessions():
    console = Console(["deploy"])
    assert run_plan_pause(console.ask, console.say, forced_dry=False) is False
    console = Console(["deploy"])
    assert run_plan_pause(console.ask, console.say, forced_dry=True) is True


def test_plan_pause_rehearse_and_quit():
    console = Console(["rehearse"])
    assert run_plan_pause(console.ask, console.say, forced_dry=False) is True
    console = Console(["q"])
    assert run_plan_pause(console.ask, console.say, forced_dry=False) is None


# ---------------------------------------------------------------------------
# The guided flow end to end (fake runner: order + gates, no live services)
# ---------------------------------------------------------------------------
def test_guided_flow_runs_subcommands_in_order(kit_dir, work_dir):
    runner = FakeRunner(work_dir)   # probe: no reachable candidates
    console = Console(["",          # probe -> continue
                       "deploy"])   # plan pause
    rc = run_launch(launch_config(work_dir, kit_dir, console, runner))
    assert rc == 0
    assert runner.commands() == ["verify-kit", "probe", "plan", "apply",
                                 "verify"]
    apply_argv = runner.calls[3]
    assert "--dry-run" not in apply_argv
    assert str(kit_dir) in apply_argv        # producer/consumer symmetry
    assert "--use" not in runner.calls[2]    # nothing to gate, nothing forced


def test_guided_flow_stops_when_arrival_gate_fails(kit_dir, work_dir):
    runner = FakeRunner(work_dir, fail_on="verify-kit")
    console = Console([])
    rc = run_launch(launch_config(work_dir, kit_dir, console, runner))
    assert rc == 1
    assert runner.commands() == ["verify-kit"]   # nothing after the refusal


def test_guided_flow_gate_fires_on_detected_postgres(kit_dir, work_dir):
    runner = FakeRunner(work_dir, probe=make_probe(postgres=[qualified_pg()]))
    console = Console(["",          # probe -> continue
                       "",          # THE GATE: Enter = self-contained
                       "deploy"])
    rc = run_launch(launch_config(work_dir, kit_dir, console, runner,
                                  profile="client-gpu"))
    assert rc == 0
    plan_argv = runner.calls[2]
    i = plan_argv.index("--use")
    assert plan_argv[i + 1] == "postgres=ours"
    assert "EXISTING POSTGRES DETECTED" in console.text()


def test_guided_flow_quit_at_gate_runs_no_plan_no_apply(kit_dir, work_dir):
    runner = FakeRunner(work_dir, probe=make_probe(postgres=[qualified_pg()]))
    console = Console(["", "q"])
    rc = run_launch(launch_config(work_dir, kit_dir, console, runner,
                                  profile="client-gpu"))
    assert rc == 0
    assert runner.commands() == ["verify-kit", "probe"]


def test_guided_flow_appliance_notices_detected_postgres(kit_dir, work_dir):
    # The on-site walk-in shape: appliance profile, a Postgres visible on
    # the network. No adoption prompt (seam is pinned ours), but the
    # non-disruption promise is printed.
    runner = FakeRunner(work_dir, probe=make_probe(postgres=[qualified_pg()]))
    console = Console(["", "deploy"])
    rc = run_launch(launch_config(work_dir, kit_dir, console, runner))
    assert rc == 0
    assert "NOT be touched" in console.text()
    assert "--use" not in runner.calls[2]


def test_guided_flow_quit_at_plan_pause_never_applies(kit_dir, work_dir):
    runner = FakeRunner(work_dir)
    console = Console(["", "q"])
    rc = run_launch(launch_config(work_dir, kit_dir, console, runner))
    assert rc == 0
    assert runner.commands() == ["verify-kit", "probe", "plan"]


def test_guided_flow_rehearse_then_deploy(kit_dir, work_dir):
    runner = FakeRunner(work_dir)
    console = Console(["", "rehearse", "deploy"])
    rc = run_launch(launch_config(work_dir, kit_dir, console, runner))
    assert rc == 0
    assert runner.commands() == ["verify-kit", "probe", "plan", "apply",
                                 "apply", "verify"]
    assert "--dry-run" in runner.calls[3]
    assert "--dry-run" not in runner.calls[4]


def test_dry_run_session_never_wets_apply_and_stops_after(kit_dir, work_dir):
    runner = FakeRunner(work_dir)
    console = Console(["", "deploy"])
    rc = run_launch(launch_config(work_dir, kit_dir, console, runner,
                                  dry_run=True))
    assert rc == 0
    assert runner.commands() == ["verify-kit", "probe", "plan", "apply"]
    assert "--dry-run" in runner.calls[3]
    assert "verify" not in runner.commands()


def test_deployed_state_offers_start_not_redeploy(kit_dir, work_dir):
    (work_dir / "probe_report.json").write_text("{}", encoding="utf-8")
    (work_dir / "deploy_plan.json").write_text("{}", encoding="utf-8")
    (work_dir / ".env.deploy").write_text("", encoding="utf-8")
    (work_dir / ".env").write_text("", encoding="utf-8")
    runner = FakeRunner(work_dir)
    console = Console(["q"])    # deployed menu -> quit
    rc = run_launch(launch_config(work_dir, kit_dir, console, runner,
                                  stack_check=lambda _env: True))
    assert rc == 0
    assert runner.commands() == []           # no guided flow re-entered
    assert "Data Ingestion program" in console.text()


# ---------------------------------------------------------------------------
# Work-dir seeding
# ---------------------------------------------------------------------------
def test_seed_copies_bundle_but_never_engagement_artifacts(tmp_path):
    kit = tmp_path / "kit"
    (kit / "migrations").mkdir(parents=True)
    (kit / "profiles.toml").write_text("x", encoding="utf-8")
    (kit / "docker-compose.yml").write_text("y", encoding="utf-8")
    (kit / "migrations" / "001.sql").write_text("z", encoding="utf-8")
    work = tmp_path / "home"
    work.mkdir()
    (work / ".env").write_text("SECRET=1", encoding="utf-8")
    (work / "deploy_plan.json").write_text("{\"mine\": 1}", encoding="utf-8")

    seed_work_dir(kit, work)
    assert (work / "profiles.toml").read_text(encoding="utf-8") == "x"
    assert (work / "docker-compose.yml").read_text(encoding="utf-8") == "y"
    assert (work / "migrations" / "001.sql").exists()
    # engagement artifacts untouched
    assert (work / ".env").read_text(encoding="utf-8") == "SECRET=1"
    assert "mine" in (work / "deploy_plan.json").read_text(encoding="utf-8")


def test_seed_preserves_an_existing_deployed_file(tmp_path):
    """BP28 #11: a field repair in the deployment home used to be silently
    reverted by the next launch.sh run's re-seed. Present = left in place;
    only NEW filenames land."""
    kit = tmp_path / "kit"
    (kit / "migrations").mkdir(parents=True)
    (kit / "docker-compose.yml").write_text("kit version", encoding="utf-8")
    (kit / "profiles.toml").write_text("profiles", encoding="utf-8")
    (kit / "migrations" / "001.sql").write_text("one", encoding="utf-8")
    (kit / "migrations" / "002.sql").write_text("two", encoding="utf-8")
    work = tmp_path / "home"
    (work / "migrations").mkdir(parents=True)
    (work / "docker-compose.yml").write_text("REPAIRED on site",
                                             encoding="utf-8")
    (work / "migrations" / "001.sql").write_text("one", encoding="utf-8")

    lines = seed_work_dir(kit, work)

    # the repair survives the re-seed
    assert (work / "docker-compose.yml").read_text(
        encoding="utf-8") == "REPAIRED on site"
    # absent bundle file + NEW migration filename still land
    assert (work / "profiles.toml").read_text(encoding="utf-8") == "profiles"
    assert (work / "migrations" / "002.sql").read_text(
        encoding="utf-8") == "two"
    assert any("left in place" in line for line in lines)


def test_seed_copies_the_kit_tokenizer_dir(tmp_path):
    kit = tmp_path / "kit"
    (kit / "tokenizer" / "bge-m3").mkdir(parents=True)
    (kit / "tokenizer" / "bge-m3" / "tokenizer.json").write_text(
        "{}", encoding="utf-8")
    work = tmp_path / "home"
    work.mkdir()
    seed_work_dir(kit, work)
    assert (work / "tokenizer" / "bge-m3" / "tokenizer.json").exists()


# ---------------------------------------------------------------------------
# The SSD wrapper — generated, anchored, LF-clean
# ---------------------------------------------------------------------------
def test_launch_sh_carries_the_current_trust_anchor():
    script = render_launch_sh()
    for key_id, pubkey in TRUSTED_PUBKEYS.items():
        assert pubkey in script and key_id in script
    assert "UNSIGNED" in script          # refuses unsigned kits
    assert "khctl launch" in script      # thin: execs the real flow
    assert "KH_ALLOW_UNSIGNED" not in script  # no dev escape on the SSD


def test_write_ssd_root_writes_the_pair_lf_only(tmp_path):
    lines = write_ssd_root(tmp_path)
    launch = tmp_path / "decant.Source" / "launch.sh"
    desktop = tmp_path / "Launch decant.Source.desktop"
    assert launch.exists() and desktop.exists()
    assert b"\r" not in launch.read_bytes()      # CRLF kills the shebang
    # the root shortcut is the one thing that crosses the folder boundary
    assert "decant.Source/launch.sh" in desktop.read_text(encoding="utf-8")
    assert any("NOT yet" in line for line in lines)  # kit absent -> said


def test_write_ssd_root_nested_root_shows_exactly_two_things(tmp_path):
    # BP27: the SSD root is ONE shortcut + ONE folder; everything else —
    # launch.sh, the console pair, PREREQS.txt, (later) kit/ — lives inside.
    write_ssd_root(tmp_path)
    entries = sorted(p.name for p in tmp_path.iterdir())
    assert entries == ["Launch decant.Source.desktop", "decant.Source"]
    inner = tmp_path / "decant.Source"
    for name in ("launch.sh", "console.sh", "Open Console.desktop",
                 "PREREQS.txt"):
        assert (inner / name).exists()
    # launch.sh stays kit-adjacent: it resolves $SCRIPT_DIR/kit, so the
    # nested move needs no script change.
    script = (inner / "launch.sh").read_text(encoding="utf-8")
    assert 'KIT="$SSD_ROOT/kit"' in script


def test_write_ssd_root_flat_keeps_the_old_layout(tmp_path):
    write_ssd_root(tmp_path, nested_dir=None)
    assert (tmp_path / "launch.sh").exists()
    assert (tmp_path / "Knowledge Hub.desktop").exists()
    assert not (tmp_path / "decant.Source").exists()


def test_ssd_root_files_live_outside_the_kit_tree(tmp_path):
    # The wrapper files must never end up inside kit/ (unlisted files break
    # the chain-of-custody audit).
    write_ssd_root(tmp_path)
    kit = tmp_path / "decant.Source" / "kit"
    assert not kit.exists() or not list(kit.glob("launch.sh"))


# ---------------------------------------------------------------------------
# The linux requirement set (first-full-scale-build lesson: a Windows freeze
# is not a linux lockfile)
# ---------------------------------------------------------------------------
def test_linux_requirements_pin_direct_deps_free_transitives():
    from knowledge_hub.deploy_kit import build_linux_requirements
    requirements = ("pydantic            # data models\n"
                    "FlagEmbedding       # sparse benchmark\n"
                    "# a full-line comment\n")
    lock = ("pydantic==2.8.2\n"
            "FlagEmbedding==1.4.0\n"
            "fsspec==2026.6.0\n"          # the live ResolutionImpossible pin
            "torch==2.13.0\n"
            'pywin32==306; sys_platform == "win32"\n')
    pyproject_deps = ["psycopg[binary]>=3.1", "pydantic-settings>=2"]
    out = build_linux_requirements(requirements, lock, pyproject_deps)
    assert "pydantic==2.8.2" in out            # direct: pilot version kept
    assert "FlagEmbedding==1.4.0" in out
    assert "fsspec" not in out                 # transitive pins are FREED
    assert "torch==2.13.0" in out              # pin-through, never floats
    assert "psycopg[binary]" in out            # extras survive
    assert "pydantic-settings" in out          # unpinned floor passes bare
    assert "pywin32" not in out                # win32 lines never leak


# ---------------------------------------------------------------------------
# Ingest plumbing (pure parts)
# ---------------------------------------------------------------------------
def test_adapter_for_filesystem_requires_a_real_root(tmp_path):
    entry = SourceRegistryEntry(tenant_id="t", source_ref="docs",
                                source_system="filesystem", config={})
    adapter, why = adapter_for(entry)
    assert adapter is None and "config.root" in why

    entry = SourceRegistryEntry(tenant_id="t", source_ref="docs",
                                source_system="filesystem",
                                config={"root": str(tmp_path / "nope")})
    adapter, why = adapter_for(entry)
    assert adapter is None and "not a directory" in why

    root = tmp_path / "watched"
    root.mkdir()
    entry = SourceRegistryEntry(tenant_id="t", source_ref="docs",
                                source_system="filesystem",
                                config={"root": str(root)})
    adapter, why = adapter_for(entry)
    assert adapter is not None and why is None
    assert adapter.source_ref == "docs"


def test_adapter_for_unsupported_system_says_runbook():
    entry = SourceRegistryEntry(tenant_id="t", source_ref="m365",
                                source_system="msgraph-files", config={})
    adapter, why = adapter_for(entry)
    assert adapter is None and "runbook" in why


# ---------------------------------------------------------------------------
# Deployed-state launch starts the OPERATOR CONSOLE too (BP22)
# ---------------------------------------------------------------------------
def test_start_program_starts_serving_and_operator_and_prints_ui_watchpoint(
        work_dir, kit_dir, monkeypatch):
    """On-site the operator watches + resolves through the console, so the
    deployed-state path is not complete until operator_http (:8081, /ui/)
    is up beside serving — and the watch point says where to look."""
    import knowledge_hub.deploy_launch as dl

    plan = {
        "plan_version": "1", "profile": "appliance", "shape": "A",
        "placement": "single_box", "secrets_custody": "operator",
        "custody_overridden": False, "seams": {},
        "extraction_tier": "fp16", "extraction_model": "qwen3.6",
        "tenants": [],
    }
    (work_dir / "deploy_plan.json").write_text(json.dumps(plan),
                                               encoding="utf-8")
    (work_dir / ".env").write_text("SERVING_PORT=8080\nOPERATOR_PORT=8081\n",
                                   encoding="utf-8")

    started: list[str] = []
    # F6 (BP25): start_program now HONORS these results — the fakes must
    # report success or the launcher rightly refuses to claim the program
    # is up.
    monkeypatch.setattr(
        dl, "ensure_serving",
        lambda work, env, say: started.append("serving") or True)
    monkeypatch.setattr(
        dl, "ensure_operator",
        lambda work, env, say: started.append("operator") or True)

    console = Console([])
    runner = FakeRunner(work_dir)
    cfg = launch_config(work_dir, kit_dir, console, runner,
                        stack_check=lambda _env: True)
    rc = dl.start_program(cfg, kit_dir, work_dir)
    assert rc == 0
    assert started == ["serving", "operator"]     # both, serving first
    out = console.text()
    assert "http://127.0.0.1:8081/ui/" in out     # the console watch point
    assert "operator.log" in out


def _deployed_home(work_dir: Path) -> None:
    plan = {
        "plan_version": "1", "profile": "appliance", "shape": "A",
        "placement": "single_box", "secrets_custody": "operator",
        "custody_overridden": False, "seams": {},
        "extraction_tier": "fp16", "extraction_model": "qwen3.6",
        "tenants": [],
    }
    (work_dir / "deploy_plan.json").write_text(json.dumps(plan),
                                               encoding="utf-8")
    (work_dir / ".env").write_text("SERVING_PORT=8080\nOPERATOR_PORT=8081\n",
                                   encoding="utf-8")


@pytest.mark.parametrize("check,expected", [
    # The pilot's drift: objects without a ledger row.
    (lambda _w, _e: drifted_ledger(), "BROKEN"),
    # Schema behind the bundle — apply has not run the newest migrations.
    (lambda _w, _e: mig.classify(
        [mig.MigrationFile("014_new.sql", Path("x"), ("t14",), ("t14",), ())],
        {}, set()), "BEHIND"),
    # The ledger cannot be read at all: never a silent pass.
    (lambda _w, _e: (_ for _ in ()).throw(RuntimeError("no migrations/")),
     "migration ledger"),
])
def test_start_program_refuses_to_ingest_on_a_bad_ledger(
        work_dir, kit_dir, monkeypatch, check, expected):
    """The 2026-08-03 gate: ingest must not start onto a schema whose ledger
    and objects disagree. Before this, every other signal here said the box
    was healthy — the stack answered and the tables existed."""
    import knowledge_hub.deploy_launch as dl

    _deployed_home(work_dir)
    started: list[str] = []
    monkeypatch.setattr(dl, "ensure_serving",
                        lambda w, e, s: started.append("serving") or True)
    monkeypatch.setattr(dl, "ensure_operator",
                        lambda w, e, s: started.append("operator") or True)

    console = Console([])
    cfg = launch_config(work_dir, kit_dir, console, FakeRunner(work_dir),
                        stack_check=lambda _env: True, ledger_check=check)
    rc = dl.start_program(cfg, kit_dir, work_dir)
    assert rc == 1
    assert expected in console.text()
    # It stops BEFORE starting anything — a refusal that already launched the
    # services would be a warning, not a gate.
    assert started == []


def test_ensure_operator_spawns_the_operator_module(work_dir, monkeypatch):
    """The launcher supervises `python -m knowledge_hub.operator_http` —
    the exact process OPERATOR_API_NOTES documents, nothing bespoke."""
    import knowledge_hub.deploy_launch as dl

    spawned: list[list[str]] = []

    class FakeProc:
        pid = 4242
        def poll(self):
            return None

    monkeypatch.setattr(dl.subprocess, "Popen",
                        lambda argv, **kw: (spawned.append(list(argv)),
                                            FakeProc())[1])
    health = iter([False, True])   # not yet answering -> healthy after start
    monkeypatch.setattr(dl, "operator_healthy",
                        lambda host, port: next(health, True))

    lines: list[str] = []
    ok = dl.ensure_operator(work_dir, {"OPERATOR_PORT": "18099"},
                            lines.append)
    assert ok is True
    assert len(spawned) == 1
    assert spawned[0][1:] == ["-m", "knowledge_hub.operator_http"]
    assert any("/ui/" in line for line in lines)


# ---------------------------------------------------------------------------
# BP33 — the settings singleton must follow the deployment home (the 0.26.1
# re-rehearsal deployed cleanly and then watched the launcher's OWN verify
# report 7 false FAILs and its ingest sweep crash on minted S3 credentials:
# the singleton bound at khctl import, in the invocation directory, and
# never saw the .env apply wrote)
# ---------------------------------------------------------------------------
def test_reload_settings_rereads_env_from_cwd(tmp_path, monkeypatch):
    from knowledge_hub.config import reload_settings, settings
    home = tmp_path / "deployhome"
    home.mkdir()
    (home / ".env").write_text("S3_ACCESS_KEY=kh_minted_bp33\n",
                               encoding="utf-8")
    before = settings.s3_access_key
    monkeypatch.chdir(home)
    reload_settings()
    try:
        # the SAME object every importer holds must see the new value
        assert settings.s3_access_key == "kh_minted_bp33"
    finally:
        monkeypatch.chdir(tmp_path)
        (home / ".env").unlink()
        reload_settings()
        assert settings.s3_access_key == before


def test_guided_flow_reloads_settings_after_wet_apply(kit_dir, work_dir):
    """Apply WRITES the deployment .env; everything after it in the same
    launcher session (step-6 verify, the first ingest) must run on those
    values, not on whatever the singleton captured at process start."""
    from knowledge_hub.config import reload_settings, settings

    class EnvWritingRunner(FakeRunner):
        def __call__(self, argv):
            rc = super().__call__(argv)
            if _command_of(argv) == "apply" and "--dry-run" not in argv:
                (self.work / ".env").write_text(
                    "S3_ACCESS_KEY=kh_minted_by_apply\n", encoding="utf-8")
            return rc

    runner = EnvWritingRunner(work_dir)
    console = Console(["", "deploy"])
    before = settings.s3_access_key
    try:
        rc = run_launch(launch_config(work_dir, kit_dir, console, runner))
        assert rc == 0
        assert runner.commands()[-1] == "verify"
        # by the time verify ran, the singleton carried apply's minted value
        assert settings.s3_access_key == "kh_minted_by_apply"
    finally:
        (work_dir / ".env").unlink(missing_ok=True)
        reload_settings()
        assert settings.s3_access_key == before


def test_deployed_launch_reloads_settings_from_work_env(kit_dir, work_dir,
                                                        tmp_path):
    """Deployed state: the launcher chdirs into the home and must refresh
    the singleton so THIS deployment's .env governs the session."""
    from knowledge_hub.config import reload_settings, settings
    (work_dir / "probe_report.json").write_text("{}", encoding="utf-8")
    (work_dir / "deploy_plan.json").write_text(json.dumps({
        "plan_version": "1", "profile": "appliance", "shape": "A",
        "placement": "single_box", "secrets_custody": "operator",
        "custody_overridden": False, "seams": {}, "tenants": [],
    }), encoding="utf-8")
    (work_dir / ".env").write_text("S3_ACCESS_KEY=kh_deployed_home\n",
                                   encoding="utf-8")
    runner = FakeRunner(work_dir)
    console = Console(["q"])                 # deployed menu -> quit
    before = settings.s3_access_key
    try:
        rc = run_launch(launch_config(
            work_dir, kit_dir, console, runner,
            stack_check=lambda _env: True))
        assert rc == 0
        assert settings.s3_access_key == "kh_deployed_home"
    finally:
        (work_dir / ".env").unlink(missing_ok=True)
        reload_settings()
        assert settings.s3_access_key == before


def test_start_program_waits_for_vault_and_reports_sealed(kit_dir, work_dir,
                                                          monkeypatch):
    """BP33: compose up can RECREATE the vault container (observed once per
    fresh deploy when .env gained the real root token) — the seal check must
    ride the same readiness gate as the apply phases, then report SEALED
    honestly instead of a connection-reset traceback."""
    import knowledge_hub.deploy_apply as da
    import knowledge_hub.deploy_launch as dl
    import knowledge_hub.deploy_probe as dp

    (work_dir / "deploy_plan.json").write_text(json.dumps({
        "plan_version": "1", "profile": "appliance", "shape": "A",
        "placement": "single_box", "secrets_custody": "operator",
        "custody_overridden": False,
        "seams": {"secrets": {"seam": "secrets", "choice": "ours"}},
        "tenants": [],
    }), encoding="utf-8")
    (work_dir / ".env").write_text("BAO_ADDR=http://localhost:18200\n",
                                   encoding="utf-8")

    waited: list[str] = []
    monkeypatch.setattr(da, "_await_vault_ready",
                        lambda ctx, addr: waited.append(addr) or "ready")

    class SealedReport:
        sealed = True
    monkeypatch.setattr(dp, "probe_secrets", lambda addr: SealedReport())

    lines: list[str] = []
    cfg = LaunchConfig(kit_dir=kit_dir, work_dir=work_dir,
                       runner=FakeRunner(work_dir), input_fn=lambda _: "",
                       print_fn=lines.append,
                       stack_check=lambda _env: True,
                       # This test is about the VAULT gate; the ledger gate
                       # sits earlier in start_program and would otherwise
                       # refuse first (no bundle in a tmp work dir).
                       ledger_check=lambda _w, _e: clean_ledger())
    rc = dl.start_program(cfg, kit_dir, work_dir)
    assert rc == 1
    assert waited == ["http://localhost:18200"]     # readiness gate ran
    joined = "\n".join(lines)
    assert "SEALED" in joined and "custody" in joined
    assert "unreachable" not in joined
