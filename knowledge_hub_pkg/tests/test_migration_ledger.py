"""The migration ledger: drift detection, the apply gate, and the backfill.

Written after the 2026-08-03 pilot finding — migrations 011/012/013 had every
object present and no ledger row, and nothing in the codebase could see it.
`phase_schema` had no tests at all, which is a large part of why.

The centrepiece fixture is `drifted`: a real database with the baseline plus
EVERY migration replayed raw and no ledger, which is the pilot's state
reproduced rather than described. `fresh_dsn` is the other half — proof that
teaching apply to refuse drift did not break a clean first deploy.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import psycopg
import pytest

from knowledge_hub import migrations as mig
from knowledge_hub.config import settings

INFRA_DIR = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = INFRA_DIR / "migrations"
BASELINE = INFRA_DIR / mig.BASELINE_SCHEMA

DRIFT_DB = "kh_ledger_drift_test"
FRESH_DB = "kh_ledger_fresh_test"


def _recreate(name: str) -> str:
    with psycopg.connect(settings.postgres_dsn, autocommit=True,
                         connect_timeout=10) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        conn.execute(f"CREATE DATABASE {name}")
    return settings.postgres_dsn.rsplit("/", 1)[0] + "/" + name


def _env_for(dbname: str) -> dict[str, str]:
    return {"POSTGRES_USER": settings.postgres_user,
            "POSTGRES_PASSWORD": settings.postgres_password,
            "POSTGRES_HOST": settings.postgres_host,
            "POSTGRES_PORT": str(settings.postgres_port),
            "POSTGRES_DB": dbname}


def _write_env(tmp_path: Path, dbname: str) -> Path:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(f"{k}={v}" for k, v in _env_for(dbname).items()),
        encoding="utf-8")
    return env_file


# ---------------------------------------------------------------------------
# Pure parsing / classification — no database
# ---------------------------------------------------------------------------
def test_parse_ignores_commented_out_ddl():
    """Every migration in this repo opens with a long -- header that discusses
    the tables it makes. A header sentence must never register as an object."""
    sql = """
    -- CREATE TABLE ghost_table (id INT);
    -- we also do not CREATE INDEX ix_ghost on anything
    CREATE TABLE real_table (id INT);
    """
    names, _ = mig.parse_created_objects(sql)
    assert names == {"real_table"}


def test_parse_captures_every_verifiable_kind():
    sql = """
    CREATE TABLE IF NOT EXISTS public.t_one (id INT);
    CREATE UNIQUE INDEX ix_one ON t_one(id);
    CREATE OR REPLACE VIEW v_one AS SELECT 1;
    ALTER TABLE t_other
        ADD COLUMN added_col BIGINT REFERENCES t_one(id);
    ALTER TABLE t_other ADD CONSTRAINT chk_x CHECK (added_col > 0);
    """
    names, unverified = mig.parse_created_objects(sql)
    assert names == {"t_one", "ix_one", "v_one", "t_other.added_col"}
    # The ADD COLUMN spans two lines (007's real shape) and is still caught.
    assert "t_other.added_col" in names
    # The constraint is NOT claimed as verified — it is reported instead.
    assert unverified == ("ADD CONSTRAINT",)


def test_discover_excludes_objects_an_earlier_file_created():
    """007 only redefines 006's view, so judging 007 on `benchmark_leaderboard`
    would call a legitimate mid-replay stop 'drift'. Its columns are the real
    evidence."""
    files = {f.filename: f for f in mig.discover(MIGRATIONS_DIR, BASELINE)}
    seven = files["007_benchmark_supersede.sql"]
    assert "benchmark_leaderboard" in seven.creates
    assert "benchmark_leaderboard" not in seven.verifiable
    assert set(seven.verifiable) == {"benchmark_runs.superseded_by_run_id",
                                     "benchmark_runs.superseded_note"}
    # Same rule against the BASELINE, not just earlier migrations: 001
    # rebuilds four objects the baseline schema already creates.
    one = files["001_persistence_addenda.sql"]
    assert {"review_queue", "facts_current",
            "ux_raw_content_hash", "ux_chunk_hash"} <= set(one.creates)
    assert not ({"review_queue", "facts_current", "ux_raw_content_hash",
                 "ux_chunk_hash"} & set(one.verifiable))


def test_every_bundled_migration_has_something_verifiable():
    """The coverage guarantee, enforced rather than assumed. A new migration
    whose objects are all redefinitions would be silently unverifiable — this
    test is the thing that says so at the time it is added."""
    files = mig.discover(MIGRATIONS_DIR, BASELINE)
    assert files, "no migrations discovered — wrong path?"
    unverifiable = [f.filename for f in files if not f.verifiable]
    assert not unverifiable, (
        f"these migrations cannot be verified by name: {unverifiable}. "
        f"Either give them a uniquely-named object or extend "
        f"parse_created_objects to cover what they do create.")


def _file(name: str, verifiable: tuple[str, ...]) -> mig.MigrationFile:
    return mig.MigrationFile(filename=name, path=Path(name),
                             creates=verifiable, verifiable=verifiable,
                             unverified=())


def _row(name: str) -> mig.LedgerRow:
    return mig.LedgerRow(filename=name, applied_at=None, note=None)


@pytest.mark.parametrize("has_row,present,missing,expected", [
    (True,  ("a", "b"), (),        mig.APPLIED),
    (False, (),         ("a", "b"), mig.PENDING),
    # The pilot's shape: DDL arrived out-of-band, ledger never written.
    (False, ("a", "b"), (),        mig.OBJECTS_NO_LEDGER),
    # The other direction: ledger claims a migration nothing backs up.
    (True,  (),         ("a", "b"), mig.LEDGER_NO_OBJECTS),
    # Half-applied, either way round — the worst case, and never replayable.
    (True,  ("a",),     ("b",),    mig.PARTIAL),
    (False, ("a",),     ("b",),    mig.PARTIAL),
])
def test_classify_states(has_row, present, missing, expected):
    f = _file("099_x.sql", tuple(present) + tuple(missing))
    ledger = {"099_x.sql": _row("099_x.sql")} if has_row else {}
    [status] = mig.classify([f], ledger, set(present))
    assert status.state == expected
    assert status.broken == (expected in mig.BROKEN_STATES)


def test_classify_marks_unverifiable_file_by_ledger_alone():
    f = _file("099_x.sql", ())
    [applied] = mig.classify([f], {"099_x.sql": _row("099_x.sql")}, set())
    [pending] = mig.classify([f], {}, set())
    assert applied.state == mig.APPLIED_UNVERIFIED
    assert not applied.broken          # unverified is not the same as broken
    assert pending.state == mig.PENDING


def test_mark_applied_demands_a_note():
    """A backfilled row that looks exactly like a replayed one is what let the
    drift hide for a day. The note is not optional."""
    for bad in ("", "   ", None):
        with pytest.raises(ValueError, match="note"):
            mig.mark_applied(None, "011_ontology_registry.sql", bad)


def test_drift_message_names_the_files_and_refuses_to_advise_ingest():
    f = _file("011_x.sql", ("ontology_active",))
    statuses = mig.classify([f], {}, {"ontology_active"})
    text = mig.drift_message(statuses)
    assert "011_x.sql" in text and mig.OBJECTS_NO_LEDGER in text
    assert "khctl migrations status" in text


# ---------------------------------------------------------------------------
# Against a real database
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def drifted_dsn() -> str:
    """The pilot's 2026-08-03 state, reproduced: baseline + every migration
    replayed raw, NO ledger row for any of them."""
    dsn = _recreate(DRIFT_DB)
    with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
        conn.execute("SET search_path = public, ag_catalog;")
        conn.execute(BASELINE.read_text(encoding="utf-8"))
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            conn.execute("SET search_path = public, ag_catalog;")
            conn.execute(path.read_text(encoding="utf-8"))
    return dsn


@pytest.fixture()
def drifted(drifted_dsn: str) -> str:
    """Each test starts from the pristine drift shape — dropping the ledger
    restores it, because the DDL itself is already (and stays) applied."""
    with psycopg.connect(drifted_dsn, autocommit=True,
                         connect_timeout=10) as conn:
        conn.execute(f"DROP TABLE IF EXISTS {mig.LEDGER_TABLE}")
    return drifted_dsn


def test_status_flags_every_migration_as_objects_without_ledger(drifted):
    with psycopg.connect(drifted, autocommit=True) as conn:
        assert not mig.ledger_exists(conn)
        statuses = mig.status(conn, MIGRATIONS_DIR, BASELINE)
    assert statuses, "no migrations classified"
    assert all(s.state == mig.OBJECTS_NO_LEDGER for s in statuses), \
        {s.filename: s.state for s in statuses}
    assert len(mig.broken(statuses)) == len(statuses)
    assert not mig.pending(statuses)


def test_phase_schema_refuses_drift_instead_of_crashing(drifted):
    """The original failure mode: replaying 011 onto its own tables raised a
    raw psycopg DuplicateTable and aborted the phase with 012/013 unreached.
    Now it is an ApplyError that says what is wrong and what to run."""
    from knowledge_hub.deploy_apply import ApplyContext, ApplyError, phase_schema

    ctx = ApplyContext(
        plan=None,                     # phase_schema reads env/infra_dir only
        infra_dir=INFRA_DIR,
        kit_dir=INFRA_DIR,
        env_file=Path(".env"),
        env=_env_for(DRIFT_DB))
    with pytest.raises(ApplyError) as excinfo:
        phase_schema(ctx)
    message = str(excinfo.value)
    assert "011_ontology_registry.sql" in message
    assert "013_reextract_scope.sql" in message
    assert "khctl migrations status" in message
    assert "DuplicateTable" not in message


def test_mark_applied_records_note_and_observed_time(drifted):
    observed = datetime(2026, 8, 3, 21, 27, 7, tzinfo=timezone.utc)
    targets = ["011_ontology_registry.sql", "012_operator_jobs.sql",
               "013_reextract_scope.sql"]
    with psycopg.connect(drifted, autocommit=True) as conn:
        for name in targets:
            assert mig.mark_applied(conn, name, "backfill: verified present",
                                    observed) is True
        # Idempotent: a second call writes nothing and says so.
        assert mig.mark_applied(conn, targets[0], "again", observed) is False
        ledger = mig.read_ledger(conn)
        statuses = {s.filename: s
                    for s in mig.status(conn, MIGRATIONS_DIR, BASELINE)}
    for name in targets:
        assert ledger[name].note == "backfill: verified present"
        assert ledger[name].applied_at == observed
        assert statuses[name].state == mig.APPLIED
    # The un-backfilled files are still broken — backfill is per file, never
    # a blanket "mark everything fine".
    assert statuses["010_operator_write.sql"].state == mig.OBJECTS_NO_LEDGER


def test_read_ledger_tolerates_the_pre_note_two_column_shape(drifted):
    """Existing deployments have the original (filename, applied_at) ledger.
    Status must read those without being a write."""
    with psycopg.connect(drifted, autocommit=True) as conn:
        conn.execute(f"CREATE TABLE {mig.LEDGER_TABLE} ("
                     " filename TEXT PRIMARY KEY,"
                     " applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
        conn.execute(f"INSERT INTO {mig.LEDGER_TABLE} (filename)"
                     " VALUES ('011_ontology_registry.sql')")
        ledger = mig.read_ledger(conn)
    assert ledger["011_ontology_registry.sql"].note is None


def test_cli_status_exits_nonzero_on_drift(drifted, tmp_path, capsys):
    """Proves the subcommand wiring and that status is genuinely read-only —
    it runs with default_transaction_read_only=on, so a stray write errors."""
    from knowledge_hub.deploy_cli import main

    env_file = _write_env(tmp_path, DRIFT_DB)
    code = main(["migrations", "status", "--infra-dir", str(INFRA_DIR),
                 "--env-file", str(env_file)])
    out = capsys.readouterr().out
    assert code == 1
    assert mig.OBJECTS_NO_LEDGER in out
    assert "011_ontology_registry.sql" in out
    # Read-only means the ledger table it warned about was NOT created.
    with psycopg.connect(drifted, autocommit=True) as conn:
        assert not mig.ledger_exists(conn)


def test_cli_mark_applied_refuses_a_file_whose_objects_are_absent(
        drifted, tmp_path, capsys):
    """mark-applied records DDL that is already there. A migration whose
    objects are missing must be replayed, never rubber-stamped."""
    from knowledge_hub.deploy_cli import main

    env_file = _write_env(tmp_path, DRIFT_DB)
    with psycopg.connect(drifted, autocommit=True) as conn:
        conn.execute("DROP TABLE operator_job_documents")
    try:
        code = main(["migrations", "mark-applied",
                     "--file", "013_reextract_scope.sql",
                     "--note", "should be refused", "--yes",
                     "--infra-dir", str(INFRA_DIR),
                     "--env-file", str(env_file)])
        out = capsys.readouterr().out
        assert code == 1
        assert "Refusing" in out
        with psycopg.connect(drifted, autocommit=True) as conn:
            assert "013_reextract_scope.sql" not in mig.read_ledger(conn)
    finally:
        # Restore the module-scoped database for the remaining tests.
        with psycopg.connect(drifted, autocommit=True) as conn:
            conn.execute("SET search_path = public, ag_catalog;")
            conn.execute((MIGRATIONS_DIR / "013_reextract_scope.sql")
                         .read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# The other half: a clean first deploy must still work
# ---------------------------------------------------------------------------
def test_fresh_apply_applies_everything_and_leaves_a_clean_ledger():
    """The regression guard for the gate. An empty database has no objects, so
    every migration is PENDING, and phase_schema must replay all of them and
    record each one."""
    from knowledge_hub.deploy_apply import ApplyContext, phase_schema

    _recreate(FRESH_DB)
    ctx = ApplyContext(plan=None, infra_dir=INFRA_DIR, kit_dir=INFRA_DIR,
                       env_file=Path(".env"), env=_env_for(FRESH_DB))
    lines = phase_schema(ctx)
    expected = len(list(MIGRATIONS_DIR.glob("*.sql")))
    assert "baseline schema applied" in lines
    assert f"migrations: {expected} applied, 0 already present" in lines

    dsn = settings.postgres_dsn.rsplit("/", 1)[0] + "/" + FRESH_DB
    with psycopg.connect(dsn, autocommit=True) as conn:
        statuses = mig.status(conn, MIGRATIONS_DIR, BASELINE)
        ledger = mig.read_ledger(conn)
    assert len(ledger) == expected
    assert not mig.broken(statuses) and not mig.pending(statuses)
    assert all(s.state in (mig.APPLIED, mig.APPLIED_UNVERIFIED)
               for s in statuses)
    # A replayed row carries no note; only a backfill does.
    assert all(r.note is None for r in ledger.values())

    # And it is idempotent: a second pass applies nothing and stays clean.
    assert f"migrations: 0 applied, {expected} already present" in \
        phase_schema(ctx)


def test_stack_alive_requires_a_nonempty_ledger(tmp_path):
    """Three states, walked in order on its own database: no ledger table, an
    EMPTY one, then a populated one. The middle case means the schema phase
    died at its first migration, and it used to read as fully deployed."""
    from knowledge_hub.deploy_launch import stack_alive

    liveness_db = "kh_ledger_liveness_test"
    dsn = _recreate(liveness_db)
    env_file = _write_env(tmp_path, liveness_db)

    assert stack_alive(env_file) is False, "no ledger table = not deployed"
    with psycopg.connect(dsn, autocommit=True) as conn:
        mig.ensure_ledger(conn)
    assert stack_alive(env_file) is False, "empty ledger = not deployed"
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(f"INSERT INTO {mig.LEDGER_TABLE} (filename)"
                     " VALUES ('001_persistence_addenda.sql')")
    assert stack_alive(env_file) is True
