"""d.s Stage 1 — the posture field: default local, explicit deployed, loud.

Three things are proven here, in rising order of how much they matter:

1. RESOLUTION. Absent, blank, and unset all mean `local`; `deployed` means
   deployed; anything else is an error rather than a silent guess.
2. LOUDNESS. Every process entry point prints the banner, enforced by reading
   the source of each `main()` — so a fifth entry point added later cannot
   quietly ship without announcing its posture.
3. SCOPE. The provenance/correctness/boundary checks do not read the posture
   field. This is a TRIPWIRE, not a courtesy test: the whole safety argument
   for internal-by-default is that posture gates PRODUCT CEREMONY only, and a
   future edit that made check_migrations or the ontology allowlist
   posture-dependent would break that argument. It should fail here first.

No services touched — everything is config and source text.
"""
from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError

from knowledge_hub.config import (
    POSTURE_DEPLOYED,
    POSTURE_LOCAL,
    POSTURES,
    DEFAULT_ENV_FILE,
    Settings,
    posture_banner,
    print_posture_banner,
    reload_settings,
    settings,
)

INFRA_DIR = Path(__file__).resolve().parents[2]
INFRA_ENV = INFRA_DIR / DEFAULT_ENV_FILE

# A path that exists nowhere, so Settings reads class defaults + OS env only.
NO_ENV = "does_not_exist_anywhere"


@pytest.fixture(autouse=True)
def _no_inherited_posture(monkeypatch):
    """The OS environment must not decide these outcomes. A developer bench
    that happened to export KH_POSTURE would otherwise flip half of them."""
    monkeypatch.delenv("KH_POSTURE", raising=False)
    monkeypatch.delenv("KH_LOCAL_SECRETS_FILE", raising=False)


# ---------------------------------------------------------------------------
# 1. Resolution
# ---------------------------------------------------------------------------
def test_default_resolves_to_local():
    """The headline default: nothing set anywhere means internal posture."""
    s = Settings(_env_file=NO_ENV)
    assert s.posture == POSTURE_LOCAL
    assert s.is_local and not s.is_deployed


def test_explicit_deployed_resolves_to_deployed(monkeypatch):
    monkeypatch.setenv("KH_POSTURE", POSTURE_DEPLOYED)
    s = Settings(_env_file=NO_ENV)
    assert s.posture == POSTURE_DEPLOYED
    assert s.is_deployed and not s.is_local


def test_explicit_local_resolves_to_local(monkeypatch):
    monkeypatch.setenv("KH_POSTURE", POSTURE_LOCAL)
    assert Settings(_env_file=NO_ENV).is_local


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_blank_resolves_to_local(monkeypatch, blank):
    """An exported-but-empty variable is unset in every way that matters. On
    Windows especially, a shell can hand through an empty string where a
    developer meant to clear the value."""
    monkeypatch.setenv("KH_POSTURE", blank)
    assert Settings(_env_file=NO_ENV).posture == POSTURE_LOCAL


@pytest.mark.parametrize("raw,expected", [
    ("DEPLOYED", POSTURE_DEPLOYED),
    (" deployed ", POSTURE_DEPLOYED),
    ("Local", POSTURE_LOCAL),
    ("\tLOCAL\n", POSTURE_LOCAL),
])
def test_case_and_whitespace_are_tolerated(monkeypatch, raw, expected):
    """`KH_POSTURE=Deployed` is unambiguous about intent. Refusing it would be
    pedantry — but note that tolerance stops at spelling: see below."""
    monkeypatch.setenv("KH_POSTURE", raw)
    assert Settings(_env_file=NO_ENV).posture == expected


@pytest.mark.parametrize("bogus", ["prod", "production", "deploy", "hardened",
                                   "soft", "internal", "true", "1"])
def test_an_unknown_posture_raises_instead_of_guessing(monkeypatch, bogus):
    """Neither fallback direction is safe, so there is no fallback.

    Reading `prod` as `local` would ship a soft build from an operator who
    believed they had hardened it. Reading it as `deployed` would demand a
    vault and a signing key on a laptop. Both are silent; an exception is not.
    """
    monkeypatch.setenv("KH_POSTURE", bogus)
    with pytest.raises(ValidationError) as e:
        Settings(_env_file=NO_ENV)
    message = str(e.value)
    assert bogus in message, "the error must quote what was actually set"
    for value in POSTURES:
        assert value in message, "the error must list the valid values"


def test_the_two_accessors_are_exclusive_and_total():
    for value in POSTURES:
        s = Settings(_env_file=NO_ENV, KH_POSTURE=value)
        assert s.is_local != s.is_deployed


def test_the_alias_is_the_only_way_in():
    """Pins a real consequence of using KH_POSTURE as a validation_alias: the
    keyword form is IGNORED, not honored, because extra="ignore" (which .env
    files need) swallows it. Fixing that with populate_by_name=True would also
    re-open bare `POSTURE` as an env var, and a stray `POSTURE=local` picked up
    from an unrelated tool would silently soften a real deploy. So this stays,
    and lives here as documentation rather than as a surprise."""
    assert Settings(_env_file=NO_ENV, KH_POSTURE=POSTURE_DEPLOYED).is_deployed
    assert Settings(_env_file=NO_ENV, posture=POSTURE_DEPLOYED).is_local


def test_this_repo_env_carries_no_posture_so_the_bench_is_local():
    """The bench proves the default end to end: the repo .env sets no posture,
    so the singleton every module imports is on `local` without anyone having
    configured it. If someone later pins KH_POSTURE=deployed in this .env,
    this fails and says so — that is a decision, not a detail."""
    assert INFRA_ENV.is_file(), f"expected a bench .env at {INFRA_ENV}"
    assert "KH_POSTURE" not in INFRA_ENV.read_text(encoding="utf-8")
    assert Settings(_env_file=str(INFRA_ENV)).posture == POSTURE_LOCAL


def test_local_secrets_file_has_a_default_and_is_overridable(monkeypatch):
    assert Settings(_env_file=NO_ENV).local_secrets_file == ".secrets.local.json"
    monkeypatch.setenv("KH_LOCAL_SECRETS_FILE", "elsewhere/creds.json")
    assert (Settings(_env_file=NO_ENV).local_secrets_file
            == "elsewhere/creds.json")


# ---------------------------------------------------------------------------
# 2. Loudness
# ---------------------------------------------------------------------------
def test_local_banner_names_the_posture_ceremony_and_credentials():
    lines = posture_banner(Settings(_env_file=NO_ENV))
    text = "\n".join(lines)
    assert "LOCAL" in lines[0]
    # Deliberately not asserting "ceremony OFF": at Stage 1 ceremony is still
    # unconditional, and the banner says so. Stage 2 rewrites that line. What
    # must hold in EVERY stage is that both subjects are addressed.
    assert "ceremony" in text
    assert "credentials" in text
    assert "KH_POSTURE=deployed" in text, (
        "a local run must say how to get the hardened path back — the switch "
        "is only reversible if the operator can see the way back")


def test_the_banner_promises_nothing_it_has_not_wired():
    """The banner's own honesty, now stated positively.

    Through Stages 1 and 2 this test asserted the ABSENCE of claims that were
    not yet true — first about ceremony, then about credentials — because a
    banner announcing a skip before the skip existed would be the exact defect
    it exists to prevent, committed by itself. Stage 3 made the last of those
    claims true, so it flips: no staged "not yet" wording should remain
    anywhere in it.
    """
    for posture in POSTURES:
        text = "\n".join(posture_banner(
            Settings(_env_file=NO_ENV, KH_POSTURE=posture)))
        for pending in ("Stage 2", "Stage 3", "not yet", "still unconditional",
                        "moves these"):
            assert pending not in text, (
                f"the {posture} banner still carries staged wording "
                f"({pending!r}) — every line must describe what is wired now")


def test_local_banner_states_the_two_things_that_changed():
    text = "\n".join(posture_banner(Settings(_env_file=NO_ENV)))
    assert "ceremony OFF" in text
    assert "local file" in text
    assert "no vault" in text


def test_deployed_banner_names_the_posture_and_the_vault():
    s = Settings(_env_file=NO_ENV, KH_POSTURE=POSTURE_DEPLOYED)
    lines = posture_banner(s)
    text = "\n".join(lines)
    assert "DEPLOYED" in lines[0]
    assert "ceremony ON" in text
    assert s.bao_addr in text


def test_the_two_banners_are_never_confusable():
    """Same shape, opposite content: whichever one is on screen, one glance at
    the first line settles which posture the process is on."""
    local = posture_banner(Settings(_env_file=NO_ENV))
    deployed = posture_banner(Settings(_env_file=NO_ENV,
                                       KH_POSTURE=POSTURE_DEPLOYED))
    assert "LOCAL" in local[0] and "LOCAL" not in deployed[0]
    assert "DEPLOYED" in deployed[0] and "DEPLOYED" not in local[0]


def test_banner_carries_no_secret_values():
    """The banner prints on every run, including into operator.log. It may
    name the vault ADDRESS and the credential FILE PATH; it must never carry
    a token, password, or key."""
    for posture in POSTURES:
        s = Settings(_env_file=str(INFRA_ENV), KH_POSTURE=posture)
        text = "\n".join(posture_banner(s))
        for secret in (s.bao_root_token, s.postgres_password,
                       s.s3_secret_key):
            assert secret not in text


def test_print_posture_banner_emits_every_line_through_the_seam():
    captured: list[str] = []
    print_posture_banner(captured.append, Settings(_env_file=NO_ENV))
    assert captured == posture_banner(Settings(_env_file=NO_ENV))
    assert captured, "the banner must not be empty"


# The four process entry points, and where the guard reads each one from.
# (module, attribute) for importable ones; check_stack.py is a root-level
# script read as text rather than imported, so this stays free of sys.path
# assumptions about the infra directory.
ENTRY_POINTS = [
    ("knowledge_hub.deploy_cli", "main"),
    ("knowledge_hub.service_http", "main"),
    ("knowledge_hub.operator_http", "main"),
]


@pytest.mark.parametrize("module_name,func_name", ENTRY_POINTS)
def test_every_package_entry_point_prints_the_banner(module_name, func_name):
    """Static, not behavioral, on purpose: calling these mains would start
    servers and open sockets. What needs guarding is that the call EXISTS in
    each one, which the source answers cheaply and exactly."""
    import importlib

    module = importlib.import_module(module_name)
    source = inspect.getsource(getattr(module, func_name))
    # `print_posture_banner(` with the paren, not `()` — khctl passes
    # brief=... (d.s Stage 2's cheaper variant), so an exact-empty-call match
    # would have quietly stopped guarding the one entry point with the most
    # subcommands.
    assert "print_posture_banner(" in source, (
        f"{module_name}.{func_name}() does not print the posture banner — "
        f"every entry point must, or a soft run can be silent")


def test_check_stack_prints_the_banner():
    source = (INFRA_DIR / "check_stack.py").read_text(encoding="utf-8")
    assert "print_posture_banner(" in source


def test_reload_settings_warns_when_the_posture_changes(tmp_path, monkeypatch,
                                                        caplog):
    """reload_settings can move config under a running session (that is the
    2026-08-03 finding its docstring records). If it moves the POSTURE, the
    banner printed at startup is now describing a process that no longer
    exists, so the change has to announce itself."""
    # Pin the STARTING posture rather than assuming the bench's. The first
    # version asserted "precondition: bench is local" and failed the moment the
    # suite was run with KH_POSTURE=deployed exported — which is exactly how the
    # deployed half of Stage 2 gets verified. A test about a transition should
    # set both of its own ends.
    monkeypatch.setattr(settings, "posture", POSTURE_LOCAL)
    home = tmp_path / "home"
    home.mkdir()
    (home / DEFAULT_ENV_FILE).write_text(f"KH_POSTURE={POSTURE_DEPLOYED}\n",
                                         encoding="utf-8")
    monkeypatch.chdir(home)
    try:
        with caplog.at_level("WARNING"):
            assert reload_settings() == Path(DEFAULT_ENV_FILE)
        assert settings.posture == POSTURE_DEPLOYED
        assert "POSTURE CHANGED" in caplog.text
    finally:
        # Mandatory: the temp .env carried ONE key, so the reload above reset
        # every other field to its class default. Leaving that in place is the
        # suite-poisoning bug config.py's docstring records.
        reload_settings(INFRA_ENV)


def test_reload_settings_is_quiet_when_the_posture_holds(tmp_path, monkeypatch,
                                                         caplog):
    """The banner is loud; this log line is not a second banner. It fires on
    CHANGE only, or it would be noise on every launcher chdir.

    Both ends pinned, same reason as the test above: the .env being read names
    no posture, and this module's autouse fixture clears KH_POSTURE from the
    environment, so the reload resolves to `local`. Starting anywhere else would
    make this a change-detection test wearing the wrong name."""
    monkeypatch.setattr(settings, "posture", POSTURE_LOCAL)
    home = tmp_path / "home"
    home.mkdir()
    (home / DEFAULT_ENV_FILE).write_text("POSTGRES_DB=other_db\n",
                                         encoding="utf-8")
    monkeypatch.chdir(home)
    try:
        with caplog.at_level("WARNING"):
            reload_settings()
        assert settings.posture == POSTURE_LOCAL, "precondition held"
        assert "POSTURE CHANGED" not in caplog.text
    finally:
        reload_settings(INFRA_ENV)


# ---------------------------------------------------------------------------
# 3. Scope tripwire — posture gates ceremony, NEVER these
# ---------------------------------------------------------------------------
# Each entry: what it guards, and how to reach its source. Functions are read
# body-only via inspect so that a posture branch elsewhere in the same module
# (checks.py legitimately gains one for check_openbao) cannot mask a violation
# inside one of these.
PROTECTED_FUNCTIONS = [
    ("knowledge_hub.checks", "check_migrations", "migration ledger drift"),
    ("knowledge_hub.checks", "check_side_doors", "side-door audit"),
    ("knowledge_hub.checks", "check_core_boundary", "core boundary"),
]

# Whole modules that must stay posture-blind: correctness machinery with no
# legitimate reason to know what kind of deployment it is running in.
PROTECTED_MODULES = [
    ("knowledge_hub/grounding.py", "span grounding / verification"),
    ("knowledge_hub/ontology_registry.py", "the ontology allowlist gate"),
    ("knowledge_hub/extraction_llm.py", "allowlist quarantine at extraction"),
    ("knowledge_hub/scoring_tiered.py", "gray-band adjudication"),
]

PKG_DIR = Path(__file__).resolve().parents[1]

# The names that would indicate a posture branch: the field itself and the two
# accessors. Matched as CODE, never as text — see _posture_references.
POSTURE_NAMES = frozenset({"posture", "is_local", "is_deployed"})
POSTURE_STRINGS = frozenset({"KH_POSTURE", "KH_LOCAL_SECRETS_FILE"})


def _posture_references(source: str) -> list[str]:
    """Genuine code references to the posture, found by parsing rather than by
    substring search.

    A plain `"posture" in source` scan looked fine and was wrong: it fired on
    extraction_llm.py, whose module docstring opens with "Determinism posture
    (§8.2c/f/g)". Prose about determinism is not a posture branch. The failure
    mode of a false positive here is worse than a missed detection, too — it
    trains whoever hits it to edit the tripwire instead of reading it, which
    is exactly how a real violation would get waved through later.

    So: walk the AST and count only attribute access (`settings.is_local`),
    bare names (`posture = ...`), keyword arguments (`posture=...`), and the
    env-var name as a string literal. Comments and docstrings are invisible to
    all four.
    """
    tree = ast.parse(textwrap.dedent(source))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in POSTURE_NAMES:
            found.add(node.attr)
        elif isinstance(node, ast.Name) and node.id in POSTURE_NAMES:
            found.add(node.id)
        elif isinstance(node, ast.keyword) and node.arg in POSTURE_NAMES:
            found.add(node.arg)
        elif (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and node.value in POSTURE_STRINGS):
            found.add(node.value)
    return sorted(found)


@pytest.mark.parametrize("module_name,func_name,what", PROTECTED_FUNCTIONS,
                         ids=[f[1] for f in PROTECTED_FUNCTIONS])
def test_protected_check_does_not_read_the_posture(module_name, func_name,
                                                   what):
    import importlib

    source = inspect.getsource(getattr(importlib.import_module(module_name),
                                       func_name))
    found = _posture_references(source)
    assert not found, (
        f"{func_name} ({what}) reads the posture ({found}) — this is a "
        f"provenance/boundary check, not product ceremony. It must behave "
        f"identically in both postures. If a posture branch really belongs "
        f"here, that is a design decision to make deliberately, not a test "
        f"to update.")


@pytest.mark.parametrize("rel_path,what", PROTECTED_MODULES,
                         ids=[m[0] for m in PROTECTED_MODULES])
def test_protected_module_stays_posture_blind(rel_path, what):
    source = (PKG_DIR / rel_path).read_text(encoding="utf-8")
    found = _posture_references(source)
    assert not found, (
        f"{rel_path} ({what}) references the posture ({found}) — correctness "
        f"machinery must not know what kind of deployment it is in")


def test_the_tripwire_detects_a_real_violation():
    """A guard nobody has seen fail is a guard nobody should trust. These are
    the four shapes a posture branch can take."""
    assert _posture_references("if settings.is_local: skip_the_gate()")
    assert _posture_references("posture = 'local'")
    assert _posture_references("build(posture=settings.posture)")
    assert _posture_references("os.environ['KH_POSTURE']")


def test_the_tripwire_ignores_prose():
    """The false positive that caught this test out on its first run:
    extraction_llm.py's docstring opens with 'Determinism posture (§8.2c/f/g)'.
    Comments and docstrings are documentation, not branches."""
    assert not _posture_references('"""Determinism posture (§8.2c/f/g)."""')
    assert not _posture_references("# the deployed posture is unchanged\nx = 1")
    assert not _posture_references("def check_migrations(dsn=None): ...")
