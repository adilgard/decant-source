"""BP25 — on-site hardening: the CODE-fixable findings from the BP24
user-readiness red-team (SANITY_CHECK_FINDINGS.md), each proven by the NEW
behavior. The absence of exactly these tests is why the originals slipped:
every case here is a moment where the system used to actively mislead the
operator (wrong cause named, success claimed after failure, secrets
scrolled away) — so each test pins the honest behavior, not the happy path.

Covers: B1 (offline prereq wall), B2 (print-once RECORDED gate + hold-open
window), B3 (repair preserves the deployed vault root token; hvac auth
failures answer with .env.bak, not a traceback), F1 (sealed vault named as
the cause everywhere), F6 (supervisor failures change the outcome), F13
(ollama restarted after the model-store copy; honest failure text), F14
(the apply ledger: a half-applied home resumes the deploy, never 'start'),
F17 (ingest pre-flight + [FAIL] language), L1/L2/L3 (khctl path hints,
tenant re-ask, silence-is-work notices). Pure logic + fakes throughout —
the wet path stays the Ubuntu replay.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

import knowledge_hub.deploy_apply as da
import knowledge_hub.deploy_cli as dc
import knowledge_hub.deploy_launch as dl
from knowledge_hub.deploy_apply import (
    APPLY_PROGRESS_FILE,
    ApplyContext,
    ApplyError,
    PILOT_PLACEHOLDER_TOKEN,
    confirm_recorded,
    parse_env_file,
    phase_env,
    phase_kit,
    phase_models,
    phase_openbao,
    run_apply,
)
from knowledge_hub.deploy_kit import render_launch_sh, write_ssd_root
from knowledge_hub.deploy_launch import (
    STATE_DEPLOYED,
    STATE_PLANNED,
    StateSignals,
    classify_state,
    gather_signals,
    khctl_hint,
    run_launch,
)
from knowledge_hub.deploy_profiles import DeployPlan

from test_deploy_launch import INFRA_DIR, Console, FakeRunner, launch_config


@pytest.fixture(autouse=True)
def _restore_cwd():
    """run_launch pins CWD to the deployment home (by design); tests must
    not leak that into the rest of the suite."""
    before = os.getcwd()
    yield
    os.chdir(before)


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


def make_plan(tenants=None, seams=None) -> DeployPlan:
    return DeployPlan.from_json(json.dumps({
        "plan_version": "1", "profile": "appliance", "shape": "A",
        "placement": "single_box", "secrets_custody": "operator",
        "custody_overridden": False,
        "seams": seams or {},
        "extraction_tier": "fp16", "extraction_model": "qwen3.6",
        "tenants": tenants or [],
    }))


def make_ctx(tmp_path: Path, *, plan=None, kit_dir=None,
             env_text="POSTGRES_USER=kh\n") -> ApplyContext:
    env_file = tmp_path / ".env.deploy"
    env_file.write_text(env_text, encoding="utf-8")
    return ApplyContext(plan=plan or make_plan(), infra_dir=tmp_path,
                        kit_dir=kit_dir or tmp_path, env_file=env_file)


# ---------------------------------------------------------------------------
# B3 — repair/re-plan must never clobber the deployed vault root token
# ---------------------------------------------------------------------------
def test_phase_env_preserves_deployed_root_token_over_the_placeholder(
        tmp_path):
    """The deceive-the-operator case: a deployed .env carries the REAL root
    token phase_openbao minted; render_env always emits the pilot
    placeholder. The repair path used to copy the placeholder over the real
    token — then every vault call failed and the box presented as mass
    credential revocation."""
    ctx = make_ctx(tmp_path, env_text=(
        f"POSTGRES_USER=kh\nBAO_ROOT_TOKEN={PILOT_PLACEHOLDER_TOKEN}\n"))
    (tmp_path / ".env").write_text(
        "POSTGRES_USER=kh\nBAO_ROOT_TOKEN=s.LIVEDEPLOYTOKEN\n",
        encoding="utf-8")

    lines = phase_env(ctx)

    installed = parse_env_file(tmp_path / ".env")
    assert installed["BAO_ROOT_TOKEN"] == "s.LIVEDEPLOYTOKEN"
    # Later phases (openbao/tenants) read ctx.env — they must get the LIVE
    # token too, or the idempotent branch calls the vault with the stale
    # placeholder and hvac raises Forbidden.
    assert ctx.env["BAO_ROOT_TOKEN"] == "s.LIVEDEPLOYTOKEN"
    assert (tmp_path / ".env.bak").exists()
    assert any("preserved" in line for line in lines)


def test_phase_env_bench_placeholder_over_placeholder_installs_plainly(
        tmp_path):
    ctx = make_ctx(tmp_path, env_text=(
        f"POSTGRES_USER=kh\nBAO_ROOT_TOKEN={PILOT_PLACEHOLDER_TOKEN}\n"))
    (tmp_path / ".env").write_text(
        f"POSTGRES_USER=old\nBAO_ROOT_TOKEN={PILOT_PLACEHOLDER_TOKEN}\n",
        encoding="utf-8")
    lines = phase_env(ctx)
    assert parse_env_file(tmp_path / ".env")["BAO_ROOT_TOKEN"] \
        == PILOT_PLACEHOLDER_TOKEN
    assert not any("preserved" in line for line in lines)


def test_phase_env_dry_run_reports_the_preservation(tmp_path):
    ctx = make_ctx(tmp_path, env_text=(
        f"BAO_ROOT_TOKEN={PILOT_PLACEHOLDER_TOKEN}\n"))
    ctx.dry_run = True
    (tmp_path / ".env").write_text("BAO_ROOT_TOKEN=s.LIVE\n",
                                   encoding="utf-8")
    (lines := phase_env(ctx))
    assert "preserving the deployed vault root token" in lines[0]
    # dry run: nothing changed on disk
    assert parse_env_file(tmp_path / ".env")["BAO_ROOT_TOKEN"] == "s.LIVE"


def test_run_apply_hvac_auth_failure_names_env_bak_not_a_traceback(
        tmp_path, capsys, monkeypatch):
    import hvac.exceptions

    def exploding_phase(_ctx):
        raise hvac.exceptions.Forbidden("permission denied")

    monkeypatch.setattr(da, "PHASES",
                        [("openbao bootstrap", exploding_phase)])
    rc = run_apply(make_ctx(tmp_path))
    out = capsys.readouterr().out
    assert rc == 1
    assert "Traceback" not in out
    assert ".env.bak" in out and "BAO_ROOT_TOKEN" in out
    assert "fix and re-run" in out
    # F14: the ledger recorded where it died.
    progress = json.loads(
        (tmp_path / APPLY_PROGRESS_FILE).read_text(encoding="utf-8"))
    assert progress["completed"] is False
    assert progress["failed_phase"] == "openbao bootstrap"


# ---------------------------------------------------------------------------
# B2 — print-once secrets never scroll away unacknowledged
# ---------------------------------------------------------------------------
# POSTURE (d.s Stage 2): confirm_recorded is a DEPLOYED-posture gate — local
# posture skips it, because the value it protects is recoverable from the local
# secrets file rather than print-once. These tests are about the gate itself, so
# they pin the deployed posture explicitly instead of inheriting the bench's
# (which now defaults to local). The local-posture behavior has its own tests in
# test_posture_ceremony.py; nothing here was weakened to accommodate it.
@pytest.fixture()
def _deployed_posture(monkeypatch):
    from knowledge_hub.config import POSTURE_DEPLOYED, settings
    monkeypatch.setattr(settings, "posture", POSTURE_DEPLOYED)


def test_confirm_recorded_blocks_until_recorded_is_typed(capsys,
                                                         _deployed_posture):
    answers = iter(["nope", "", "  recorded  "])
    confirm_recorded("unseal shares",
                     input_fn=lambda _prompt: next(answers), is_tty=True)
    out = capsys.readouterr().out
    assert out.count("not confirmed") == 2


def test_confirm_recorded_skips_without_a_tty(_deployed_posture):
    confirm_recorded(
        "anything",
        input_fn=lambda _p: pytest.fail("must never prompt off-tty"),
        is_tty=False)


def test_confirm_recorded_survives_a_lying_tty(capsys, _deployed_posture):
    """Windows null-device stdin still reports a console handle: isatty()
    says True, input() raises EOFError. The gate must degrade gracefully,
    never crash the ceremony that just printed the secrets (found live on
    the bench)."""

    def eof(_prompt):
        raise EOFError

    confirm_recorded("credential value", input_fn=eof, is_tty=True)
    assert "stdin closed" in capsys.readouterr().out


def test_print_once_credential_engages_the_recorded_gate(monkeypatch,
                                                         capsys):
    gates: list[str] = []
    monkeypatch.setattr(da, "confirm_recorded",
                        lambda what, **kw: gates.append(what))
    da._print_once_credential("TEST credential", "t", "kh-tok-x", "pid-1")
    assert gates == ["credential value"]
    assert "kh-tok-x" in capsys.readouterr().out


class _FakeBaoSys:
    """Uninitialized-vault fake for phase_openbao's init ceremony."""

    def __init__(self):
        self.submitted: list[str] = []

    def is_initialized(self):
        return False

    def initialize(self, secret_shares, secret_threshold):
        return {"keys_base64": [f"share-{i}" for i in range(secret_shares)],
                "root_token": "s.NEWROOT"}

    def submit_unseal_key(self, key):
        self.submitted.append(key)

    def list_mounted_secrets_engines(self):
        return {"secret/": {}}


class _FakeBaoClient:
    def __init__(self, sys_obj):
        self.sys = sys_obj


def test_phase_openbao_init_pauses_for_the_shares_ceremony(
        tmp_path, monkeypatch, capsys):
    """The 5 shares + root token print ONCE — and the phase now holds for a
    RECORDED acknowledgment before unsealing continues (B2)."""
    import hvac

    fake_sys = _FakeBaoSys()
    monkeypatch.setattr(
        hvac, "Client",
        lambda url=None, token=None: _FakeBaoClient(fake_sys))
    gates: list[str] = []
    monkeypatch.setattr(da, "confirm_recorded",
                        lambda what, **kw: gates.append(what))
    # the BP31 waits have their own tests; this one proves the ceremony
    monkeypatch.setattr(da, "_await_vault_ready",
                        lambda ctx, addr: "vault answering (patched)")
    monkeypatch.setattr(da, "_await_vault_leader",
                        lambda client: "leader elected (patched)")

    (tmp_path / ".env").write_text("BAO_ROOT_TOKEN=stale\n",
                                   encoding="utf-8")
    ctx = make_ctx(tmp_path, plan=make_plan(seams={
        "secrets": {"seam": "secrets", "choice": "ours",
                    "compose_service": "openbao"}}))
    ctx.env = {"BAO_ADDR": "http://fake:8200"}

    lines = phase_openbao(ctx)
    out = capsys.readouterr().out
    assert "unseal share 1/5" in out
    assert gates == ["5 unseal shares + root token"]     # gate AFTER print
    assert fake_sys.submitted == ["share-0", "share-1", "share-2"]
    assert "BAO_ROOT_TOKEN=s.NEWROOT" in \
        (tmp_path / ".env").read_text(encoding="utf-8")
    assert any("initialized" in line for line in lines)


# ---------------------------------------------------------------------------
# B1 + L1 + L3 + B2 — the SSD wrapper: prereq wall, hold-open, "$@", un-quiet
# ---------------------------------------------------------------------------
def test_launch_sh_prereq_wall_fires_before_anything_else():
    script = render_launch_sh()
    for prereq in ("docker", "minisign", "tar", "ollama"):
        assert prereq in script
    assert "PREREQS.txt" in script
    assert "apt-get" not in script       # offline-honest: no impossible fix
    assert script.index("missing prerequisite") \
        < script.index("TRUSTED_PUBKEYS=(")
    # BP46 Fix 3: python must NOT be part of the wall any more — that check
    # is exactly what refused on Ubuntu 26.04 (3.14, no 3.12 available).
    wall = script[script.index('missing=""'):script.index('if [ -n "$missing"')]
    checks = [line for line in wall.splitlines()
              if line.strip() and not line.lstrip().startswith("#")]
    assert not any("python" in line for line in checks), checks


def test_launch_sh_holds_the_window_and_passes_flags_through():
    script = render_launch_sh()
    # B2: the terminal (and the print-once secrets in it) never self-close.
    assert 'read -r -p "press Enter to close "' in script
    assert 'exec "$VENV/bin/khctl"' not in script     # exec dropped the hold
    # L1: recovery flags reach khctl launch; khctl lands on PATH.
    assert '--work-dir "$WORK" "$@"' in script
    assert '.local/bin/khctl' in script
    # L3: the multi-GB pip install prints progress.
    assert "--quiet" not in script


def test_make_ssd_writes_an_offline_honest_prereqs_file(tmp_path):
    lines = write_ssd_root(tmp_path)
    prereqs = tmp_path / "decant.Source" / "PREREQS.txt"
    text = prereqs.read_text(encoding="utf-8")
    # four host packages (BP28 #11: Ollama's installer aborts without zstd).
    # It was five until BP46 Fix 3 carried a portable python into the kit —
    # the host-python requirement is what made 26.04 undeployable.
    for item in ("Docker", "minisign", "zstd", "Ollama"):
        assert item in text
    assert "four host" in text
    assert "python is NOT on that list" in text
    assert "26.04" in text                        # names the OS it unblocks
    # a headless box needs the tunnel endpoint installable up front —
    # and tmux, so a dropped SSH never kills a deploy mid-flight
    assert "openssh-server" in text
    assert "iptables" in text
    assert "tmux" in text
    assert "rsync" in text
    assert "BEFORE going offline" in text
    # the working unseal form — WITH BAO_ADDR (BP28 #17: the CLI defaults
    # to HTTPS, the listener is plain HTTP; without it the command fails)
    assert da.UNSEAL_COMMAND in text
    assert b"\r\n" not in prereqs.read_bytes()
    assert any("PREREQS.txt" in line for line in lines)


# ---------------------------------------------------------------------------
# F13 — the model-store copy restarts ollama and fails honestly
# ---------------------------------------------------------------------------
class _OllamaReport:
    def __init__(self, models, reachable=True, error=None):
        self.models = models
        self.reachable = reachable
        self.error = error


def _models_ctx(tmp_path, monkeypatch, *, kit_store: bool,
                served_models: list[str]):
    import knowledge_hub.deploy_probe as dp

    kit = tmp_path / "kit"
    kit.mkdir(exist_ok=True)
    if kit_store:
        (kit / "ollama_models").mkdir()
        (kit / "ollama_models" / "blob").write_bytes(b"x" * 32)
    home = tmp_path / "fakehome"
    home.mkdir(exist_ok=True)
    monkeypatch.setattr(da.Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(dp, "probe_ollama",
                        lambda host: _OllamaReport(served_models))
    ctx = make_ctx(tmp_path, kit_dir=kit, plan=make_plan(seams={
        "inference": {"seam": "inference", "choice": "local"}}))
    ctx.env = {}
    return ctx, home


def test_phase_models_restarts_ollama_after_the_kit_copy(
        tmp_path, monkeypatch, capsys):
    restarts: list[bool] = []
    monkeypatch.setattr(da, "_restart_ollama",
                        lambda: restarts.append(True) or True)
    ctx, home = _models_ctx(tmp_path, monkeypatch, kit_store=True,
                            served_models=["bge-m3:latest", "qwen3.6:latest"])
    lines = phase_models(ctx)
    assert restarts == [True]                       # the missing restart
    assert any("ollama restarted" in line for line in lines)
    assert (home / ".ollama" / "models" / "blob").exists()
    # L3: the multi-GB copy announces itself BEFORE the silence.
    assert "silence here is work" in capsys.readouterr().out


def test_phase_models_failure_after_copy_says_restart_not_egress(
        tmp_path, monkeypatch):
    monkeypatch.setattr(da, "_restart_ollama", lambda: False)
    ctx, _ = _models_ctx(tmp_path, monkeypatch, kit_store=True,
                         served_models=[])
    with pytest.raises(ApplyError) as err:
        phase_models(ctx)
    message = str(err.value)
    # The OLD message was factually wrong ("no kit ollama_models/") and
    # prescribed an egress-only fix. The new one names the real cause.
    assert "restart" in message
    assert "no kit ollama_models/" not in message
    assert "ollama pull" not in message


def test_phase_models_failure_without_kit_store_is_offline_honest(
        tmp_path, monkeypatch):
    ctx, _ = _models_ctx(tmp_path, monkeypatch, kit_store=False,
                         served_models=[])
    with pytest.raises(ApplyError, match="pre-load"):
        phase_models(ctx)


# ---------------------------------------------------------------------------
# F14 — a half-finished deploy is NOT "deployed"
# ---------------------------------------------------------------------------
def test_classify_state_incomplete_apply_is_planned_not_deployed():
    live = StateSignals(True, True, True, True)
    assert classify_state(live) == STATE_DEPLOYED
    half = StateSignals(True, True, True, True, apply_incomplete=True)
    assert classify_state(half) == STATE_PLANNED


def test_gather_signals_reads_the_apply_ledger(work_dir):
    for name, content in (("probe_report.json", "{}"),
                          ("deploy_plan.json", "{}"),
                          (".env.deploy", ""), (".env", "")):
        (work_dir / name).write_text(content, encoding="utf-8")
    (work_dir / APPLY_PROGRESS_FILE).write_text(
        json.dumps({"completed": False, "failed_phase": "model store"}),
        encoding="utf-8")
    assert gather_signals(work_dir, lambda _e: True).apply_incomplete is True

    (work_dir / APPLY_PROGRESS_FILE).write_text(
        json.dumps({"completed": True, "failed_phase": None}),
        encoding="utf-8")
    assert gather_signals(work_dir, lambda _e: True).apply_incomplete is False


def test_half_applied_home_resumes_the_deploy_not_the_start_menu(
        kit_dir, work_dir):
    """Kill a deploy mid-phase (the ledger records it), re-run the launcher
    with a live-looking stack: the operator gets the guided REPAIR flow and
    the failed phase named — plain Enter can no longer start a box with no
    models or credentials."""
    for name in ("probe_report.json", "deploy_plan.json", ".env.deploy",
                 ".env"):
        (work_dir / name).write_text("{}" if name.endswith(".json") else "",
                                     encoding="utf-8")
    (work_dir / APPLY_PROGRESS_FILE).write_text(
        json.dumps({"completed": False, "failed_phase": "model store"}),
        encoding="utf-8")
    runner = FakeRunner(work_dir)
    console = Console(["",     # probe -> continue
                       "q"])   # plan pause -> stop
    rc = run_launch(launch_config(work_dir, kit_dir, console, runner,
                                  stack_check=lambda _env: True))
    assert rc == 0
    text = console.text()
    assert "did NOT finish" in text and "model store" in text
    assert "A deployment is live on this box" not in text   # no start menu
    assert runner.commands()[:2] == ["verify-kit", "probe"]  # guided flow


def test_completed_apply_ledger_keeps_the_deployed_menu(kit_dir, work_dir):
    for name in ("probe_report.json", "deploy_plan.json", ".env.deploy",
                 ".env"):
        (work_dir / name).write_text("{}" if name.endswith(".json") else "",
                                     encoding="utf-8")
    (work_dir / APPLY_PROGRESS_FILE).write_text(
        json.dumps({"completed": True, "failed_phase": None}),
        encoding="utf-8")
    runner = FakeRunner(work_dir)
    console = Console(["q"])
    rc = run_launch(launch_config(work_dir, kit_dir, console, runner,
                                  stack_check=lambda _env: True))
    assert rc == 0
    assert "A deployment is live on this box" in console.text()


def test_start_program_refuses_when_required_models_are_missing(
        kit_dir, work_dir, monkeypatch):
    """The F14 live check: ollama up but empty (deploy died before 'model
    store') → [FAIL] pointing at repair, never 'program is up'."""
    import knowledge_hub.deploy_probe as dp

    plan = {"plan_version": "1", "profile": "appliance", "shape": "A",
            "placement": "single_box", "secrets_custody": "operator",
            "custody_overridden": False,
            "seams": {"inference": {"seam": "inference", "choice": "local"}},
            "extraction_tier": "fp16", "extraction_model": "qwen3.6",
            "tenants": []}
    (work_dir / "deploy_plan.json").write_text(json.dumps(plan),
                                               encoding="utf-8")
    (work_dir / ".env").write_text("", encoding="utf-8")
    monkeypatch.setattr(dp, "probe_ollama",
                        lambda host: _OllamaReport([]))

    console = Console([])
    cfg = launch_config(work_dir, kit_dir, console, FakeRunner(work_dir),
                        stack_check=lambda _env: True)
    rc = dl.start_program(cfg, kit_dir, work_dir)
    assert rc == 1
    text = console.text()
    assert "not served" in text and "repair" in text
    assert "Where to watch" not in text


# ---------------------------------------------------------------------------
# F6 — supervisor failures change the outcome (no browser onto a dead page)
# ---------------------------------------------------------------------------
def test_start_program_refuses_to_claim_success_when_operator_never_came_up(
        kit_dir, work_dir, monkeypatch):
    plan = {"plan_version": "1", "profile": "appliance", "shape": "A",
            "placement": "single_box", "secrets_custody": "operator",
            "custody_overridden": False, "seams": {},
            "extraction_tier": "fp16", "extraction_model": "qwen3.6",
            "tenants": []}
    (work_dir / "deploy_plan.json").write_text(json.dumps(plan),
                                               encoding="utf-8")
    (work_dir / ".env").write_text("", encoding="utf-8")
    monkeypatch.setattr(dl, "ensure_serving", lambda work, env, say: True)
    monkeypatch.setattr(dl, "ensure_operator", lambda work, env, say: False)

    console = Console([])
    cfg = launch_config(work_dir, kit_dir, console, FakeRunner(work_dir),
                        stack_check=lambda _env: True)
    rc = dl.start_program(cfg, kit_dir, work_dir)
    assert rc == 1
    text = console.text()
    assert "did NOT fully start" in text and "operator console" in text
    assert "operator.log" in text
    assert "Where to watch" not in text          # success never claimed


def test_console_failure_prints_diagnostics_and_never_opens_a_browser(
        tmp_path, capsys, monkeypatch):
    import webbrowser

    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open",
                        lambda url: opened.append(url) or True)
    monkeypatch.setattr(dl, "ensure_operator",
                        lambda work, env, say: False)
    monkeypatch.setattr(dc, "_vault_status", lambda addr: "ok")
    monkeypatch.chdir(tmp_path)

    rc = dc.main(["console", "--work-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 1
    assert opened == []                            # no connection-refused tab
    assert "[FAIL]" in out and "operator.log" in out
    assert "browser opened" not in out


# ---------------------------------------------------------------------------
# F1 — a sealed vault is named as the cause, everywhere
# ---------------------------------------------------------------------------
class _HealthSys:
    def __init__(self, payload=None, exc=None):
        self._payload, self._exc = payload, exc

    def read_health_status(self, method="GET"):
        if self._exc:
            raise self._exc
        return self._payload


def test_resolver_status_distinguishes_sealed_from_ok_from_unreachable():
    from knowledge_hub.choke_point import OpenBaoCredentialResolver

    sealed = OpenBaoCredentialResolver(
        client=_FakeBaoClient(_HealthSys({"sealed": True,
                                          "initialized": True})),
        mount="secret")
    assert sealed.status() == "sealed"
    assert sealed.ping() is False       # health must not say vault:true

    ok = OpenBaoCredentialResolver(
        client=_FakeBaoClient(_HealthSys({"sealed": False})), mount="secret")
    assert ok.status() == "ok" and ok.ping() is True

    down = OpenBaoCredentialResolver(
        client=_FakeBaoClient(_HealthSys(exc=ConnectionError("refused"))),
        mount="secret")
    assert down.status() == "unreachable" and down.ping() is False


def test_operator_health_reports_sealed_distinctly():
    from knowledge_hub.operator_http import OperatorApp

    class SealedResolver:
        def status(self):
            return "sealed"

    class FakeGate:
        def operations(self):
            return []

    class FakeService:
        def ping_postgres(self):
            return True

    app = OperatorApp(FakeGate(), FakeService(), SealedResolver())
    status, body = app.handle("GET", "/v1/health", {}, b"")
    assert status == 503
    assert body["vault"] is False               # sealed = NOT usable
    assert body["vault_status"] == "sealed"     # ...and says why


def test_console_sealed_vault_names_the_real_cause(tmp_path, capsys,
                                                   monkeypatch,
                                                   _deployed_posture):
    # DEPLOYED posture (d.s Stage 3): local posture has no vault, so there is no
    # seal to diagnose and this whole failure class does not exist there. The
    # diagnosis itself is unchanged and still fires wherever a vault is in play.
    monkeypatch.setattr(dc, "_vault_status", lambda addr: "sealed")
    monkeypatch.setattr(
        dl, "ensure_operator",
        lambda *a: pytest.fail("sealed refusal must fire before the door"))
    monkeypatch.chdir(tmp_path)
    rc = dc.main(["console", "--work-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "SEALED" in out
    assert ("docker exec -it -e BAO_ADDR=http://127.0.0.1:8200 "
            "kh-openbao bao operator unseal") in out
    assert "not recognized" not in out


def test_provision_sealed_vault_is_not_a_custody_refusal(tmp_path, capsys,
                                                         monkeypatch,
                                                         _deployed_posture):
    # DEPLOYED posture (d.s Stage 3): in local posture both provision-* commands
    # mint into the local store and never consult a vault, so "sealed" cannot
    # arise. The F1 distinction being tested here is a vault-custody concern.
    monkeypatch.setattr(dc, "_vault_status", lambda addr: "sealed")
    monkeypatch.chdir(tmp_path)
    for command in (["provision-operator", "--tenant", "t"],
                    ["provision-agent", "--tenant", "t"]):
        rc = dc.main([*command, "--work-dir", str(tmp_path)])
        out = capsys.readouterr().out
        assert rc == 1
        assert "SEALED" in out and "custody shares" in out
        assert "gate working" not in out       # the custody message is WRONG here


# ---------------------------------------------------------------------------
# F17 — day-2 `khctl ingest` answers in [FAIL] language, never a traceback
# ---------------------------------------------------------------------------
def test_ingest_preflight_postgres_down_is_an_actionable_fail(monkeypatch):
    from knowledge_hub.config import settings

    monkeypatch.setattr(settings, "postgres_host", "127.0.0.1")
    monkeypatch.setattr(settings, "postgres_port", 9)      # nothing listens
    said: list[str] = []
    assert dl.ingest_preflight(said.append) is False
    assert said and said[0].startswith("[FAIL]")
    assert "postgres" in said[0] and "docker compose up" in said[0]


def test_run_ingest_postgres_down_returns_fail_not_a_traceback(monkeypatch):
    from knowledge_hub.config import settings

    monkeypatch.setattr(settings, "postgres_host", "127.0.0.1")
    monkeypatch.setattr(settings, "postgres_port", 9)
    said: list[str] = []
    rc = dl.run_ingest(["some-tenant"], [], say=said.append)
    assert rc == 1
    assert any(line.startswith("[FAIL]") for line in said)


def test_infra_failure_classifier_names_components_and_ignores_bugs():
    import psycopg

    assert "postgres" in dl._infra_failure(psycopg.OperationalError("gone"))
    assert dl._infra_failure(ValueError("a real bug")) is None  # stays loud


# ---------------------------------------------------------------------------
# L2 — plain Enter can no longer deploy an unloginnable zero-tenant box
# ---------------------------------------------------------------------------
def test_empty_tenant_input_reasks_with_the_recovery_spelled_out(
        kit_dir, work_dir):
    runner = FakeRunner(work_dir)
    console = Console(["",           # probe -> continue
                       "",           # tenant prompt: plain Enter -> re-ask
                       "diversified-botanics",
                       "q"])         # plan pause -> stop
    rc = run_launch(launch_config(work_dir, kit_dir, console, runner,
                                  tenants=None))
    assert rc == 0
    text = console.text()
    assert "NO credentials" in text and "cannot be logged into" in text
    plan_argv = runner.calls[2]
    assert plan_argv[plan_argv.index("--tenants") + 1] \
        == "diversified-botanics"


def test_explicit_none_still_deploys_tenantless(kit_dir, work_dir):
    runner = FakeRunner(work_dir)
    console = Console(["", "none", "q"])
    rc = run_launch(launch_config(work_dir, kit_dir, console, runner,
                                  tenants=None))
    assert rc == 0
    assert "--tenants" not in runner.calls[2]


# ---------------------------------------------------------------------------
# L1 — printed hints are runnable as typed
# ---------------------------------------------------------------------------
def test_khctl_hint_points_at_the_venv_console_script(tmp_path, monkeypatch):
    exe_name = "python.exe" if os.name == "nt" else "python"
    khctl_name = "khctl.exe" if os.name == "nt" else "khctl"
    (tmp_path / exe_name).write_text("", encoding="utf-8")
    (tmp_path / khctl_name).write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "executable", str(tmp_path / exe_name))
    assert khctl_hint() == str(tmp_path / khctl_name)
    (tmp_path / khctl_name).unlink()
    assert khctl_hint() == "khctl"        # honest fallback (symlink covers it)


# ---------------------------------------------------------------------------
# L3 — phase_kit announces the multi-GB hash before the silence
# ---------------------------------------------------------------------------
def test_phase_kit_prints_the_silence_notice_for_big_kits(tmp_path, capsys):
    artifact = tmp_path / "docker-compose.yml"
    artifact.write_text("services: {}\n", encoding="utf-8")
    (tmp_path / "manifest.json").write_text(json.dumps({
        "artifacts": [{"path": "docker-compose.yml",
                       "sha256": da.sha256_stream(artifact),
                       "bytes": 60_000_000_000}]}), encoding="utf-8")
    ctx = make_ctx(tmp_path)
    lines = phase_kit(ctx)
    assert "silence here is work" in capsys.readouterr().out
    assert any("verified docker-compose.yml" in line for line in lines)


# ---------------------------------------------------------------------------
# BP30 (BP28 #19) — s3config.json rendered on-site; phases VERIFY readiness
# ---------------------------------------------------------------------------
class _Result:
    def __init__(self, returncode=0, stderr="", stdout=""):
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout


class _Probe:
    def __init__(self, reachable, error=None):
        self.reachable = reachable
        self.error = error


def _services_ctx(tmp_path, env=None):
    plan = make_plan(seams={"object_store": {
        "seam": "object_store", "choice": "ours",
        "compose_service": "seaweedfs"}})
    ctx = make_ctx(tmp_path, plan=plan)
    ctx.env = env or {"S3_ACCESS_KEY": "AK123", "S3_SECRET_KEY": "SK456",
                      "S3_ENDPOINT": "http://localhost:8333"}
    return ctx


def test_phase_services_renders_s3config_from_env_before_up(
        tmp_path, monkeypatch):
    import knowledge_hub.deploy_probe as dp

    calls = []

    def fake_compose(ctx, *args):
        calls.append((args, (ctx.infra_dir / "seaweedfs"
                             / "s3config.json").is_file()))
        return _Result()

    monkeypatch.setattr(da, "_compose", fake_compose)
    monkeypatch.setattr(da, "_docker_inspect", lambda c: (0, "created"))
    monkeypatch.setattr(dp, "probe_object_store",
                        lambda *a, **k: _Probe(True))
    ctx = _services_ctx(tmp_path)

    lines = da.phase_services(ctx)

    config = json.loads((tmp_path / "seaweedfs" / "s3config.json")
                        .read_text(encoding="utf-8"))
    assert config["identities"][0]["credentials"][0] == {
        "accessKey": "AK123", "secretKey": "SK456"}
    ups = [c for c in calls if c[0][0] == "up"]
    assert ups and ups[0][1] is True   # the FILE existed when compose up ran
    assert any("ready" in line for line in lines)


def test_phase_services_never_passes_build(tmp_path, monkeypatch):
    import knowledge_hub.deploy_probe as dp

    argvs = []
    monkeypatch.setattr(da, "_compose",
                        lambda ctx, *args: argvs.append(args) or _Result())
    monkeypatch.setattr(da, "_docker_inspect", lambda c: (0, "created"))
    monkeypatch.setattr(dp, "probe_object_store",
                        lambda *a, **k: _Probe(True))
    da.phase_services(_services_ctx(tmp_path))
    up = next(a for a in argvs if a[0] == "up")
    assert "--build" not in up


def test_phase_services_replaces_a_directory_shaped_s3config(
        tmp_path, monkeypatch):
    import knowledge_hub.deploy_probe as dp

    (tmp_path / "seaweedfs" / "s3config.json").mkdir(parents=True)
    monkeypatch.setattr(da, "_compose", lambda ctx, *args: _Result())
    monkeypatch.setattr(da, "_docker_inspect", lambda c: (0, "created"))
    monkeypatch.setattr(dp, "probe_object_store",
                        lambda *a, **k: _Probe(True))

    lines = da.phase_services(_services_ctx(tmp_path))

    assert (tmp_path / "seaweedfs" / "s3config.json").is_file()
    assert any("DIRECTORY" in line for line in lines)


def test_phase_services_gate_fails_loudly_when_a_service_never_answers(
        tmp_path, monkeypatch):
    import knowledge_hub.deploy_probe as dp

    monkeypatch.setattr(da, "_compose", lambda ctx, *args: _Result())
    monkeypatch.setattr(da, "_docker_inspect", lambda c: (0, "running"))
    monkeypatch.setattr(dp, "probe_object_store",
                        lambda *a, **k: _Probe(False, "connect refused"))
    monkeypatch.setattr(da, "SERVICE_READY_TIMEOUT_S", 0)

    with pytest.raises(ApplyError) as err:
        da.phase_services(_services_ctx(tmp_path))

    message = str(err.value)
    assert "seaweedfs" in message
    assert "kh-seaweedfs" in message
    assert "docker logs" in message
    assert "connect refused" in message


def test_phase_services_gate_trips_on_a_restart_loop_early(
        tmp_path, monkeypatch):
    import knowledge_hub.deploy_probe as dp

    # generous default deadline: the RestartCount tripwire must fire FIRST
    monkeypatch.setattr(da, "_compose", lambda ctx, *args: _Result())
    monkeypatch.setattr(da, "_docker_inspect", lambda c: (7, "restarting"))
    monkeypatch.setattr(dp, "probe_object_store",
                        lambda *a, **k: _Probe(False, "down"))

    with pytest.raises(ApplyError, match="restart-looping"):
        da.phase_services(_services_ctx(tmp_path))


# ---------------------------------------------------------------------------
# BP30 (BP28 #21) — deployed S3 creds survive a re-plan's fresh mint
# ---------------------------------------------------------------------------
def test_phase_env_preserves_live_s3_credentials(tmp_path):
    ctx = make_ctx(tmp_path, env_text=(
        "S3_ACCESS_KEY=kh-s3-freshmint\nS3_SECRET_KEY=freshsecret\n"))
    (tmp_path / ".env").write_text(
        "S3_ACCESS_KEY=kh-s3-live\nS3_SECRET_KEY=livesecret\n",
        encoding="utf-8")

    lines = phase_env(ctx)

    installed = parse_env_file(tmp_path / ".env")
    assert installed["S3_ACCESS_KEY"] == "kh-s3-live"
    assert installed["S3_SECRET_KEY"] == "livesecret"
    # later phases (services renders s3config from ctx.env) get the live pair
    assert ctx.env["S3_ACCESS_KEY"] == "kh-s3-live"
    assert ctx.env["S3_SECRET_KEY"] == "livesecret"
    assert any("S3 credentials preserved" in line for line in lines)


def test_phase_services_s3config_carries_the_preserved_pair(tmp_path):
    ctx = make_ctx(tmp_path, env_text=(
        "S3_ACCESS_KEY=kh-s3-freshmint\nS3_SECRET_KEY=freshsecret\n"))
    (tmp_path / ".env").write_text(
        "S3_ACCESS_KEY=kh-s3-live\nS3_SECRET_KEY=livesecret\n",
        encoding="utf-8")
    phase_env(ctx)

    da.render_s3_config(ctx)

    config = json.loads((tmp_path / "seaweedfs" / "s3config.json")
                        .read_text(encoding="utf-8"))
    assert config["identities"][0]["credentials"][0] == {
        "accessKey": "kh-s3-live", "secretKey": "livesecret"}


# ---------------------------------------------------------------------------
# BP30 (BP28 #18) — the model store lands where THIS box's ollama reads it
# ---------------------------------------------------------------------------
def test_phase_models_systemd_store_routes_through_root_install(
        tmp_path, monkeypatch):
    store = tmp_path / "sysstore"
    installs = []
    monkeypatch.setattr(da, "_ollama_store_target", lambda: (store, True))
    monkeypatch.setattr(da, "_install_store_as_root",
                        lambda kit, target: installs.append((kit, target)))
    monkeypatch.setattr(da, "_restart_ollama", lambda: True)
    ctx, _ = _models_ctx(tmp_path, monkeypatch, kit_store=True,
                         served_models=["bge-m3:latest", "qwen3.6:latest"])

    lines = phase_models(ctx)

    assert installs == [(ctx.kit_dir / "ollama_models", store)]
    assert any(str(store) in line for line in lines)


def test_install_store_as_root_uses_cp_a_n_and_chowns(tmp_path, monkeypatch):
    recorded = []
    monkeypatch.setattr(da, "_ensure_root_session", lambda: True)
    monkeypatch.setattr(da.subprocess, "run",
                        lambda argv, **kw: recorded.append(argv) or _Result())
    kit_store = tmp_path / "kit" / "ollama_models"
    kit_store.mkdir(parents=True)
    target = tmp_path / "usr_share" / ".ollama" / "models"

    da._install_store_as_root(kit_store, target)

    assert recorded[0][:3] == ["sudo", "-n", "mkdir"]
    cp = recorded[1]
    assert "cp" in cp and "-a" in cp and "-n" in cp    # never cp -al
    assert not any(arg == "-l" for arg in cp)
    chown = recorded[2]
    assert "chown" in chown and "ollama:ollama" in chown
    assert str(target.parent) in chown                 # .ollama, not models


def test_install_store_as_root_without_sudo_prints_the_manual_path(
        tmp_path, monkeypatch):
    monkeypatch.setattr(da, "_ensure_root_session", lambda: False)
    kit_store = tmp_path / "ollama_models"
    kit_store.mkdir()
    with pytest.raises(ApplyError) as err:
        da._install_store_as_root(
            kit_store, Path("/usr/share/ollama/.ollama/models"))
    message = str(err.value)
    assert "chown -R ollama:ollama" in message
    assert "cp -a " in message and "cp -al" not in message


def test_phase_models_skips_a_present_store_without_sudo(
        tmp_path, monkeypatch):
    store = tmp_path / "sysstore"
    monkeypatch.setattr(da, "_ollama_store_target", lambda: (store, True))
    monkeypatch.setattr(
        da, "_install_store_as_root",
        lambda kit, target: pytest.fail("present store must not re-copy"))
    restarts = []
    monkeypatch.setattr(da, "_restart_ollama",
                        lambda: restarts.append(True) or True)
    ctx, _ = _models_ctx(tmp_path, monkeypatch, kit_store=True,
                         served_models=["bge-m3:latest", "qwen3.6:latest"])
    store.mkdir(parents=True)
    (store / "blob").write_bytes(b"y" * 32)   # same name + size as the kit's

    lines = phase_models(ctx)

    assert any("already present" in line for line in lines)
    assert restarts == []                     # no copy -> no restart needed


def test_phase_models_partial_store_still_copies(tmp_path, monkeypatch):
    ctx, home = _models_ctx(tmp_path, monkeypatch, kit_store=True,
                            served_models=["bge-m3:latest", "qwen3.6:latest"])
    target = home / ".ollama" / "models"
    monkeypatch.setattr(da, "_ollama_store_target", lambda: (target, False))
    monkeypatch.setattr(da, "_restart_ollama", lambda: True)
    kit_store = ctx.kit_dir / "ollama_models"
    (kit_store / "manifests").mkdir()
    (kit_store / "manifests" / "m1").write_bytes(b"a" * 8)
    target.mkdir(parents=True)
    (target / "blob").write_bytes(b"x" * 32)  # one present, one missing

    phase_models(ctx)

    assert (target / "manifests" / "m1").exists()


# ---------------------------------------------------------------------------
# BP31 (BP28 #13) — the raft volume is writable by the vault on a CLEAN box
# ---------------------------------------------------------------------------
def test_prod_bao_override_chowns_the_raft_path_before_handoff():
    """A fresh named volume mounts at /openbao/data root-owned; the vault
    user cannot write vault.db and the container crash-loops forever
    (BP28 #13 — first execution of the production vault path ever). The
    override must start as root, fix ownership, and hand off to the stock
    entrypoint (which itself drops privileges to the openbao user)."""
    text = (INFRA_DIR / "docker-compose.openbao-prod.yml") \
        .read_text(encoding="utf-8")
    assert 'user: "0:0"' in text
    assert "chown -R openbao:openbao /openbao/data" in text
    # hand-off preserves the image's own privilege drop — the vault
    # PROCESS never runs as root
    assert "exec /usr/local/bin/docker-entrypoint.sh" in text
    # $$ so compose passes a literal $@ to the shell, not an interpolation
    assert '"$$@"' in text
    assert "server -config=/etc/openbao/config.hcl" in text
    assert "kh_bao_data:/openbao/data" in text


# ---------------------------------------------------------------------------
# BP31 (BP28 #12) — the vault gets a real readiness wait; connection errors
# become "waiting for vault…" retries, never bare tracebacks
# ---------------------------------------------------------------------------
def _secrets_report(reachable, error=None, initialized=None, sealed=None):
    from knowledge_hub.deploy_probe import SecretsReport
    return SecretsReport(addr="http://fake:8200", reachable=reachable,
                         initialized=initialized, sealed=sealed, error=error)


def test_await_vault_ready_retries_then_reports(tmp_path, monkeypatch,
                                                capsys):
    import knowledge_hub.deploy_probe as dp

    answers = iter([_secrets_report(False, "ConnectionError: refused"),
                    _secrets_report(False, "ConnectionError: refused"),
                    _secrets_report(True, initialized=True, sealed=True)])
    monkeypatch.setattr(dp, "probe_secrets", lambda addr: next(answers))
    monkeypatch.setattr(da, "_docker_inspect", lambda c: (0, "running"))
    monkeypatch.setattr(da.time, "sleep", lambda s: None)

    line = da._await_vault_ready(make_ctx(tmp_path), "http://fake:8200")

    assert "vault answering" in line and "sealed=True" in line
    assert "waiting for vault" in capsys.readouterr().out


def test_await_vault_ready_timeout_is_an_apply_error_not_a_traceback(
        tmp_path, monkeypatch):
    import knowledge_hub.deploy_probe as dp

    monkeypatch.setattr(dp, "probe_secrets", lambda addr: _secrets_report(
        False, "ConnectionError: refused"))
    monkeypatch.setattr(da, "_docker_inspect", lambda c: (0, "running"))
    monkeypatch.setattr(da, "VAULT_READY_TIMEOUT_S", 0)

    with pytest.raises(ApplyError) as err:
        da._await_vault_ready(make_ctx(tmp_path), "http://fake:8200")
    message = str(err.value)
    assert "docker logs kh-openbao" in message
    assert "ConnectionError" in message          # the probe's reason, named


def test_await_vault_ready_trips_on_a_crash_loop_early(tmp_path,
                                                       monkeypatch):
    import knowledge_hub.deploy_probe as dp

    monkeypatch.setattr(dp, "probe_secrets",
                        lambda addr: _secrets_report(False, "down"))
    monkeypatch.setattr(da, "_docker_inspect", lambda c: (5, "restarting"))

    with pytest.raises(ApplyError, match="restart-looping"):
        da._await_vault_ready(make_ctx(tmp_path), "http://fake:8200")


# ---------------------------------------------------------------------------
# BP31 (BP28 #16) — no write before raft elects a leader
# ---------------------------------------------------------------------------
class _RecordingBaoSys:
    """Initialized + unsealed vault whose call ORDER is the assertion."""

    def __init__(self, leader_after=0):
        self.calls: list[str] = []
        self._leader_after = leader_after

    def is_initialized(self):
        self.calls.append("is_initialized")
        return True

    def is_sealed(self):
        self.calls.append("is_sealed")
        return False

    def read_leader_status(self):
        self.calls.append("read_leader_status")
        if self.calls.count("read_leader_status") <= self._leader_after:
            return {"ha_enabled": True, "is_self": False,
                    "leader_address": ""}
        return {"ha_enabled": True, "is_self": True,
                "leader_address": "http://127.0.0.1:8200"}

    def list_mounted_secrets_engines(self):
        self.calls.append("list_mounts")
        return {}

    def enable_secrets_engine(self, *args, **kwargs):
        self.calls.append("enable_kv")


def test_phase_openbao_writes_only_after_a_raft_leader_exists(
        tmp_path, monkeypatch):
    import hvac

    fake_sys = _RecordingBaoSys(leader_after=2)
    monkeypatch.setattr(
        hvac, "Client",
        lambda url=None, token=None: _FakeBaoClient(fake_sys))
    monkeypatch.setattr(da, "_await_vault_ready",
                        lambda ctx, addr: "vault answering (patched)")
    monkeypatch.setattr(da.time, "sleep", lambda s: None)
    ctx = make_ctx(tmp_path, plan=make_plan(seams={
        "secrets": {"seam": "secrets", "choice": "ours",
                    "compose_service": "openbao"}}))
    ctx.env = {"BAO_ADDR": "http://fake:8200", "BAO_ROOT_TOKEN": "s.LIVE"}

    lines = phase_openbao(ctx)

    calls = fake_sys.calls
    assert calls.count("read_leader_status") == 3    # two not-ready polls
    assert calls.index("list_mounts") > calls.index("read_leader_status")
    assert calls.index("enable_kv") > calls.index("read_leader_status")
    assert any("leader elected" in line for line in lines)


def test_await_vault_leader_timeout_says_wait_never_token(monkeypatch):
    class _NeverLeaderSys:
        def read_leader_status(self):
            import hvac.exceptions
            raise hvac.exceptions.InternalServerError("local node not active")

    monkeypatch.setattr(da, "VAULT_LEADER_TIMEOUT_S", 0)
    with pytest.raises(ApplyError) as err:
        da._await_vault_leader(_FakeBaoClient(_NeverLeaderSys()))
    message = str(err.value)
    assert "leader" in message
    assert "NOT a token problem" in message
    assert ".env.bak" not in message


# ---------------------------------------------------------------------------
# BP31 (BP28 #15) — error classes get their own truth; NO code path may
# advise an action that destroys a working root token
# ---------------------------------------------------------------------------
def _apply_with(tmp_path, exc, monkeypatch, capsys, live_token="s.LIVE"):
    """run_apply against a single phase raising `exc`; returns stdout.
    A live .env rides along to prove no handler ever rewrites it."""
    monkeypatch.setattr(da, "PHASES",
                        [("openbao bootstrap", lambda _ctx: (_ for _ in ())
                          .throw(exc))])
    (tmp_path / ".env").write_text(f"BAO_ROOT_TOKEN={live_token}\n",
                                   encoding="utf-8")
    rc = run_apply(make_ctx(tmp_path))
    assert rc == 1
    assert parse_env_file(tmp_path / ".env")["BAO_ROOT_TOKEN"] \
        == live_token                       # the handler never writes .env
    out = capsys.readouterr().out
    assert "Traceback" not in out
    return out


def test_connection_error_says_wait_and_retry_never_env_bak(
        tmp_path, monkeypatch, capsys):
    """The BP28 defect: requests.exceptions.ConnectionError is not an hvac
    type, so it sailed past the friendly handler into a bare traceback."""
    import requests.exceptions

    out = _apply_with(tmp_path, requests.exceptions.ConnectionError(
        "connection refused"), monkeypatch, capsys)
    assert "not answering" in out and "re-run" in out
    assert ".env.bak" not in out
    assert "do not change BAO_ROOT_TOKEN" in out


def test_leader_election_500_says_wait_never_a_token_restore(
        tmp_path, monkeypatch, capsys):
    """THE vault-bricking scenario, observed live in BP28: a raft
    leader-election 500 used to be reported as a token mismatch with
    'restore from .env.bak' advice — which replaces the freshly-minted
    token with the pilot placeholder, unrecoverably."""
    import hvac.exceptions

    out = _apply_with(tmp_path, hvac.exceptions.InternalServerError(
        "local node not active but active cluster node not found"),
        monkeypatch, capsys)
    assert "electing a leader" in out and "Wait" in out
    assert ".env.bak" not in out and "restore" not in out
    assert "do not change BAO_ROOT_TOKEN" in out


def test_sealed_503_says_unseal_with_the_working_command(
        tmp_path, monkeypatch, capsys):
    import hvac.exceptions

    out = _apply_with(tmp_path, hvac.exceptions.VaultDown("sealed"),
                      monkeypatch, capsys)
    assert "SEALED" in out
    assert da.UNSEAL_COMMAND in out              # the BAO_ADDR form
    assert ".env.bak" not in out
    assert "custody model working" in out        # named as routine, not loss


def test_rejected_token_with_placeholder_bak_forbids_the_restore(
        tmp_path, monkeypatch, capsys):
    """Even for a GENUINELY rejected token, .env.bak is only named after
    checking what it holds — restoring the pilot placeholder bricks the
    vault, so a placeholder bak gets an explicit DO NOT."""
    import hvac.exceptions

    (tmp_path / ".env.bak").write_text(
        f"BAO_ROOT_TOKEN={PILOT_PLACEHOLDER_TOKEN}\n", encoding="utf-8")
    out = _apply_with(tmp_path, hvac.exceptions.Forbidden(
        "permission denied"), monkeypatch, capsys)
    assert "REJECTED" in out
    assert "Do NOT restore .env.bak" in out
    assert "pilot placeholder" in out
    assert "custody records" in out


def test_rejected_token_with_a_real_bak_demands_verification_first(
        tmp_path, monkeypatch, capsys):
    import hvac.exceptions

    (tmp_path / ".env.bak").write_text("BAO_ROOT_TOKEN=s.OLDREAL\n",
                                       encoding="utf-8")
    out = _apply_with(tmp_path, hvac.exceptions.Forbidden(
        "permission denied"), monkeypatch, capsys)
    assert "VERIFY it against custody records" in out
    assert "never overwrite a token that still works" in out


def test_non_vault_bugs_stay_loud(tmp_path, monkeypatch):
    monkeypatch.setattr(da, "PHASES",
                        [("schema", lambda _ctx: (_ for _ in ())
                          .throw(ValueError("a real bug")))])
    with pytest.raises(ValueError, match="a real bug"):
        run_apply(make_ctx(tmp_path))


# ---------------------------------------------------------------------------
# BP31 (BP28 #17) — every printed unseal command is the form that WORKS
# ---------------------------------------------------------------------------
def test_unseal_command_carries_bao_addr():
    """The `bao` CLI defaults to HTTPS; the production listener is plain
    HTTP. Without BAO_ADDR the printed post-reboot recovery command fails
    with 'server gave HTTP response to HTTPS client' — in front of an
    operator whose stack is DOWN."""
    assert "-e BAO_ADDR=http://127.0.0.1:8200" in da.UNSEAL_COMMAND
    assert da.UNSEAL_COMMAND.endswith("kh-openbao bao operator unseal")


def test_console_sealed_message_carries_bao_addr():
    app_js = (Path(da.__file__).parent / "operator_ui" / "app.js") \
        .read_text(encoding="utf-8")
    sealed_msg = next(line for line in app_js.splitlines()
                      if "vault is SEALED" in line)
    assert "-e BAO_ADDR=http://127.0.0.1:8200" in sealed_msg
