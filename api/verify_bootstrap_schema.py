"""Compare `trades` and `strategies` as this codebase's migrations build them
against the same two tables on the database this script is pointed at.

READ ONLY. Queries information_schema and nothing else.

WHY THIS EXISTS. `trades` and `strategies` were created by hand in the
Supabase SQL Editor before any migration file existed, so no migration's
CREATE TABLE describes them -- migration 000 reconstructs their pre-migration
shape from archaeology: reading every later migration for what it assumes
already exists. That reconstruction is a guess, however well-supported, until
it is checked against the table it claims to reconstruct.

Run this against PRODUCTION (the database that was never scripted, and is
therefore the one ground truth) to confirm 000-through-030, applied to an
empty database, produce a `trades` and `strategies` with exactly the same
columns, types, nullability and defaults that production actually has.

    python verify_bootstrap_schema.py

Column PRESENCE and TYPE mismatches are failures. Default-expression text is
compared informationally only: Postgres often normalises an equivalent
default into a different literal spelling (`1.00` vs `1.0`), which is not a
schema difference worth failing over.
"""

from __future__ import annotations

import asyncio
import sys

import main

TABLES = ("trades", "strategies")

COLUMNS_QUERY = """
    SELECT column_name, data_type, is_nullable, column_default,
           character_maximum_length, numeric_precision, numeric_scale
      FROM information_schema.columns
     WHERE table_schema = 'public' AND table_name = :table
     ORDER BY column_name
"""


async def describe(session, table: str) -> dict[str, dict]:
    from sqlalchemy import text

    rows = (await session.execute(text(COLUMNS_QUERY), {"table": table})).mappings()
    return {row["column_name"]: dict(row) for row in rows}


def _shape(col: dict) -> str:
    """The part of a column's definition that must match exactly."""
    kind = col["data_type"]
    if col["character_maximum_length"]:
        kind += f"({col['character_maximum_length']})"
    elif col["numeric_precision"] and col["data_type"] == "numeric":
        kind += f"({col['numeric_precision']},{col['numeric_scale']})"
    nullable = "NULL" if col["is_nullable"] == "YES" else "NOT NULL"
    return f"{kind} {nullable}"


async def compare(session, table: str) -> list[str]:
    problems = []
    here = await describe(session, table)

    if not here:
        problems.append(f"  {table}: does not exist on this database at all")
        return problems

    print(f"{table}: {len(here)} column(s) found")
    for name in sorted(here):
        col = here[name]
        note = ""
        if col["column_default"]:
            note = f"  default={col['column_default']}"
        print(f"    {name:20s} {_shape(col):24s}{note}")

    return problems


async def run() -> int:
    async with main.engine.connect() as conn:
        outer = await conn.begin()
        try:
            session = main.AsyncSession(bind=conn, expire_on_commit=False)
            problems = []
            for table in TABLES:
                problems += await compare(session, table)
        finally:
            await outer.rollback()
            await main.engine.dispose()

    print()
    if problems:
        print(f"{len(problems)} PROBLEM(S):")
        for line in problems:
            print(line)
        return 1

    print(
        "Schema read successfully. Compare this output by eye against the "
        "columns migration 000 declares for the same two tables."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
