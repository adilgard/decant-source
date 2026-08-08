"""Least-privilege Postgres roles (§8.8) — the privilege split, proven.

These tests run against a throwaway DATABASE, but the objects under test are
CLUSTER-level. That mismatch is the whole point of the most important test in
here (`test_absent_password_never_demotes_an_existing_login`): the first
version of ensure_serving_roles demoted every role to NOLOGIN when a caller
supplied no passwords, and because roles are shared across the cluster, one
`pytest` run took the live deployment's four logins offline.
"""
from __future__ import annotations

import uuid

import psycopg
import pytest

from knowledge_hub import roles as R
from knowledge_hub.config import settings


@pytest.fixture(scope="module")
def roles_db() -> str:
    """A throwaway database with one table, for grant checks."""
    admin = settings.postgres_dsn
    name = f"kh_roles_test_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(admin, autocommit=True, connect_timeout=10) as c:
        c.execute(f"CREATE DATABASE {name}")
    dsn = admin.rsplit("/", 1)[0] + "/" + name
    with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as c:
        c.execute("CREATE TABLE demo (id bigserial primary key, v text)")
        c.execute("INSERT INTO demo (v) VALUES ('seed')")
    yield dsn
    with psycopg.connect(admin, autocommit=True, connect_timeout=10) as c:
        c.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


@pytest.fixture(scope="module")
def role_set(roles_db) -> R.RoleSet:
    """A DISJOINT set of role names, never the production ones.

    Roles are CLUSTER-level. Exercising `kh_serving` here would ALTER the
    live deployment's serving role on the shared cluster and re-key it out
    from under the .env — the services would keep running on their open
    connections and fail at the next restart, which is about the worst
    shape a failure can take.

    Torn down explicitly for the same reason: a dropped test DATABASE does
    not take its roles with it, so without this every run would leave four
    more orphans on the cluster forever.
    """
    rs = R.RoleSet.suffixed(f"_t{uuid.uuid4().hex[:8]}")
    yield rs
    drop_roles(roles_db, rs.all)


@pytest.fixture(scope="module")
def passwords(role_set) -> dict[str, str]:
    salt = uuid.uuid4().hex[:8]
    return {role: f"pw_{salt}" for role in role_set.all}


def role_dsn(roles_db: str, role: str, password: str) -> str:
    tail = roles_db.split("@", 1)[1]
    return f"postgresql://{role}:{password}@{tail}"


def drop_roles(roles_db: str, names) -> None:
    """Remove test roles from the CLUSTER, grants and all.

    `DROP OWNED BY`, connected to the database that holds the grants — a
    plain `REVOKE ... ON DATABASE` leaves the table, sequence and default-
    privilege grants behind, and DROP ROLE then refuses with
    DependentObjectsStillExist. Dropping the database later does not help:
    roles outlive it, which is the whole cluster-vs-database trap this
    module is about.
    """
    with psycopg.connect(roles_db, autocommit=True, connect_timeout=10) as c:
        for role in names:
            c.execute(f'DROP OWNED BY "{role}" CASCADE')
    with psycopg.connect(settings.postgres_dsn, autocommit=True,
                         connect_timeout=10) as c:
        for role in names:
            c.execute(f'DROP ROLE IF EXISTS "{role}"')


def test_roles_are_created_and_idempotent(roles_db, passwords, role_set):
    with psycopg.connect(roles_db, autocommit=True, connect_timeout=10) as c:
        first = R.ensure_serving_roles(c, passwords, role_set=role_set)
        assert R.login_roles(c, role_set.all) == set(role_set.all)
        # Running it again must be a no-op, not an error: it runs on EVERY
        # apply so grants keep pace with new tables.
        second = R.ensure_serving_roles(c, passwords, role_set=role_set)
    assert all("present" in line for line in second if line.startswith("role "))
    assert len(first) == len(second)


def test_absent_password_never_demotes_an_existing_login(roles_db, passwords, role_set):
    """REGRESSION, and the expensive one.

    Roles are CLUSTER-level; the database a caller runs against is not. A
    caller with no passwords (the test suite, running phase_schema against a
    throwaway DB) must leave working logins alone. The first version issued
    `ALTER ROLE ... NOLOGIN` here and disabled the live pilot's serving,
    operator, pipeline and reporting roles from a pytest run.

    Absence is not an instruction. Rotation requires an explicit password.
    """
    with psycopg.connect(roles_db, autocommit=True, connect_timeout=10) as c:
        R.ensure_serving_roles(c, passwords, role_set=role_set)
        assert R.login_roles(c, role_set.all) == set(role_set.all)

        R.ensure_serving_roles(c, {}, role_set=role_set)           # the dangerous call
        assert R.login_roles(c, role_set.all) == set(role_set.all), \
            "a caller without passwords revoked logins it did not provision"

        R.ensure_serving_roles(c, None, role_set=role_set)         # and the other spelling
        assert R.login_roles(c, role_set.all) == set(role_set.all)


def test_a_never_provisioned_role_stays_nologin(roles_db):
    """The flip side: unusable-until-provisioned IS right for a FRESH role.
    A role born able to log in would need a password, and any password this
    code could invent would be a guessable one on every deployment.

    Its own role set, function-scoped: the module-scoped one has already
    been fully provisioned by the tests above, so it could not show that a
    never-provisioned role stays shut.
    """
    virgin = R.RoleSet.suffixed(f"_v{uuid.uuid4().hex[:8]}")
    try:
        with psycopg.connect(roles_db, autocommit=True,
                             connect_timeout=10) as c:
            # Provision ONE of the four; the rest must not gain a login.
            R.ensure_serving_roles(c, {virgin.pipeline: "pw_only_pipeline"},
                                   role_set=virgin)
            logins = R.login_roles(c, virgin.all)
            assert logins == {virgin.pipeline}
    finally:
        drop_roles(roles_db, virgin.all)


@pytest.mark.parametrize("kind", ["serving", "report"])
def test_read_roles_cannot_write(roles_db, passwords, role_set, kind):
    """The read-only promise as a GRANT the server enforces, not a session
    setting the client puts on itself."""
    with psycopg.connect(roles_db, autocommit=True, connect_timeout=10) as c:
        R.ensure_serving_roles(c, passwords, role_set=role_set)
    role = getattr(role_set, kind)
    with psycopg.connect(role_dsn(roles_db, role, passwords[role]),
                         autocommit=True, connect_timeout=10) as rc:
        assert rc.execute("SELECT count(*) FROM demo").fetchone()[0] >= 1
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            rc.execute("INSERT INTO demo (v) VALUES ('nope')")


@pytest.mark.parametrize("kind", ["pipeline", "operator"])
def test_write_roles_can_read_and_write(roles_db, passwords, role_set, kind):
    with psycopg.connect(roles_db, autocommit=True, connect_timeout=10) as c:
        R.ensure_serving_roles(c, passwords, role_set=role_set)
    role = getattr(role_set, kind)
    with psycopg.connect(role_dsn(roles_db, role, passwords[role]),
                         autocommit=True, connect_timeout=10) as rc:
        rc.execute("INSERT INTO demo (v) VALUES ('ok')")
        assert rc.execute("SELECT count(*) FROM demo").fetchone()[0] >= 1


def test_each_role_is_its_own_session_user(roles_db, passwords, role_set):
    """What makes check_side_doors able to distinguish consumers at all:
    pg_stat_activity reports the SESSION user, so the four roles must be
    four distinct logins rather than one login wearing SET ROLE."""
    with psycopg.connect(roles_db, autocommit=True, connect_timeout=10) as c:
        R.ensure_serving_roles(c, passwords, role_set=role_set)
    seen = set()
    for role in role_set.all:
        with psycopg.connect(role_dsn(roles_db, role, passwords[role]),
                             autocommit=True, connect_timeout=10) as rc:
            seen.add(rc.execute("SELECT session_user").fetchone()[0])
    assert seen == set(role_set.all)


def test_unsafe_role_identifiers_are_refused():
    """DDL takes no bound parameters, so names are validated rather than
    trusted — refusing beats escaping."""
    for bad in ('kh"; DROP DATABASE knowledge_hub; --', "KH_Serving", "", "kh-serving"):
        with pytest.raises(ValueError):
            R._ident(bad)


def test_passwords_from_env_skips_blanks():
    env = {"KH_PG_SERVING_PASSWORD": "s3cret",
           "KH_PG_REPORT_PASSWORD": "   ",
           "KH_PG_PIPELINE_PASSWORD": ""}
    out = R.passwords_from_env(env)
    assert out == {R.SERVING_ROLE: "s3cret"}


def test_production_roles_are_refused_from_another_database(roles_db):
    """THE CLUSTER GUARD, and the reason it exists.

    Roles are cluster-level; databases are not. Any apply run against a
    throwaway database would otherwise re-key the live deployment's logins:
    `khctl plan` mints fresh KH_PG_* passwords, phase_schema passes them
    here, the ALTER lands cluster-wide, and the real .env still holds the
    old values. Services keep working on their open connections and fail at
    the NEXT RESTART — far from the cause.

    Observed for real on 2026-08-07: two full suite runs silently re-keyed
    the pilot's four roles before this guard was added.
    """
    with psycopg.connect(roles_db, autocommit=True, connect_timeout=10) as c:
        assert c.execute("SELECT current_database()").fetchone()[0] \
            != settings.postgres_db, "fixture must not be the real database"
        lines = R.ensure_serving_roles(
            c, {r: "would_have_clobbered" for r in R.ALL_ROLES})

    assert len(lines) == 1 and "SKIPPED" in lines[0]
    # And it really did nothing: no production role was created here.
    with psycopg.connect(settings.postgres_dsn, autocommit=True,
                         connect_timeout=10) as c:
        for role in R.ALL_ROLES:
            assert R.role_exists(c, role), \
                "guard must skip, not delete — production roles are live"


def test_a_suffixed_role_set_is_never_blocked_by_the_guard(roles_db):
    """The guard keys on the production NAMES, so a disjoint set provisions
    freely from any database — that is what makes these tests possible."""
    rs = R.RoleSet.suffixed(f"_g{uuid.uuid4().hex[:8]}")
    try:
        with psycopg.connect(roles_db, autocommit=True,
                             connect_timeout=10) as c:
            lines = R.ensure_serving_roles(
                c, {r: "pw_guard_probe" for r in rs.all}, role_set=rs)
            assert not any("SKIPPED" in ln for ln in lines)
            assert R.login_roles(c, rs.all) == set(rs.all)
    finally:
        drop_roles(roles_db, rs.all)
