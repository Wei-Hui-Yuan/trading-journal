"""Apply pending SQL migrations, exactly once each, in a deterministic order.

WHY THIS EXISTS. Migrations were applied by hand, and nothing recorded which
ones had been. That is survivable while the sequence is short and recent, and
stops being survivable at exactly the moment it matters: rebuilding this
database from scratch after a loss, or standing up a second environment, with
31 files and no way to tell which of them the target has already seen.

DELIBERATELY STANDALONE. This imports nothing from `main`. A migration runner
that depends on the application cannot run when the application will not import
-- which, if the schema is the thing that is broken, is precisely when it is
needed. The database URL is rebuilt here rather than imported for the same
reason; the duplication is small, and the alternative is a runner that dies
with the app.

CONNECT DIRECTLY, NOT THROUGH THE TRANSACTION POOLER. These statements are DDL.
Supabase's transaction-mode pooler (PgBouncer, port 6543) does not support it
reliably -- the same constraint that stops `Base.metadata.create_all` from
running at startup, documented at length in main.py's lifespan. Point
DATABASE_URL at the session pooler or the direct connection (port 5432) when
running this. It will warn if it sees 6543.

USAGE

    python migrate.py --dry-run     # what would run, in what order
    python migrate.py               # apply everything pending
    python migrate.py --baseline    # adopt an already-migrated database
    python migrate.py --status      # what is applied, what drifted

ADOPTING AN EXISTING DATABASE. The production database already has every one of
these applied, and re-running them would range from wasteful to destructive. Run
`--baseline` against it ONCE: that records every migration currently on disk as
applied, without executing any of them. From then on only genuinely new files
run. A fresh, empty database needs no baseline -- just run it.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence

import asyncpg
from dotenv import load_dotenv

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

TRACKING_TABLE = "schema_migrations"

# Two prefixes were used twice before tracking existed: 021 and 022 each name
# two different migrations. Renaming them was considered and rejected -- an
# applied migration's filename is its identity, the same rule Rails, Django,
# Alembic and Flyway all keep, and roughly a hundred comments across the
# codebase cite these numbers. They are recorded here as historical fact and
# allowed through.
#
# Only these four. A NEW file sharing a prefix with anything is an error, which
# is what stops this from happening again. The four are provably safe to leave:
# they touch disjoint objects (two indexes on position_fills, a new
# timeframe_presets table, a column on positions, a new realized_legs table), so
# no ordering between them changes the result.
GRANDFATHERED_DUPLICATES = frozenset(
    {
        "021_drop_redundant_position_fill_indexes.sql",
        "021_timeframe_presets.sql",
        "022_positions_direction.sql",
        "022_realized_legs.sql",
    }
)

# A migration whose first meaningful statement is `BEGIN;` runs its own
# transaction and must not be wrapped in a second one. Postgres treats a nested
# BEGIN as a no-op with a warning, and the file's COMMIT would then close the
# runner's transaction early -- committing the migration while the row recording
# it was still unwritten.
#
# Anchored and requiring the semicolon, which is what separates a SQL
# transaction from the PL/pgSQL `BEGIN` that opens a DO block. The latter never
# carries one.
_OWN_TRANSACTION = re.compile(r"^\s*BEGIN\s*;", re.IGNORECASE | re.MULTILINE)

# Escape hatch for a future migration that cannot run inside a transaction at
# all -- CREATE INDEX CONCURRENTLY being the usual reason. None of the current
# 31 need it.
_NO_TRANSACTION_MARKER = "-- migrate: no-transaction"


_LINE_COMMENT = re.compile(r"--[^\n]*")


class MigrationError(RuntimeError):
    """Anything that should stop the run before the database is touched."""


def strip_comments(sql: str) -> str:
    """SQL with `--` comments removed, FOR INSPECTION ONLY.

    These files carry more prose than statements -- 021 spends thirty lines
    explaining why it drops two indexes, and mentions DROP INDEX CONCURRENTLY
    while doing so. Anything that asks "does this migration do X" has to read
    what it executes, not what it says about itself, or a discussion of a
    technique counts as a use of it.

    Crude on purpose: a `--` inside a string literal is stripped too. That is
    harmless here because the result is only ever pattern-matched, never
    executed. `apply` always runs the original text.
    """
    return _LINE_COMMENT.sub("", sql)


@dataclass(frozen=True)
class Migration:
    filename: str
    sql: str
    checksum: str

    @property
    def statements(self) -> str:
        """What this migration actually executes, minus the commentary."""
        return strip_comments(self.sql)

    @property
    def manages_own_transaction(self) -> bool:
        return bool(_OWN_TRANSACTION.search(self.statements))

    @property
    def opts_out_of_transaction(self) -> bool:
        # Read against the raw text: the marker IS a comment, so stripping
        # comments first would remove the thing being looked for.
        return _NO_TRANSACTION_MARKER in self.sql


# ---------------------------------------------------------------------------
# Pure logic -- no database, and unit-tested in tests/test_migrate.py
# ---------------------------------------------------------------------------


def normalise(text: str) -> str:
    """Content with line endings flattened, for checksumming.

    Without this the checksum is a property of whoever checked the file out.
    Git converts LF to CRLF on Windows, so the same commit hashes differently on
    a dev machine than in the Linux container, and every migration would look
    like it had been edited since it was applied.
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")


def checksum(text: str) -> str:
    return hashlib.sha256(normalise(text).encode("utf-8")).hexdigest()


def sort_key(filename: str) -> tuple[int, str]:
    """Numeric prefix first, then the whole name to break the known ties.

    Sorting by string alone would be deterministic but wrong the moment the
    sequence reaches three digits of a different width; sorting by prefix alone
    is ambiguous for 021 and 022. Both together are total and stable.
    """
    match = re.match(r"^(\d+)", filename)
    if not match:
        raise MigrationError(
            f"{filename}: migrations must start with a numeric prefix, "
            "so the order they apply in is not a matter of opinion."
        )
    return (int(match.group(1)), filename)


def check_prefix_collisions(filenames: Sequence[str]) -> None:
    """Refuse to run when two migrations share a number.

    Grandfathered pairs pass. Anything else is a genuine ambiguity about what
    ran before what, and the right time to find out is before touching the
    database rather than after two environments have diverged.
    """
    by_prefix: dict[str, list[str]] = {}
    for name in filenames:
        by_prefix.setdefault(name[:3], []).append(name)

    for prefix, names in sorted(by_prefix.items()):
        if len(names) == 1:
            continue
        if set(names) <= GRANDFATHERED_DUPLICATES:
            continue
        listed = "\n  ".join(sorted(names))
        raise MigrationError(
            f"Migration prefix {prefix} is used by more than one file:\n"
            f"  {listed}\n\n"
            "Two migrations sharing a number have no defined order. Renumber "
            "the new one to the next free prefix. (The 021 and 022 pairs "
            "predate this check and are recorded as known exceptions in "
            "GRANDFATHERED_DUPLICATES; do not add to that list.)"
        )


def load_migrations(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    """Every migration on disk, in the order it must be applied."""
    if not directory.is_dir():
        raise MigrationError(f"No migrations directory at {directory}")

    paths = sorted(directory.glob("*.sql"), key=lambda p: sort_key(p.name))
    check_prefix_collisions([p.name for p in paths])

    migrations = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        migrations.append(
            Migration(filename=path.name, sql=text, checksum=checksum(text))
        )
    return migrations


def pending(
    migrations: Iterable[Migration], applied: Iterable[str]
) -> list[Migration]:
    """Those not yet recorded, in order. Keyed on filename, never on number."""
    seen = set(applied)
    return [m for m in migrations if m.filename not in seen]


def drifted(
    migrations: Iterable[Migration], applied: dict[str, Optional[str]]
) -> list[str]:
    """Applied migrations whose file no longer matches what was recorded.

    Editing a migration that has already run is silent by nature: the database
    keeps the old shape while the file describes a new one, and every fresh
    environment built from that file diverges from production. This is the only
    way to notice.

    A NULL recorded checksum means the row came from `--baseline` -- adopted
    from a database that was migrated by hand, where there is nothing truthful
    to compare against. Skipped rather than guessed at.
    """
    out = []
    for migration in migrations:
        recorded = applied.get(migration.filename)
        if recorded is not None and recorded != migration.checksum:
            out.append(migration.filename)
    return out


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


def database_url() -> str:
    """The same URL the app builds, in the form asyncpg wants directly.

    Rebuilt rather than imported from main -- see the module docstring. The
    `+asyncpg` dialect suffix is SQLAlchemy's; asyncpg.connect rejects it.
    """
    url = os.environ.get("DATABASE_URL")
    if not url:
        try:
            user = os.environ["SUPABASE_DB_USER"]
            password = os.environ["SUPABASE_DB_PASSWORD"]
            host = os.environ["SUPABASE_DB_HOST"]
        except KeyError as exc:
            raise MigrationError(
                "Set DATABASE_URL, or SUPABASE_DB_USER / SUPABASE_DB_PASSWORD / "
                f"SUPABASE_DB_HOST. Missing: {exc.args[0]}"
            ) from exc
        port = os.environ.get("SUPABASE_DB_PORT", "5432")
        name = os.environ.get("SUPABASE_DB_NAME", "postgres")
        url = f"postgresql://{user}:{password}@{host}:{port}/{name}"

    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def ensure_tracking_table(conn: asyncpg.Connection) -> None:
    """Create the ledger if it is not there.

    Owned by the runner rather than by a migration file, which would otherwise
    need the table to exist in order to record that it had created it. Every
    migration tool resolves this the same way.

    `filename` is the primary key, not the number: it is the one identifier that
    stays unique across the 021 and 022 collisions, and the one that never
    changes.
    """
    await conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {TRACKING_TABLE} (
            filename     TEXT PRIMARY KEY,
            checksum     TEXT,
            applied_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            duration_ms  INTEGER
        );

        COMMENT ON TABLE {TRACKING_TABLE} IS
            'Which files in api/migrations have been applied. Written by '
            'api/migrate.py. A NULL checksum marks a row adopted by --baseline '
            'from a database migrated by hand, where the content that actually '
            'ran was never recorded and cannot be verified.';
        """
    )


async def fetch_applied(conn: asyncpg.Connection) -> dict[str, Optional[str]]:
    rows = await conn.fetch(f"SELECT filename, checksum FROM {TRACKING_TABLE}")
    return {row["filename"]: row["checksum"] for row in rows}


async def record(
    conn: asyncpg.Connection, migration: Migration, duration_ms: int
) -> None:
    await conn.execute(
        f"""
        INSERT INTO {TRACKING_TABLE} (filename, checksum, duration_ms)
        VALUES ($1, $2, $3)
        ON CONFLICT (filename) DO UPDATE
            SET checksum = EXCLUDED.checksum,
                applied_at = NOW(),
                duration_ms = EXCLUDED.duration_ms
        """,
        migration.filename,
        migration.checksum,
        duration_ms,
    )


async def apply(conn: asyncpg.Connection, migration: Migration) -> int:
    """Run one migration and record it. Returns elapsed milliseconds.

    Three cases, because the existing files are not consistent about it:

    - Opts out explicitly: run as-is, record separately. For CREATE INDEX
      CONCURRENTLY and friends, which cannot be in a transaction at all.
    - Manages its own transaction (11 of the 31 open with `BEGIN;`): run as-is
      and record after it commits. The record is a separate statement, so a
      crash in the gap between the two leaves the migration applied but
      unrecorded -- the next run would re-apply it. Survivable because these
      files are written idempotently (IF NOT EXISTS / IF EXISTS throughout), and
      the error message names the file so it can be marked by hand.
    - Everything else (20 files, no transaction of their own): wrapped here, with
      the bookkeeping INSERT inside the same transaction. Applied-and-recorded
      is then a single atomic step, which is the behaviour to prefer for
      anything written from now on.
    """
    started = time.perf_counter()

    if migration.opts_out_of_transaction or migration.manages_own_transaction:
        await conn.execute(migration.sql)
        elapsed = int((time.perf_counter() - started) * 1000)
        await record(conn, migration, elapsed)
        return elapsed

    async with conn.transaction():
        await conn.execute(migration.sql)
        elapsed = int((time.perf_counter() - started) * 1000)
        await record(conn, migration, elapsed)
    return elapsed


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


async def run(args: argparse.Namespace) -> int:
    migrations = load_migrations()

    url = database_url()
    if ":6543" in url and not args.allow_pooler:
        print(
            "Refusing to run DDL through what looks like Supabase's "
            "transaction-mode pooler (port 6543), which does not reliably "
            "support it. Use the session pooler or direct connection on 5432, "
            "or pass --allow-pooler if you know better.",
            file=sys.stderr,
        )
        return 2

    # statement_cache_size=0 for the same reason the app sets it: prepared
    # statement caching is incompatible with connection poolers.
    conn = await asyncpg.connect(url, statement_cache_size=0)
    try:
        await ensure_tracking_table(conn)
        applied = await fetch_applied(conn)

        stale = drifted(migrations, applied)
        outstanding = pending(migrations, applied)

        # The two read-only commands come FIRST, deliberately ahead of the drift
        # gate below. Drift is the condition you would reach for --status to
        # investigate, and a gate that refused to report it would leave the only
        # diagnostics in the tool unusable exactly when they are needed.
        if args.status:
            print(f"{len(applied)} applied, {len(outstanding)} pending\n")
            for migration in migrations:
                if migration.filename in applied:
                    mark = "." if applied[migration.filename] else "~"
                else:
                    mark = "+"
                flag = "  <- file changed since it was applied" if migration.filename in stale else ""
                print(f"  {mark} {migration.filename}{flag}")
            print("\n  . applied   ~ adopted by --baseline   + pending")
            if stale:
                print(f"\n{len(stale)} migration(s) have changed since being applied.")
            return 0

        if args.dry_run:
            if stale:
                print("Changed since being applied (would block a real run):")
                for name in stale:
                    print(f"  {name}")
                print()
            if not outstanding:
                print("Up to date; nothing would run.")
                return 0
            print(f"{len(outstanding)} migration(s) would run, in this order:")
            for migration in outstanding:
                how = (
                    "no transaction"
                    if migration.opts_out_of_transaction
                    else "own transaction"
                    if migration.manages_own_transaction
                    else "wrapped"
                )
                print(f"  {migration.filename}  ({how})")
            return 0

        if args.baseline:
            if not outstanding:
                print("Nothing to adopt; every migration is already recorded.")
                return 0
            print(f"Adopting {len(outstanding)} migration(s) WITHOUT running them:")
            for migration in outstanding:
                print(f"  {migration.filename}")
                # NULL checksum: this records that the file ran at some point,
                # which is true, without claiming to know that what ran matches
                # what is on disk today. It did not necessarily.
                await conn.execute(
                    f"INSERT INTO {TRACKING_TABLE} (filename, checksum) "
                    "VALUES ($1, NULL) ON CONFLICT (filename) DO NOTHING",
                    migration.filename,
                )
            print("\nBaselined. Only genuinely new migrations will run from here.")
            return 0

        # Gates the real run only. Editing a migration that has already been
        # applied leaves the database on the old shape while every environment
        # built fresh from the file gets the new one -- a divergence that is
        # invisible until the two are compared. The fix is a new migration, not
        # an edit, so this stops rather than warns.
        if stale and not args.ignore_drift:
            print("Applied migrations whose files have since changed:", file=sys.stderr)
            for name in stale:
                print(f"  {name}", file=sys.stderr)
            print(
                "\nThe database has the old shape; the file describes a new one. "
                "Write a new migration for the change instead of editing one that "
                "has already run. `--status` shows the full picture; "
                "`--ignore-drift` proceeds anyway.",
                file=sys.stderr,
            )
            return 3

        if not outstanding:
            print("Up to date; nothing to apply.")
            return 0

        for migration in outstanding:
            print(f"applying {migration.filename} ...", end=" ", flush=True)
            try:
                elapsed = await apply(conn, migration)
            except Exception as exc:
                print("FAILED")
                print(f"\n{migration.filename}: {exc}", file=sys.stderr)
                print(
                    "\nStopped here. Migrations before this one are applied and "
                    "recorded; this one and everything after it are not.",
                    file=sys.stderr,
                )
                return 1
            print(f"ok ({elapsed} ms)")

        print(f"\nApplied {len(outstanding)} migration(s).")
        return 0
    finally:
        await conn.close()


def main(argv: Optional[Sequence[str]] = None) -> int:
    load_dotenv(Path(__file__).resolve().parent / ".env")

    parser = argparse.ArgumentParser(
        description="Apply pending SQL migrations exactly once each.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="list what would run, in order, without touching the schema",
    )
    mode.add_argument(
        "--status",
        action="store_true",
        help="show which migrations are applied, adopted, or pending",
    )
    mode.add_argument(
        "--baseline",
        action="store_true",
        help="record every migration as applied WITHOUT running it, to adopt a "
        "database that was migrated by hand. Run once, on an existing database.",
    )
    parser.add_argument(
        "--ignore-drift",
        action="store_true",
        help="proceed even though an already-applied migration file has changed",
    )
    parser.add_argument(
        "--allow-pooler",
        action="store_true",
        help="permit connecting on port 6543 despite its DDL limitations",
    )
    args = parser.parse_args(argv)

    try:
        return asyncio.run(run(args))
    except MigrationError as exc:
        print(f"{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
