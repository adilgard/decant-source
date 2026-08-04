"""The migration ledger — one owner for "what has actually been applied".

Before this module, `deploy_apply.phase_schema` was the ONLY code that knew
the ledger existed, and it knew exactly one thing about it: whether a
filename had a row. That single bit made a whole class of drift invisible.
The pilot DB proved it on 2026-08-03: migrations 011/012/013 had their DDL
executed out-of-band (psql, not `khctl apply`), so every object existed and
NO ledger row did. Consequences, none of which announced themselves:

  * `phase_schema` would replay 011 on the next apply and die on a raw
    `DuplicateTable` from Postgres — a wall of psycopg traceback in front of
    an operator, with 012/013 never reached.
  * `deploy_launch.stack_alive` asked only whether the ledger TABLE existed,
    so the stack reported itself live and healthy.
  * `khctl` had no way to answer "what is applied?" at all, which is why two
    progress docs could disagree with nobody able to settle it.

So the ledger gets a real model. THE RULE: the ledger is a claim, the
database is the truth, and this module compares them and refuses to guess
when they disagree.

VERIFICATION IS BY NAME, and its limits are stated out loud rather than
implied. Each file is parsed for what it creates — tables, indexes, views,
and the columns it adds via `ALTER TABLE ... ADD COLUMN` — and those names are
looked up in the live catalog. Two rules keep that honest:

  * An object another file created FIRST is not evidence about this file.
    The baseline schema creates `review_queue`; 001 and 003 and 004 each
    `CREATE OR REPLACE` it. If 003's presence were judged on `review_queue`,
    a deploy that died between 002 and 003 would look like drift instead of
    a resumable stop. So a file's verifiable set is what it creates MINUS
    everything the baseline and every earlier file create.
  * Columns are verified because dropping them from coverage would leave 007
    unverifiable — it only redefines 006's view, so its two
    `ADD COLUMN`s on benchmark_runs are the ONLY evidence it ran. Those
    ALTERs carry no IF NOT EXISTS, so a replay of an already-applied 007
    fails exactly like a duplicate CREATE TABLE. A blind spot there would be
    the same bug in a quieter place.

What is still NOT verified by name: `ADD CONSTRAINT` (003 has the only one),
and functions/triggers (this migration set has none). Those are reported per
file as a coverage note rather than silently implying full coverage. A file
left with zero verifiable names is classified on its ledger row alone and
SAYS so — no file in the current set is in that position.

The ledger table is this module's own bookkeeping, not domain schema, so it
is not itself a numbered migration: `ensure_ledger` owns its shape with
idempotent DDL, the same way `phase_schema` always created it inline.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

LEDGER_TABLE = "schema_migrations"
BASELINE_SCHEMA = "knowledge_hub_baseline_schema.sql"

# ---------------------------------------------------------------------------
# States. `broken` is the one the gates branch on — APPLIED and PENDING are
# both normal, everything else means the ledger and the database disagree and
# a human decides what happens next.
# ---------------------------------------------------------------------------
APPLIED = "APPLIED"
APPLIED_UNVERIFIED = "APPLIED?"      # ledger row, nothing verifiable by name
PENDING = "PENDING"                  # no ledger row, objects absent — will run
OBJECTS_NO_LEDGER = "BROKEN:objects-without-ledger"
LEDGER_NO_OBJECTS = "BROKEN:ledger-without-objects"
PARTIAL = "BROKEN:partial"           # some objects present, some missing

BROKEN_STATES = frozenset({OBJECTS_NO_LEDGER, LEDGER_NO_OBJECTS, PARTIAL})

# Line comments come off before parsing so a commented-out CREATE — every
# migration in this repo has an explanatory header full of them — never
# counts as a created object.
_LINE_COMMENT = re.compile(r"--[^\n]*")
_CREATE_TABLE = re.compile(
    r"\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"(?:public\.)?\"?([a-zA-Z_][a-zA-Z0-9_]*)\"?", re.I)
_CREATE_INDEX = re.compile(
    r"\bCREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?"
    r"(?:IF\s+NOT\s+EXISTS\s+)?(?:public\.)?\"?([a-zA-Z_][a-zA-Z0-9_]*)\"?",
    re.I)
_CREATE_VIEW = re.compile(
    r"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:MATERIALIZED\s+)?VIEW\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?(?:public\.)?\"?([a-zA-Z_][a-zA-Z0-9_]*)\"?",
    re.I)
# Columns land in the same namespace as objects, qualified "table.column".
# \s+ spans newlines, which this repo's migrations need — 007 puts the table
# and the ADD COLUMN on separate lines. Every ALTER in the set adds exactly
# one column, so one match per statement is complete (a comma-separated
# multi-ADD would need statement splitting; there are none, and the coverage
# note would not report the miss, so re-check this if one appears).
_ADD_COLUMN = re.compile(
    r"\bALTER\s+TABLE\s+(?:public\.)?\"?([a-zA-Z_][a-zA-Z0-9_]*)\"?\s+"
    r"ADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?\"?([a-zA-Z_][a-zA-Z0-9_]*)\"?",
    re.I)
# Present-but-not-verified constructs, reported so the coverage gap is loud.
_UNVERIFIED_KINDS = (
    ("ADD CONSTRAINT", re.compile(r"\bADD\s+CONSTRAINT\b", re.I)),
    ("FUNCTION", re.compile(r"\bCREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\b", re.I)),
    ("TRIGGER", re.compile(r"\bCREATE\s+TRIGGER\b", re.I)),
)


@dataclass(frozen=True)
class MigrationFile:
    filename: str
    path: Path
    creates: tuple[str, ...]      # every object name this file CREATEs
    verifiable: tuple[str, ...]   # creates MINUS anything earlier also creates
    unverified: tuple[str, ...]   # e.g. ("ALTER x10", "TRIGGER")


@dataclass(frozen=True)
class LedgerRow:
    filename: str
    applied_at: Optional[datetime]
    note: Optional[str]


@dataclass(frozen=True)
class MigrationStatus:
    file: MigrationFile
    state: str
    ledger: Optional[LedgerRow]
    present: tuple[str, ...]      # verifiable objects found in the DB
    missing: tuple[str, ...]      # verifiable objects NOT found

    @property
    def broken(self) -> bool:
        return self.state in BROKEN_STATES

    @property
    def filename(self) -> str:
        return self.file.filename


# ---------------------------------------------------------------------------
# Parsing (pure — unit-tested without a database)
# ---------------------------------------------------------------------------
def parse_created_objects(sql: str) -> tuple[set[str], tuple[str, ...]]:
    """Object names a SQL script CREATEs, plus the kinds it contains that we
    deliberately do not verify by name.

    Drop-then-recreate inside one file (001 on its unique indexes, 003/004/007
    on their views) is not special-cased on purpose: the file's END STATE is
    what matters, and every DROP in this repo's migrations is followed by the
    matching CREATE in the same file. A file that dropped without recreating
    would need its own handling, and there is none to write yet."""
    body = _LINE_COMMENT.sub("", sql)
    names: set[str] = set()
    for pattern in (_CREATE_TABLE, _CREATE_INDEX, _CREATE_VIEW):
        names.update(pattern.findall(body))
    names.update(f"{table}.{column}"
                 for table, column in _ADD_COLUMN.findall(body))
    unverified: list[str] = []
    for label, pattern in _UNVERIFIED_KINDS:
        hits = len(pattern.findall(body))
        if hits:
            unverified.append(f"{label} x{hits}" if hits > 1 else label)
    return names, tuple(unverified)


def discover(migrations_dir: Path,
             baseline: Optional[Path] = None) -> list[MigrationFile]:
    """The bundled migration files in replay order, each with its verifiable
    object set already narrowed against everything applied before it.

    Replay order is `sorted()` on filename — the SAME ordering phase_schema
    uses. The zero-padded 0NN prefix is what makes that correct, and it is a
    naming convention this function inherits rather than enforces.
    """
    files = sorted(migrations_dir.glob("*.sql"))
    # Seed the "already created by something earlier" set from the baseline:
    # it makes review_queue / facts_current / ux_raw_content_hash / ux_chunk_hash,
    # all of which later migrations legitimately redefine.
    seen: set[str] = set()
    if baseline is None:
        candidate = migrations_dir.parent / BASELINE_SCHEMA
        baseline = candidate if candidate.exists() else None
    if baseline is not None and baseline.exists():
        seen, _ = parse_created_objects(baseline.read_text(encoding="utf-8"))

    out: list[MigrationFile] = []
    for path in files:
        creates, unverified = parse_created_objects(
            path.read_text(encoding="utf-8"))
        verifiable = creates - seen
        seen |= creates
        out.append(MigrationFile(
            filename=path.name,
            path=path,
            creates=tuple(sorted(creates)),
            verifiable=tuple(sorted(verifiable)),
            unverified=unverified))
    return out


def classify(files: Iterable[MigrationFile],
             ledger: dict[str, LedgerRow],
             live: set[str]) -> list[MigrationStatus]:
    """Compare the ledger's claim against the database's objects, per file.

    Fail-closed in spirit: anything that is not cleanly applied-and-present
    or cleanly pending-and-absent is BROKEN and gets a human, not a guess.
    """
    out: list[MigrationStatus] = []
    for f in files:
        row = ledger.get(f.filename)
        present = tuple(n for n in f.verifiable if n in live)
        missing = tuple(n for n in f.verifiable if n not in live)
        if not f.verifiable:
            # Nothing this file creates is unique to it, so its objects can
            # neither confirm nor deny the ledger. Report the ledger's claim
            # and mark it unverified rather than inventing confidence.
            state = APPLIED_UNVERIFIED if row else PENDING
        elif row and not missing:
            state = APPLIED
        elif row and present and missing:
            state = PARTIAL
        elif row and not present:
            state = LEDGER_NO_OBJECTS
        elif not row and not present:
            state = PENDING
        elif not row and missing:
            state = PARTIAL
        else:
            state = OBJECTS_NO_LEDGER
        out.append(MigrationStatus(file=f, state=state, ledger=row,
                                   present=present, missing=missing))
    return out


def broken(statuses: Iterable[MigrationStatus]) -> list[MigrationStatus]:
    return [s for s in statuses if s.broken]


def pending(statuses: Iterable[MigrationStatus]) -> list[MigrationStatus]:
    return [s for s in statuses if s.state == PENDING]


# ---------------------------------------------------------------------------
# Database side. Reads are strictly read-only so `khctl migrations status`
# can run against a production box without being a write.
# ---------------------------------------------------------------------------
def ledger_exists(conn) -> bool:
    return bool(conn.execute(
        "SELECT count(*) FROM pg_tables WHERE schemaname='public'"
        " AND tablename=%s", (LEDGER_TABLE,)).fetchone()[0])


def ensure_ledger(conn) -> None:
    """Create/extend the ledger table. The ONLY write in this module's setup
    path, and idempotent — safe on a fresh box and on every existing
    deployment. `note` carries WHY a row exists when it was not written by a
    live replay (see mark_applied); it is added by ALTER rather than a
    numbered migration because the ledger is the migration system's own
    bookkeeping, and a migration that repairs the ledger could not run on a
    database whose ledger is the thing that is broken."""
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {LEDGER_TABLE} ("
        " filename TEXT PRIMARY KEY,"
        " applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
    conn.execute(
        f"ALTER TABLE {LEDGER_TABLE} ADD COLUMN IF NOT EXISTS note TEXT")


def read_ledger(conn) -> dict[str, LedgerRow]:
    """Ledger rows, keyed by filename. Read-only: tolerates both the
    pre-`note` two-column shape and a missing table entirely, so status can
    report on a box this module has never written to."""
    if not ledger_exists(conn):
        return {}
    has_note = bool(conn.execute(
        "SELECT count(*) FROM information_schema.columns"
        " WHERE table_schema='public' AND table_name=%s"
        " AND column_name='note'", (LEDGER_TABLE,)).fetchone()[0])
    column = "note" if has_note else "NULL::text AS note"
    rows = conn.execute(
        f"SELECT filename, applied_at, {column} FROM {LEDGER_TABLE}").fetchall()
    return {r[0]: LedgerRow(filename=r[0], applied_at=r[1], note=r[2])
            for r in rows}


def live_object_names(conn) -> set[str]:
    """Every verifiable name in the public schema: table / index / view /
    matview names, plus every column as "table.column".

    pg_class rather than information_schema for the relations because indexes
    are not in the latter's tables/views listings, and index names are a large
    share of what the migrations actually create. Columns come from
    information_schema, which is the portable place for them.
    """
    rows = conn.execute(
        "SELECT c.relname FROM pg_class c"
        " JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = 'public'"
        "   AND c.relkind IN ('r', 'p', 'i', 'I', 'v', 'm')").fetchall()
    names = {r[0] for r in rows}
    cols = conn.execute(
        "SELECT table_name, column_name FROM information_schema.columns"
        " WHERE table_schema = 'public'").fetchall()
    names.update(f"{t}.{c}" for t, c in cols)
    return names


def status(conn, migrations_dir: Path,
           baseline: Optional[Path] = None) -> list[MigrationStatus]:
    """The whole read-only comparison: files x ledger x live objects."""
    return classify(discover(migrations_dir, baseline),
                    read_ledger(conn),
                    live_object_names(conn))


def mark_applied(conn, filename: str, note: str,
                 applied_at: Optional[datetime] = None) -> bool:
    """Record a file as applied WITHOUT running it — the deliberate repair for
    DDL that reached the database out-of-band. Returns False if a row was
    already there (nothing written).

    `note` is MANDATORY and free text: a backfilled row must never be
    indistinguishable from one a live replay wrote, because that
    indistinguishability is exactly what let the 011/012/013 drift masquerade
    as normal state for a day. Pass `applied_at` when the database can
    evidence when the DDL really ran (011's `ontology_active` seed row
    timestamped its own burst); omit it and the row records now(), which is
    honest about being a reconstruction only because the note says so.
    """
    if not note or not note.strip():
        raise ValueError("mark_applied requires a note explaining why the "
                         "row is being written without a replay")
    ensure_ledger(conn)
    if applied_at is None:
        cur = conn.execute(
            f"INSERT INTO {LEDGER_TABLE} (filename, note) VALUES (%s, %s)"
            " ON CONFLICT (filename) DO NOTHING", (filename, note.strip()))
    else:
        cur = conn.execute(
            f"INSERT INTO {LEDGER_TABLE} (filename, applied_at, note)"
            " VALUES (%s, %s, %s) ON CONFLICT (filename) DO NOTHING",
            (filename, applied_at, note.strip()))
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def format_report(statuses: list[MigrationStatus]) -> list[str]:
    """Human-readable per-file lines, plus the coverage caveats. Callers add
    their own header/footer (the CLI prints a verdict, apply raises)."""
    lines: list[str] = []
    for s in statuses:
        detail = ""
        if s.state == APPLIED:
            detail = f"{len(s.present)}/{len(s.file.verifiable)} objects"
        elif s.state == APPLIED_UNVERIFIED:
            detail = "ledger row only — this file creates nothing unique to it"
        elif s.state == PENDING:
            detail = "no ledger row, objects absent — apply will run it"
        elif s.state == OBJECTS_NO_LEDGER:
            detail = (f"objects EXIST ({', '.join(s.present)}) but NO ledger "
                      f"row — DDL reached the DB outside khctl apply")
        elif s.state == LEDGER_NO_OBJECTS:
            detail = (f"ledger says applied but objects MISSING: "
                      f"{', '.join(s.missing)}")
        elif s.state == PARTIAL:
            detail = (f"HALF-APPLIED — present: {', '.join(s.present) or 'none'}"
                      f" / missing: {', '.join(s.missing)}")
        lines.append(f"  {s.state:<32} {s.filename:<34} {detail}")
        if s.ledger and s.ledger.note:
            lines.append(f"  {'':<32} {'':<34} note: {s.ledger.note}")
    caveats = [s for s in statuses if s.file.unverified]
    if caveats:
        lines.append("")
        lines.append("  coverage note — these constructs are NOT verified by "
                     "name (presence of the file's tables/indexes/views is):")
        for s in caveats:
            lines.append(f"    {s.filename:<34} {', '.join(s.file.unverified)}")
    return lines


def drift_message(statuses: list[MigrationStatus]) -> str:
    """The refusal text every gate shares, so apply, verify and the CLI say
    the same thing about the same condition."""
    bad = broken(statuses)
    parts = [f"{s.filename} [{s.state}]" for s in bad]
    return (
        f"migration ledger and database DISAGREE on "
        f"{len(bad)} migration(s): {'; '.join(parts)}. "
        "Replaying is NOT safe — a migration whose objects already exist "
        "fails on CREATE and aborts the whole schema phase. Run "
        "`khctl migrations status` for the object-by-object picture, then "
        "reconcile deliberately (`khctl migrations mark-applied` records DDL "
        "that reached the database out-of-band).")
