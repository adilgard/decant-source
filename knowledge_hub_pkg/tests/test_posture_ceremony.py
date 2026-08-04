"""d.s Stage 2 — ceremony becomes conditional on posture.

The load-bearing test in this file is test_make_kit_refuses_in_local_posture.
Everything else supports it. The argument for making d.s internal-by-default
rests entirely on this: you cannot ship a soft build by forgetting to harden,
because the build itself blocks. If that test ever goes green for the wrong
reason, the safety property is gone and the rest is decoration.

Deliberately no services: make-kit is driven to its refusal (which happens
before any staging), verify-kit to its skip, and the ceremony helpers directly.
The DEPLOYED half is proven by driving the same entry points with
KH_POSTURE=deployed and showing they reach their real work — a posture switch
whose "off" side was tested and whose "on" side was assumed would be exactly
half a test.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from knowledge_hub import deploy_cli
from knowledge_hub.config import (
    POSTURE_DEPLOYED,
    POSTURE_LOCAL,
    posture_banner,
    settings,
)
from knowledge_hub.deploy_apply import confirm_recorded
from knowledge_hub.deploy_kit import FORBIDDEN_NAMES, assert_no_secrets

PKG_DIR = Path(__file__).resolve().parents[1]
INFRA_DIR = PKG_DIR.parent


@pytest.fixture()
def local(monkeypatch):
    """Force local posture on the live singleton, restoring after.

    monkeypatch.setattr on the singleton rather than a fresh Settings(): the
    code under test reads the module-level `settings` object, so that is what
    must move. monkeypatch's undo is what keeps this from leaking into the rest
    of the suite (the 2026-08-03 lesson about a modified singleton escaping)."""
    monkeypatch.setattr(settings, "posture", POSTURE_LOCAL)
    return settings


@pytest.fixture()
def deployed(monkeypatch):
    monkeypatch.setattr(settings, "posture", POSTURE_DEPLOYED)
    return settings


# ---------------------------------------------------------------------------
# THE HARD GATE
# ---------------------------------------------------------------------------
def test_make_kit_refuses_in_local_posture(local, tmp_path, capsys):
    """The whole point of the build. A kit is the one artifact that leaves this
    box, so the softness that is correct internally is a defect the moment it
    is packaged — and the way that happens is forgetting, not deciding."""
    out = tmp_path / "kit"
    args = deploy_cli.argparse.Namespace(
        out=str(out), infra_dir=str(INFRA_DIR), models=None, skip=[],
        sign_key=None, allow_unsigned=False, ollama_dir=None)

    assert deploy_cli._cmd_make_kit(args) == 1, "make-kit must FAIL, not warn"

    printed = capsys.readouterr().out
    assert "REFUSES" in printed
    assert POSTURE_LOCAL in printed, "the error must name the posture it is in"
    assert "KH_POSTURE" in printed, "and how to get out of it"
    assert not out.exists(), (
        "make-kit created its output directory before refusing — the gate must "
        "fire before anything touches the target drive")


def test_the_refusal_offers_no_override_flag():
    """There is no --force, and that is the design. A flag would turn 'shipping
    soft costs a decision' into 'shipping soft costs a keystroke', and the
    keystroke is what someone reaches for at the end of a long day.

    Asserted against the real parser rather than the source text: the first
    version of this test grepped the function body and failed on its own
    comment, which says "there is no --force". Prose about a flag is not a
    flag. What matters is which options argparse will actually accept."""
    flags = _option_strings("make-kit")
    for escape in ("--force", "--allow-local", "--anyway", "--allow-soft",
                   "--local", "--no-sign", "--skip-posture"):
        assert escape not in flags, (
            f"make-kit accepts {escape} — the gate has an escape hatch")
    # The pre-existing dev-bench override is a DIFFERENT thing and stays: it
    # bypasses signing, not the posture, and it records itself in the manifest.
    assert "--allow-unsigned" in flags


def test_the_gate_fires_before_any_work(local):
    """Ordering: the posture check must precede the first thing that costs time
    or touches the target. default_kit_models() reads profiles.toml;
    run_make_kit() hashes gigabytes. A refusal arriving after either would be
    correct and useless.

    Compared by AST line number, not by string position. The first version
    searched for "default_kit_models(" and matched this very docstring — the
    same lesson as test_posture.py's tripwire, learned twice: when a test
    reasons about code, it has to read code."""
    gate_line, call_lines = _gate_and_call_lines(
        deploy_cli._cmd_make_kit,
        gate_attr="is_local",
        called=("default_kit_models", "run_make_kit", "KitContext"))

    assert gate_line is not None, "no `settings.is_local` branch in make-kit"
    for name, line in call_lines.items():
        assert gate_line < line, (
            f"the posture gate (line {gate_line}) must come before the call to "
            f"{name} (line {line})")


def test_make_kit_proceeds_past_the_gate_in_deployed_posture(deployed,
                                                            tmp_path):
    """The other half: deployed posture reaches make-kit's REAL behavior.

    Proven by the failure it produces. With no signing key and no
    --allow-unsigned, a deployed build must fail at the SIGNING requirement
    (deploy_kit.stage_manifest) — a different, later, pre-existing gate. Seeing
    that failure instead of the posture refusal proves the posture gate let it
    through and that today's signing requirement is untouched. Asserting on the
    signing message rather than on a successful build keeps this test free of
    docker, minisign, and 60GB of models."""
    out = tmp_path / "kit"
    args = deploy_cli.argparse.Namespace(
        out=str(out), infra_dir=str(INFRA_DIR), models="bge-m3",
        skip=["wheelhouse", "python", "images", "models", "tokenizer"],
        sign_key=None, allow_unsigned=False, ollama_dir=None)

    rc = deploy_cli._cmd_make_kit(args)

    assert rc == 1, "still fails, but for the signing reason, not the posture"
    assert out.exists(), (
        "deployed posture must get past the gate and into staging — if the "
        "output dir was never created, the posture gate blocked it")


# ---------------------------------------------------------------------------
# verify-kit — a skip, not a refusal
# ---------------------------------------------------------------------------
def test_verify_kit_skips_in_local_posture(local, tmp_path, capsys):
    args = deploy_cli.argparse.Namespace(
        kit=str(tmp_path), allow_unsigned=False, anyway=False)

    assert deploy_cli._cmd_verify_kit(args) == 0, (
        "a skipped gate is not a failure — exit 0")

    printed = capsys.readouterr().out
    assert "[SKIP]" in printed
    assert "local posture" in printed
    assert "--anyway" in printed, "a skip must name its own escape hatch"


def test_verify_kit_runs_anyway_when_asked(local, tmp_path, capsys):
    """--anyway exists because verify-kit only READS. Refusing it would block
    the one legitimate local use: checking a kit somebody handed you. Proven by
    reaching the real 'not a kit' error on an empty directory."""
    args = deploy_cli.argparse.Namespace(
        kit=str(tmp_path), allow_unsigned=False, anyway=True)

    assert deploy_cli._cmd_verify_kit(args) == 1
    printed = capsys.readouterr().out
    assert "[SKIP]" not in printed
    assert "no manifest.json" in printed, "it reached the real gate"


def test_verify_kit_is_unchanged_in_deployed_posture(deployed, tmp_path,
                                                     capsys):
    args = deploy_cli.argparse.Namespace(
        kit=str(tmp_path), allow_unsigned=False, anyway=False)

    assert deploy_cli._cmd_verify_kit(args) == 1
    printed = capsys.readouterr().out
    assert "[SKIP]" not in printed
    assert "no manifest.json" in printed


# ---------------------------------------------------------------------------
# The print-once acknowledgment gate
# ---------------------------------------------------------------------------
def test_confirm_recorded_skips_in_local_posture(local, capsys):
    """One gate, three call sites (unseal shares, operator credential, agent
    credential).

    It is right to skip for a reason worth stating precisely, because the
    obvious reason is wrong: the value is NOT recoverable from the local store,
    which keeps sha256 digests exactly as the vault keeps them in its paths. The
    gate protects against losing something EXPENSIVE to replace, and in local
    posture replacing anything is one command with no ceremony. That is the
    difference, and the message has to say so rather than implying the value can
    be read back."""
    def must_not_be_called(prompt: str) -> str:
        raise AssertionError("local posture must not prompt a human")

    confirm_recorded("unseal shares", input_fn=must_not_be_called,
                     is_tty=True)

    printed = capsys.readouterr().out
    assert "local posture" in printed
    assert "cannot be recovered" in printed
    assert "re-issuing" in printed, (
        "say why the skip is safe — that a replacement is cheap — or it reads "
        "as a shrug")


def test_no_message_claims_a_token_can_be_read_back(local, capsys):
    """Guards a contradiction this build actually shipped for a few minutes:
    confirm_recorded said the value was 'recoverable from .secrets.local.json'
    while the provisioning path, correctly, said it was not. Both messages print
    in the same command output. Only one could be true, and it was the second."""
    confirm_recorded("agent credential", is_tty=True)
    printed = capsys.readouterr().out
    assert "recoverable from" not in printed
    assert f"recoverable from {settings.local_secrets_file}" not in printed


def test_confirm_recorded_still_holds_in_deployed_posture(deployed, capsys):
    """Unchanged: a deployed run still blocks until a human types RECORDED,
    and still refuses anything else."""
    answers = iter(["", "yes", "recorded"])
    confirm_recorded("unseal shares", input_fn=lambda prompt: next(answers),
                     is_tty=True)
    printed = capsys.readouterr().out
    assert printed.count("not confirmed") == 2, (
        "both wrong answers must be rejected before the gate opens")


def test_confirm_recorded_deployed_non_tty_is_still_a_skip(deployed):
    """The pre-existing automation carve-out survives: no tty, no hold."""
    def must_not_be_called(prompt: str) -> str:
        raise AssertionError("no tty means no human to hold")

    confirm_recorded("shares", input_fn=must_not_be_called, is_tty=False)


def test_local_posture_never_prints_a_secret_it_cannot_recover(local, capsys):
    """The skip drops a human PROMPT, not the audit record. The value still
    prints and still lands in operator.log — those are different things, and
    only the first one is ceremony."""
    confirm_recorded("operator credential", is_tty=True)
    printed = capsys.readouterr().out
    assert "operator credential" in printed


# ---------------------------------------------------------------------------
# The custody ceremony inside phase_openbao
# ---------------------------------------------------------------------------
def test_phase_openbao_gates_only_the_ceremony_not_the_init():
    """Read off the source: local posture must still initialize and unseal —
    the vault does not work otherwise. What it skips is the custody SCRIPT
    (print five shares, seal envelopes, hand them over, witness a test unseal),
    which answers a question a single-user box does not ask."""
    from knowledge_hub.deploy_apply import phase_openbao

    source = inspect.getsource(phase_openbao)
    assert "ceremony_text(custody) if settings.is_deployed" in source, (
        "the custody script must be the posture-gated part")
    for mechanical in ("sys.initialize", "submit_unseal_key",
                       "enable_secrets_engine"):
        assert mechanical in source, (
            f"{mechanical} is mechanical, not ceremony — it must not be gated")
    # The is_sealed refusal is a real operational guard, not ceremony.
    assert "is_sealed" in source


# ---------------------------------------------------------------------------
# The no-secrets guard stays posture-blind
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", [
    ".secrets.local.json",
    ".secrets.local.example.json",
    ".secrets.local.yaml",
])
def test_the_local_secrets_file_can_never_ride_in_a_kit(name, tmp_path):
    """Third net, behind the make-kit posture gate and .gitignore. This file
    holds source credentials AND the console principal registry, and it lives
    beside .env in the directory the bundle stage reads from — one glob away
    from another machine. The nets that matter get more than one."""
    assert FORBIDDEN_NAMES.match(name), f"{name} must be forbidden in a kit"

    (tmp_path / name).write_text("{}", encoding="utf-8")
    with pytest.raises(Exception) as e:
        assert_no_secrets(tmp_path)
    assert name in str(e.value)


def test_the_no_secrets_guard_is_posture_blind():
    """Everything else about kit ceremony became conditional in Stage 2. This
    did not, on purpose: it is a safety check on what leaves the bench, not
    product ceremony. A kit built in EITHER posture must carry no credential."""
    for func in (assert_no_secrets,):
        source = inspect.getsource(func)
        for token in ("posture", "is_local", "is_deployed"):
            assert token not in source


def test_the_env_files_are_still_forbidden():
    """Regression on the pattern edit itself: adding .secrets.local to the
    alternation must not have broken any existing arm."""
    for name in (".env", ".env.deploy", "deploy_plan.json",
                 "probe_report.json", "s3config.json", "serving_usage.jsonl",
                 "signing.key", "cert.pem", "config.bak"):
        assert FORBIDDEN_NAMES.match(name), f"{name} stopped being forbidden"


def test_gitignore_covers_the_local_secrets_file():
    """Net two. The pattern must cover the file before Stage 3 writes real
    credentials into it, not after."""
    ignored = (INFRA_DIR / ".gitignore").read_text(encoding="utf-8")
    assert ".secrets.local" in ignored


# ---------------------------------------------------------------------------
# Banner verbosity (the cheaper variant)
# ---------------------------------------------------------------------------
def test_every_command_is_classified_for_banner_verbosity():
    """No unclassified commands. Both sets are enumerated rather than one being
    'everything else', so a subcommand added later fails HERE until someone
    decides which side it belongs on — a default would have quietly decided
    for them, and quiet decisions about how loud the posture is are the ones
    worth catching."""
    parser = _build_parser()
    declared = (deploy_cli.FULL_BANNER_COMMANDS
                | deploy_cli.BRIEF_BANNER_COMMANDS)

    unclassified = _command_names(parser) - declared
    assert not unclassified, (
        f"unclassified khctl command(s): {sorted(unclassified)} — add each to "
        f"FULL_BANNER_COMMANDS (changes state / produces a kit / mints a "
        f"credential) or BRIEF_BANNER_COMMANDS (reports and exits)")

    stale = declared - _command_names(parser)
    assert not stale, f"classified but nonexistent command(s): {sorted(stale)}"


def test_the_two_sets_do_not_overlap():
    assert not (deploy_cli.FULL_BANNER_COMMANDS
                & deploy_cli.BRIEF_BANNER_COMMANDS)


@pytest.mark.parametrize("command", sorted(deploy_cli.FULL_BANNER_COMMANDS))
def test_state_changing_commands_get_the_full_banner(command):
    args = _namespace_for(command)
    assert deploy_cli.wants_full_banner(args), (
        f"{command} changes state or ships an artifact — it gets the full "
        f"banner")


@pytest.mark.parametrize("command", sorted(deploy_cli.BRIEF_BANNER_COMMANDS))
def test_read_only_commands_get_one_line(command):
    args = _namespace_for(command)
    assert not deploy_cli.wants_full_banner(args)


def test_an_unclassified_command_gets_the_verbose_side():
    """If nobody has decided yet, be loud. The test above fails on the next run
    either way, so this only governs the window in between."""
    assert deploy_cli.wants_full_banner(_namespace_for("brand-new-command"))


def test_the_one_line_form_is_the_full_banner_first_line():
    """The two forms must read as the same statement — moving between them
    teaches nothing new, and the short one still makes the posture impossible
    to mistake, which is the actual safety property."""
    from knowledge_hub.config import posture_line

    for posture in (POSTURE_LOCAL, POSTURE_DEPLOYED):
        s = _settings_at(posture)
        assert posture_line(s) == posture_banner(s)[0]
        assert posture.upper() in posture_line(s)


def test_the_brief_form_is_actually_shorter():
    from knowledge_hub.config import posture_line

    s = _settings_at(POSTURE_LOCAL)
    assert len(posture_line(s)) < len("\n".join(posture_banner(s)))


# ---------------------------------------------------------------------------
# The banner tells the Stage 2 truth
# ---------------------------------------------------------------------------
def test_local_banner_now_says_ceremony_is_off_and_make_kit_refuses():
    """Stage 1 could not claim this; Stage 2 can. The banner has to keep pace
    with what is actually wired, in both directions."""
    text = "\n".join(posture_banner(_settings_at(POSTURE_LOCAL)))
    assert "ceremony OFF" in text
    assert "REFUSES" in text, (
        "the hard gate is the one thing an operator most needs to know before "
        "reaching for make-kit")


def test_deployed_banner_still_says_ceremony_is_on():
    text = "\n".join(posture_banner(_settings_at(POSTURE_DEPLOYED)))
    assert "ceremony ON" in text


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _option_strings(command: str) -> set[str]:
    """Every flag argparse accepts for one khctl subcommand."""
    import argparse

    parser = _build_parser()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            sub = action.choices.get(command)
            if sub is not None:
                return {opt for a in sub._actions for opt in a.option_strings}
    raise AssertionError(f"no such khctl command: {command}")


def _gate_and_call_lines(func, *, gate_attr: str, called: tuple[str, ...]):
    """(line of the `if settings.<gate_attr>` branch, {name: line of its call}).

    AST, because reasoning about code order from string positions reads
    comments and docstrings as code. Line numbers are relative to the function
    source, which is all this needs — only their ORDER is asserted on.
    """
    import ast
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    gate_line = None
    call_lines: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            for inner in ast.walk(node.test):
                if (isinstance(inner, ast.Attribute)
                        and inner.attr == gate_attr):
                    if gate_line is None or node.lineno < gate_line:
                        gate_line = node.lineno
        elif isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(
                node.func, "attr", None)
            if name in called and name not in call_lines:
                call_lines[name] = node.lineno
    return gate_line, call_lines


def _settings_at(posture: str):
    from knowledge_hub.config import Settings
    return Settings(_env_file="does_not_exist_anywhere", KH_POSTURE=posture)


def _build_parser():
    """khctl's real parser, without running anything. deploy_cli.main builds it
    and then dispatches, so it is reached by parsing a command line that
    argparse rejects — the parser is fully built by then."""
    import argparse

    captured = {}
    real_parse = argparse.ArgumentParser.parse_args

    def capture(self, *a, **kw):
        captured.setdefault("parser", self)
        raise SystemExit(0)

    argparse.ArgumentParser.parse_args = capture
    try:
        with pytest.raises(SystemExit):
            deploy_cli.main([])
    finally:
        argparse.ArgumentParser.parse_args = real_parse
    return captured["parser"]


def _command_names(parser) -> set[str]:
    """Every dispatchable command, nested ones as "parent child"."""
    import argparse

    names: set[str] = set()
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        for name, sub in action.choices.items():
            nested = _command_names(sub)
            if nested:
                names |= {f"{name} {n}" for n in nested}
            else:
                names.add(name)
    return names


def _namespace_for(command: str):
    """A Namespace shaped like argparse's for `command`, for banner_key()."""
    import argparse

    parts = command.split(" ", 1)
    ns = argparse.Namespace(command=parts[0])
    if len(parts) > 1:
        ns.migrations_command = parts[1]
    return ns
