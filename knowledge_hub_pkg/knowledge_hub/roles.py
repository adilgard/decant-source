"""Least-privilege Postgres login roles — the isolation property, enforced
by the database instead of asserted by the client.

Before this module the whole stack connected as ONE superuser (`kh`). Three
consequences, and only the first was ever written down:

  * The serving layer's read-only promise was a session GUC the client set
    on ITSELF (`SET default_transaction_read_only = on`, choke_point.py).
    Belt and braces with no belt: any code path that opened its own
    connection, or forgot the SET, had full write access to every table.
  * `check_side_doors` could not fail. Its allowlist defaults to the DSN's
    own username; every consumer used that username; therefore every
    connection was allowlisted. A drift detector for a distinction the
    database did not make.
  * "Only the pipeline and the service hold a DSN" was unverifiable after
    the fact. `pg_stat_activity` reported four different processes under
    one indistinguishable name.

THE ROLES, and why the split falls where it does:

    kh_pipeline   read/write on domain tables. The only writer of facts,
                  documents, chunks, entities. Owns nothing — DDL stays
                  with the bootstrap account, so a pipeline bug cannot
                  drop a table it can merely fill.
    kh_serving    SELECT and NOTHING else, on the tables the choke point
                  reads. This is the one that turns a promise into a
                  grant: the serving connection can no longer write even
                  if every guard above it is removed.
    kh_operator   read/write, because operator actions ARE writes —
                  resolve reviews, control ingestion, triage quarantine.
                  Separate from kh_pipeline so `pg_stat_activity` can tell
                  a human-initiated write from an ingest one, which is the
                  distinction the audit trail (migration 010) assumes.
    kh_report     SELECT-only, for the consumers that legitimately read the
                  OPERATIONAL tables and are not one of the three trusted
                  services: metrics_report.py, build_corpus.py's summary,
                  the verifier's table probes.

WHY NOT A NUMBERED MIGRATION. Two independent reasons, either sufficient:

  1. `CREATE ROLE` is CLUSTER-level; migrations run inside one database. A
     numbered file would create roles once and then fail its own replay in
     the next database on the same cluster — which is exactly what the test
     harness does, repeatedly, on throwaway databases.
  2. Grants must re-run whenever new tables appear. A one-shot migration
     grants on the tables that existed the day it ran, and every migration
     after it silently ships a table `kh_serving` cannot read. The failure
     surfaces in production as a permission error on one endpoint, months
     later.

So this is idempotent DDL owned by code and re-run on every apply — the
same argument, and the same shape, as `migrations.ensure_ledger`.

PASSWORDS come from the environment (`KH_PG_*_PASSWORD`), rendered into
`.env` by `khctl plan` alongside POSTGRES_PASSWORD. DB credentials are
infrastructure credentials and that is where this codebase already keeps
them; the vault holds SERVING PRINCIPAL tokens, which are a different thing
with a different lifecycle. A role whose password is unset is CREATED
NOLOGIN — present and grantable, but unusable until someone provisions it,
which is a louder failure than a role with a guessable password.

CLUSTER-LEVEL, AND THAT CUTS BOTH WAYS. The reasoning above explains why
`CREATE ROLE` cannot be a numbered migration. The same fact makes every
write in here reach outside the database it was called on: a run against a
throwaway test database alters roles the LIVE deployment is using. So an
absent password NEVER demotes an existing login role — see the comment on
that branch, which exists because the first version did exactly that and
took the pilot's four logins offline from a `pytest` run.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, Optional

from psycopg import sql

logger = logging.getLogger(__name__)

# The trusted set, as data. Every consumer maps onto exactly one of these,
# and `check_side_doors` allowlists precisely these names — so adding a role
# here without giving it a consumer, or giving a consumer a role that is not
# here, both show up as a failing check rather than a silent widening.
PIPELINE_ROLE = "kh_pipeline"
SERVING_ROLE = "kh_serving"
OPERATOR_ROLE = "kh_operator"
REPORT_ROLE = "kh_report"

WRITE_ROLES = (PIPELINE_ROLE, OPERATOR_ROLE)
READ_ROLES = (SERVING_ROLE, REPORT_ROLE)
ALL_ROLES = (PIPELINE_ROLE, SERVING_ROLE, OPERATOR_ROLE, REPORT_ROLE)


@dataclass(frozen=True)
class RoleSet:
    """The four role NAMES this function operates on.

    Parameterized, with the production names as the default, for one reason:
    roles are CLUSTER-level. A test that exercised the real names would
    `ALTER ROLE kh_serving ... PASSWORD` on the shared cluster and silently
    re-key the live deployment's serving role — the .env would still hold
    the old password and every service would fail to connect at the next
    restart. Tests pass a suffixed set (`RoleSet.suffixed('_test_ab12')`) so
    they touch nothing real.

    Not a convenience knob. It is the seam that keeps a cluster-wide write
    inside the blast radius its caller intended.
    """
    pipeline: str = PIPELINE_ROLE
    serving: str = SERVING_ROLE
    operator: str = OPERATOR_ROLE
    report: str = REPORT_ROLE

    @classmethod
    def suffixed(cls, suffix: str) -> "RoleSet":
        return cls(*(f"{r}{suffix}" for r in ALL_ROLES))

    @property
    def all(self) -> tuple[str, ...]:
        return (self.pipeline, self.serving, self.operator, self.report)

    @property
    def read(self) -> tuple[str, ...]:
        return (self.serving, self.report)

    @property
    def write(self) -> tuple[str, ...]:
        return (self.pipeline, self.operator)

    @property
    def is_production(self) -> bool:
        return self.all == ALL_ROLES


DEFAULT_ROLES = RoleSet()

# Env var carrying each role's login password.
PASSWORD_ENV = {
    PIPELINE_ROLE: "KH_PG_PIPELINE_PASSWORD",
    SERVING_ROLE: "KH_PG_SERVING_PASSWORD",
    OPERATOR_ROLE: "KH_PG_OPERATOR_PASSWORD",
    REPORT_ROLE: "KH_PG_REPORT_PASSWORD",
}


def _ident(name: str) -> sql.Identifier:
    """A role/schema name as a composable identifier.

    DDL takes no bound parameters — `ALTER ROLE x PASSWORD %s` is a syntax
    error, not a parameterized statement — so everything here is composed
    with `psycopg.sql` rather than %-formatted. The name is also validated
    first: these names are ours, not user input, and refusing an unexpected
    one beats escaping it.
    """
    if not name or not all(c.islower() or c.isdigit() or c == "_"
                           for c in name):
        raise ValueError(f"unsafe role identifier: {name!r}")
    return sql.Identifier(name)


def role_exists(conn, name: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM pg_roles WHERE rolname = %s", (name,)).fetchone())


def ensure_serving_roles(conn, passwords: Optional[dict[str, str]] = None,
                         schema: str = "public",
                         role_set: Optional[RoleSet] = None) -> list[str]:
    """Create (or update) the four least-privilege login roles and re-grant
    them across every table in `schema`. Idempotent: safe on a fresh
    cluster, safe on every subsequent apply.

    `passwords` maps role name -> password. A role absent from the mapping
    (or mapped to an empty value) is created NOLOGIN — see the module
    docstring on why that is the honest default.

    Returns human-readable lines for the apply transcript. Runs as the
    bootstrap account, which must be able to CREATE ROLE; that is the one
    privilege the split does not remove from `kh`, because something has to
    be able to make the others.
    """
    passwords = passwords or {}
    rs = role_set or DEFAULT_ROLES
    lines: list[str] = []

    # ---- THE CLUSTER GUARD -------------------------------------------------
    # Roles are cluster-level; the database this runs against is not. The
    # PRODUCTION role set therefore belongs to exactly ONE database — the
    # configured one — and a call from anywhere else must not touch it.
    #
    # Without this, any code path that runs an apply against a throwaway
    # database re-keys the live deployment's roles: `khctl plan` mints fresh
    # KH_PG_* passwords, phase_schema hands them to this function, and the
    # ALTER lands on the shared cluster while the real .env still holds the
    # old values. Every service keeps working on its open connection and
    # then cannot reconnect — a failure that appears at the next restart,
    # far from the cause. The test suite did this twice on 2026-08-07 before
    # the guard existed.
    #
    # A test provisioning its OWN disjoint names (RoleSet.suffixed) is never
    # blocked: it cannot collide with production by construction.
    from knowledge_hub.config import settings

    current_db = conn.execute("SELECT current_database()").fetchone()[0]
    if rs.is_production and current_db != settings.postgres_db:
        note = (f"roles: SKIPPED — refusing to touch the production role set "
                f"from database {current_db!r} (the deployment's database is "
                f"{settings.postgres_db!r}). Roles are cluster-level, so this "
                f"would re-key the live deployment's logins. Pass a "
                f"RoleSet.suffixed(...) to provision test roles instead.")
        logger.warning(note)
        return [note]
    # Snapshot BEFORE any ALTER: which roles could already log in. Used to
    # report honestly below, and a reminder that this function shares a
    # cluster with every other database on the box.
    already_login = login_roles(conn, rs.all)

    for role in rs.all:
        ident = _ident(role)
        pw = (passwords.get(role) or "").strip()
        existed = role_exists(conn, role)
        if not existed:
            # NOINHERIT is deliberate: these are login roles, not group
            # roles, and a role that silently inherits another's rights is
            # the same indistinguishability problem one level up.
            conn.execute(
                sql.SQL("CREATE ROLE {} NOLOGIN NOINHERIT").format(ident))
        if pw:
            conn.execute(sql.SQL("ALTER ROLE {} LOGIN PASSWORD {}").format(
                ident, sql.Literal(pw)))
            state = "login (password set)"
        else:
            # ABSENT PASSWORD MEANS "LEAVE IT ALONE", NOT "REVOKE IT".
            #
            # This branch used to run `ALTER ROLE ... NOLOGIN`, and that was
            # a genuinely dangerous bug, because ROLES ARE CLUSTER-LEVEL
            # while the databases this function runs against are not. The
            # test suite calls phase_schema against throwaway databases
            # whose env carries no KH_PG_*_PASSWORD; every such run reached
            # this branch and disabled the LIVE deployment's four logins on
            # the shared cluster. One `pytest` took the pilot's serving,
            # operator, pipeline and reporting roles offline — observed
            # 2026-08-07, which is how this comment came to be here.
            #
            # A fresh role is still born NOLOGIN (see CREATE above): unusable
            # until provisioned is the right default. But demoting a role
            # that already works, because this particular caller happened not
            # to know its password, is never what anyone meant. Rotation
            # requires an explicit new password; silence is not an
            # instruction.
            state = ("login (left as-is; no password supplied by this "
                     "caller)" if role in already_login
                     else "NOLOGIN (never provisioned)")
        lines.append(f"role {role}: {'present' if existed else 'created'}"
                     f", {state}")

    # ---- grants -----------------------------------------------------------
    # Re-granted wholesale every run rather than diffed. The set is small,
    # the operation is cheap, and a diff would need to be right about
    # revocations too — which is where this class of code goes wrong.
    sch = _ident(schema)
    read_list = sql.SQL(", ").join(_ident(r) for r in rs.read)
    write_list = sql.SQL(", ").join(_ident(r) for r in rs.write)
    all_list = sql.SQL(", ").join(_ident(r) for r in rs.all)

    # CONNECT explicitly, rather than relying on the default grant Postgres
    # gives PUBLIC. It works without this today — which is the problem: the
    # roles would silently stop being able to log in the day someone
    # hardens the cluster with `REVOKE CONNECT ON DATABASE ... FROM PUBLIC`,
    # and the failure would look like bad credentials rather than a missing
    # grant.
    db = conn.execute("SELECT current_database()").fetchone()[0]
    conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
        sql.Identifier(db), all_list))
    conn.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
        sch, all_list))
    conn.execute(sql.SQL(
        "GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(
            sch, read_list))
    conn.execute(sql.SQL(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {}"
        " TO {}").format(sch, write_list))
    # Sequences: a writer that cannot advance a sequence cannot INSERT into
    # any table with a generated key, which is nearly all of them.
    conn.execute(sql.SQL(
        "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {} TO {}").format(
            sch, write_list))

    # DEFAULT PRIVILEGES close reason (2) from the module docstring for the
    # future: tables created LATER by this same bootstrap account carry the
    # grants automatically. The explicit re-grant above stays anyway — it
    # covers tables that already exist, which default privileges never
    # retroactively touch.
    conn.execute(sql.SQL(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA {} GRANT SELECT ON TABLES"
        " TO {}").format(sch, read_list))
    conn.execute(sql.SQL(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA {} GRANT SELECT, INSERT, UPDATE,"
        " DELETE ON TABLES TO {}").format(sch, write_list))
    conn.execute(sql.SQL(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA {} GRANT USAGE, SELECT ON"
        " SEQUENCES TO {}").format(sch, write_list))

    n_tables = conn.execute(
        "SELECT count(*) FROM pg_tables WHERE schemaname = %s",
        (schema,)).fetchone()[0]
    lines.append(f"grants: {len(rs.read)} read role(s) SELECT + "
                 f"{len(rs.write)} write role(s) CRUD over {n_tables} "
                 f"table(s) in {schema}, default privileges set for future "
                 f"tables")
    return lines


def passwords_from_env(env: dict[str, str]) -> dict[str, str]:
    """Role -> password, read out of a rendered .env mapping. Missing keys
    are simply absent, which `ensure_serving_roles` turns into NOLOGIN."""
    out: dict[str, str] = {}
    for role, var in PASSWORD_ENV.items():
        value = (env.get(var) or "").strip()
        if value:
            out[role] = value
    return out


def login_roles(conn, candidates: Iterable[str] = ALL_ROLES) -> set[str]:
    """Which of `candidates` currently exist AND can log in. The honest
    input to any 'is the split actually in force?' question — a role that
    exists but is NOLOGIN is provisioned, not adopted."""
    rows = conn.execute(
        "SELECT rolname FROM pg_roles WHERE rolcanlogin AND rolname = ANY(%s)",
        (list(candidates),)).fetchall()
    return {r[0] for r in rows}
