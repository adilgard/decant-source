"""Two hangs that cost a test run 45 minutes each, and the guards against them.

Both were found on 2026-08-03 while trying to get a clean full-suite result
after the migration-ledger work, and neither was caused by it.

1. `ollama.Client` defaults to `httpx.Timeout(None)` — unbounded on connect AND
   read. A stalled generate wedged the suite indefinitely: two ESTABLISHED
   connections to 127.0.0.1:11434, zero bytes, zero CPU, no end. psycopg had
   been bounded after the same class of hang (fe30871) and every urlopen call
   already passed a timeout; the inference seam was missed.

2. `reload_settings()` called `Settings()` unconditionally, so running it from a
   directory with no .env silently reverted EVERY field to its class default.
   Three tests restored themselves that way while still chdir'd into a temp
   home, which put the whole process on `localhost` instead of the .env's
   pinned `127.0.0.1` — and every later test then paid Docker Desktop's
   dual-stack stall (~10s) on each fresh connection. Their restore assertions
   passed because they checked `s3_access_key`, the one field whose .env value
   equals its class default.

The `test_no_module_constructs_an_ollama_client_directly` test below is the one
that matters most over time: without it, an eighth unbounded client is one
convenient line away.
"""
from __future__ import annotations

import ast
from pathlib import Path

import httpx
import pytest

from knowledge_hub.config import (
    DEFAULT_ENV_FILE,
    Settings,
    reload_settings,
    settings,
)
from knowledge_hub.ollama_client import make_ollama_client, ollama_timeout

PKG_DIR = Path(__file__).resolve().parents[1] / "knowledge_hub"
INFRA_DIR = Path(__file__).resolve().parents[2]
INFRA_ENV = INFRA_DIR / DEFAULT_ENV_FILE


# ---------------------------------------------------------------------------
# 1. Ollama HTTP budget
# ---------------------------------------------------------------------------
def test_ollama_timeout_is_bounded_on_every_phase():
    """`None` on ANY phase is the bug. httpx treats None as 'wait forever'."""
    t = ollama_timeout()
    assert isinstance(t, httpx.Timeout)
    for phase in ("connect", "read", "write", "pool"):
        value = getattr(t, phase)
        assert value is not None, f"{phase} timeout is None — unbounded"
        assert value > 0


def test_connect_budget_is_much_shorter_than_read_budget():
    """The two failure modes are unrelated: a connect that accepts and never
    answers is the dual-stack black hole (fail fast), while a read can
    legitimately be a 36B model generating for minutes (be patient)."""
    t = ollama_timeout()
    assert t.connect < t.read
    assert t.read >= 60, "a real extraction needs minutes, not seconds"


def test_client_carries_the_bounded_timeout_not_the_library_default():
    """The library default is Timeout(None) — prove we overrode it, by reading
    the timeout off the httpx client the ollama client actually uses."""
    import ollama

    default = ollama.Client(host="http://127.0.0.1:11434")
    assert default._client.timeout.read is None, (
        "upstream ollama no longer defaults to an unbounded read — this test "
        "documents WHY the factory exists; re-check the factory if it changed")

    ours = make_ollama_client()
    assert ours._client.timeout.read == settings.ollama_read_timeout_s
    assert ours._client.timeout.connect == settings.ollama_connect_timeout_s


def test_timeout_follows_settings_reloads():
    """Read from settings per call, not captured at import: the launcher
    refreshes the singleton in place when it chdirs into a deployment home,
    and a frozen value would ignore that deployment's tuning."""
    before = settings.ollama_read_timeout_s
    try:
        settings.ollama_read_timeout_s = 123.0
        assert ollama_timeout().read == 123.0
        assert make_ollama_client()._client.timeout.read == 123.0
    finally:
        settings.ollama_read_timeout_s = before


def test_no_module_constructs_an_ollama_client_directly():
    """THE GUARD. Every Ollama consumer must go through the factory, or it
    inherits the unbounded default. Parsed rather than grepped so a rename or
    an `from ollama import Client` spelling cannot slip past.

    If this fails: use `make_ollama_client(host)` instead of building a client.
    Tests inject fakes through the existing `client=` parameters, which this
    does not touch.
    """
    offenders: list[str] = []
    for path in sorted(PKG_DIR.rglob("*.py")):
        if path.name == "ollama_client.py":       # the one legal home
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # Names bound to ollama.Client by an `from ollama import Client`.
        aliased = {
            alias.asname or alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "ollama"
            for alias in node.names if alias.name == "Client"
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            hit = (
                (isinstance(fn, ast.Attribute) and fn.attr == "Client"
                 and isinstance(fn.value, ast.Name) and fn.value.id == "ollama")
                or (isinstance(fn, ast.Name) and fn.id in aliased)
            )
            if hit:
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, (
        f"these construct an ollama client directly and so inherit the "
        f"unbounded default: {offenders}. Use make_ollama_client(host).")


# ---------------------------------------------------------------------------
# 2. reload_settings must not reset what it cannot reload
# ---------------------------------------------------------------------------
def test_reload_with_no_env_is_a_noop_not_a_reset(tmp_path, monkeypatch, caplog):
    """The exact sequence that poisoned the suite: chdir somewhere with no
    .env, then reload. Every field must survive, and it must say so."""
    reload_settings(INFRA_ENV)
    pinned = settings.postgres_host
    assert pinned == "127.0.0.1", (
        f"expected the repo .env's pinned host, got {pinned!r} — this test "
        f"needs that .env to differ from the class default to mean anything")

    monkeypatch.chdir(tmp_path)
    with caplog.at_level("WARNING"):
        result = reload_settings()

    assert result is None
    assert settings.postgres_host == pinned, (
        "reload_settings() reverted to class defaults with no .env to read — "
        "that is the bug: for a deployed process it swaps the real config for "
        "the pilot credentials")
    assert "no .env" in caplog.text.lower() or "leaving" in caplog.text.lower()


def test_reload_from_an_explicit_file_ignores_cwd(tmp_path, monkeypatch):
    """The restore path a test (or the launcher) should use: name the file,
    do not depend on where the process happens to be standing."""
    env = tmp_path / "elsewhere.env"
    env.write_text("POSTGRES_HOST=10.1.2.3\nPOSTGRES_DB=other_db\n",
                   encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    try:
        assert reload_settings(env) == env
        assert settings.postgres_host == "10.1.2.3"
        assert settings.postgres_db == "other_db"
    finally:
        assert reload_settings(INFRA_ENV) == INFRA_ENV
    assert settings.postgres_host == "127.0.0.1"


def test_reload_reads_the_env_in_cwd_when_one_is_there(tmp_path, monkeypatch):
    """The behaviour the launcher depends on is unchanged (BP33): chdir into a
    deployment home and its .env governs the session."""
    home = tmp_path / "home"
    home.mkdir()
    (home / DEFAULT_ENV_FILE).write_text("S3_ACCESS_KEY=kh_from_home\n",
                                         encoding="utf-8")
    monkeypatch.chdir(home)
    try:
        assert reload_settings() == Path(DEFAULT_ENV_FILE)
        assert settings.s3_access_key == "kh_from_home"
    finally:
        reload_settings(INFRA_ENV)


def test_postgres_host_is_the_field_that_reveals_env_loss():
    """Documents why the old restore assertions could not catch this: the field
    they checked is identical in the .env and in the class defaults."""
    from_env = Settings(_env_file=str(INFRA_ENV))
    defaults = Settings(_env_file="does_not_exist_anywhere")
    assert from_env.s3_access_key == defaults.s3_access_key   # blind spot
    assert from_env.postgres_host != defaults.postgres_host   # the tell


@pytest.fixture(autouse=True)
def _restore_real_settings():
    """No test in this module may leak a modified singleton into the rest of
    the suite — that leak is precisely the bug being fixed."""
    yield
    reload_settings(INFRA_ENV)
