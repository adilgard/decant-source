"""The postgres bootstrap-password guard (isolation rider close-out).

A deployment that forgets to set POSTGRES_PASSWORD used to come up on the
committed pilot value — a KNOWN password on the schema-owner account, the
same layer the role isolation work secured. config.require_safe_postgres_password
now refuses to build the bootstrap DSN in any context where that is unsafe:
deployed posture, or a database host that is not this box. The one legitimate
holder of the default is the local single-user pilot (local posture, loopback
DB), whose plaintext-local credential model is deliberate.

Both directions are pinned here: the guard FIRES where it must, and it does
NOT fire on the pilot dev path it was told to leave alone.
"""
from __future__ import annotations

import pytest

from knowledge_hub.config import (
    PILOT_POSTGRES_PASSWORD,
    POSTURE_DEPLOYED,
    InsecurePostgresPasswordError,
    Settings,
)

# Same convention as test_posture.py: a path that exists nowhere, so Settings
# reads class defaults + OS env only — never this repo's .env.
NO_ENV = "does_not_exist_anywhere"


@pytest.fixture(autouse=True)
def _no_inherited_credentials(monkeypatch):
    """The bench shell may carry KH_POSTURE or POSTGRES_* from other work —
    these tests state their inputs explicitly, so inherited values are
    noise that would make them pass or fail for the wrong reason."""
    for var in ("KH_POSTURE", "POSTGRES_PASSWORD", "POSTGRES_HOST",
                "POSTGRES_USER", "POSTGRES_PORT", "POSTGRES_DB"):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# 1. The guard FIRES — a security guard you've only seen pass is one you
#    don't yet trust
# ---------------------------------------------------------------------------
def test_deployed_posture_on_the_pilot_default_refuses():
    s = Settings(_env_file=NO_ENV, KH_POSTURE=POSTURE_DEPLOYED)
    assert s.postgres_password == PILOT_POSTGRES_PASSWORD  # the silent path
    with pytest.raises(InsecurePostgresPasswordError):
        _ = s.postgres_dsn


def test_deployed_posture_on_an_empty_password_refuses():
    s = Settings(_env_file=NO_ENV, KH_POSTURE=POSTURE_DEPLOYED,
                 postgres_password="")
    with pytest.raises(InsecurePostgresPasswordError) as e:
        _ = s.postgres_dsn
    assert "unset" in str(e.value)


def test_nonlocal_db_host_on_the_default_refuses_even_in_local_posture():
    """Alex's rule: a pilot-default credential pointed at a database that is
    not this box is never right, posture regardless."""
    s = Settings(_env_file=NO_ENV, postgres_host="pg.client.lan")
    assert s.is_local
    with pytest.raises(InsecurePostgresPasswordError):
        _ = s.postgres_dsn


def test_the_message_carries_the_fix_in_one_read():
    """A user who hits this must know what to set and where without digging:
    the env key, the .env location, and the server-side rotation that a
    live database also needs."""
    s = Settings(_env_file=NO_ENV, KH_POSTURE=POSTURE_DEPLOYED)
    with pytest.raises(InsecurePostgresPasswordError) as e:
        _ = s.postgres_dsn
    message = str(e.value)
    assert "POSTGRES_PASSWORD" in message
    assert ".env" in message
    assert "ALTER USER" in message          # .env alone doesn't rotate the DB
    assert "khctl plan" in message          # the fresh-deploy path mints one


def test_role_dsn_bootstrap_fallback_is_guarded_too():
    """The loud fallback for an unprovisioned role lands on the bootstrap
    DSN — in deployed posture on the default password that funnel must
    refuse the same way, or the side door reopens through it."""
    from knowledge_hub import roles

    s = Settings(_env_file=NO_ENV, KH_POSTURE=POSTURE_DEPLOYED)
    assert not s.kh_pg_serving_password  # unprovisioned -> fallback path
    with pytest.raises(InsecurePostgresPasswordError):
        _ = s.role_dsn(roles.SERVING_ROLE)


# ---------------------------------------------------------------------------
# 2. The guard does NOT over-fire
# ---------------------------------------------------------------------------
def test_local_pilot_on_loopback_keeps_its_deliberate_default():
    """The dev box: local posture, DB on this box, committed default — the
    single-user plaintext-local model is a decision, not an accident, and
    the guard must not tax it."""
    for host in ("localhost", "127.0.0.1"):
        s = Settings(_env_file=NO_ENV, postgres_host=host)
        assert PILOT_POSTGRES_PASSWORD in s.postgres_dsn


def test_deployed_posture_with_a_real_password_builds_the_dsn():
    s = Settings(_env_file=NO_ENV, KH_POSTURE=POSTURE_DEPLOYED,
                 postgres_password="a-real-minted-password")
    assert "a-real-minted-password" in s.postgres_dsn


def test_nonlocal_host_with_a_real_password_builds_the_dsn():
    s = Settings(_env_file=NO_ENV, postgres_host="pg.client.lan",
                 postgres_password="their-svc-password")
    assert "pg.client.lan" in s.postgres_dsn


# ---------------------------------------------------------------------------
# 3. The SECOND funnel — dsn_from_env never constructs Settings, which is
#    exactly how the first draft of this guard got walked around on its
#    first live fire test (khctl migrations status connected happily in
#    deployed posture on the pilot password)
# ---------------------------------------------------------------------------
def test_dsn_from_env_refuses_the_default_in_deployed_posture(monkeypatch):
    from knowledge_hub.deploy_apply import dsn_from_env

    monkeypatch.setenv("KH_POSTURE", POSTURE_DEPLOYED)
    with pytest.raises(InsecurePostgresPasswordError):
        dsn_from_env({"POSTGRES_PASSWORD": PILOT_POSTGRES_PASSWORD,
                      "POSTGRES_HOST": "127.0.0.1"})


def test_dsn_from_env_reads_posture_from_the_env_dict_too():
    """A deployed home's .env may carry KH_POSTURE while the invoking shell
    does not — the parsed dict is the deployment's own statement of what it
    is, so the guard honors it."""
    from knowledge_hub.deploy_apply import dsn_from_env

    with pytest.raises(InsecurePostgresPasswordError):
        dsn_from_env({"KH_POSTURE": POSTURE_DEPLOYED,
                      "POSTGRES_PASSWORD": PILOT_POSTGRES_PASSWORD,
                      "POSTGRES_HOST": "127.0.0.1"})


def test_dsn_from_env_refuses_a_missing_password_on_a_nonlocal_host():
    from knowledge_hub.deploy_apply import dsn_from_env

    with pytest.raises(InsecurePostgresPasswordError):
        dsn_from_env({"POSTGRES_HOST": "pg.client.lan"})


def test_dsn_from_env_local_pilot_and_real_passwords_still_build():
    from knowledge_hub.deploy_apply import dsn_from_env

    # the pilot dev box: local posture, loopback, committed default
    assert PILOT_POSTGRES_PASSWORD in dsn_from_env(
        {"POSTGRES_PASSWORD": PILOT_POSTGRES_PASSWORD,
         "POSTGRES_HOST": "127.0.0.1"})
    # a rendered deployment: minted value builds anywhere
    assert "minted" in dsn_from_env(
        {"KH_POSTURE": POSTURE_DEPLOYED, "POSTGRES_PASSWORD": "minted",
         "POSTGRES_HOST": "127.0.0.1"})


def test_dsn_from_env_candidate_flag_is_the_probe_opt_out(monkeypatch):
    """Probe's discovery sweep TRIES the pilot credential to see whether a
    pilot stack answers — that DSN is a guess to test, not a credential to
    trust, and it must keep building in any posture."""
    from knowledge_hub.deploy_apply import dsn_from_env

    monkeypatch.setenv("KH_POSTURE", POSTURE_DEPLOYED)
    dsn = dsn_from_env({"POSTGRES_PASSWORD": PILOT_POSTGRES_PASSWORD,
                        "POSTGRES_HOST": "127.0.0.1"}, candidate=True)
    assert PILOT_POSTGRES_PASSWORD in dsn


def test_deployed_posture_never_needs_the_dsn_until_it_touches_the_db():
    """The bench flow that ruled out construction-time enforcement:
    KH_POSTURE=deployed against a pilot .env is exactly how make-kit runs,
    and make-kit never builds a postgres DSN. Constructing Settings and
    reading non-DB fields must stay legal."""
    s = Settings(_env_file=NO_ENV, KH_POSTURE=POSTURE_DEPLOYED)
    assert s.is_deployed
    assert s.ollama_host  # any non-DB read: no raise before the DSN funnel
