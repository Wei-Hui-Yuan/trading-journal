"""Shared pytest configuration.

Puts the `api/` package root on sys.path so tests can import `services.*` the
same way the application does, without needing an installed package.
"""

import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from dotenv import load_dotenv

API_ROOT = Path(__file__).resolve().parents[1]
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

# `requires_db` below reads DATABASE_URL at collection time, before any test
# module has imported `main` (which is what loads .env normally) -- without
# this, every DB-backed test skips itself even when a database is reachable,
# because the marker's condition was evaluated too early to see it.
load_dotenv(API_ROOT / ".env")

# A handful of tests exercise real Postgres behaviour that a stub session
# cannot stand in for -- EXISTS correlation, and transaction/savepoint
# semantics that only a real asyncpg connection has. They always run inside a
# transaction that gets rolled back (see `db_transaction` below) and never
# commit past it, but they still need DATABASE_URL to reach a database at
# all. Skipped rather than failed when it is absent, so the rest of the suite
# stays runnable offline.
requires_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set"
)


@asynccontextmanager
async def db_transaction():
    """One connection, one outer transaction, always rolled back.

    Yields the raw connection; build sessions on it with `db_session` below.
    Safe even for handlers that call `session.commit()` one or more times
    internally (`ingest_ibkr` commits twice; `delete_trade` and
    `update_execution` commit once via `run_matching_for_ticker`): a session
    bound with `join_transaction_mode="create_savepoint"` turns each of those
    into a SAVEPOINT release and opens a fresh savepoint for what follows, all
    still nested inside this outer transaction. `outer.rollback()` undoes
    everything regardless of how many internal commits happened.

    Disposes `main.engine`'s pool on the way out, on EVERY path including a
    failing assertion -- not just as the last line of the function body. The
    suite has no pytest-asyncio, so each DB-backed test drives its own
    `asyncio.run(...)`, a fresh event loop torn down when that call returns.
    asyncpg connections are bound to the loop that created them; left pooled,
    a connection opened during one test's loop gets handed to the next test,
    which runs in a DIFFERENT loop, and any attempt to use or even close it
    raises "Event loop is closed" from inside SQLAlchemy's own cleanup.
    Disposing here forces the next test to open fresh connections on its own
    loop instead of inheriting a dead one.

    This was originally a plain statement after the `async with` block, which
    only ran on the success path: a failing `assert` inside the `async with
    db_transaction()` body in the CALLER is thrown into this generator at the
    `yield`, the `finally` below still runs, and then the exception continues
    propagating OUT of the `async with main.engine.connect()` block -- past
    the dispose call, which a statement merely following that block can never
    catch. One failing test then broke every DB-backed test after it in the
    same run. Wrapping the whole thing in `try/finally` is what makes dispose
    unconditional.
    """
    import main  # noqa: PLC0415 - importing main loads .env and builds the engine

    try:
        async with main.engine.connect() as conn:
            outer = await conn.begin()
            try:
                yield conn
            finally:
                await outer.rollback()
    finally:
        await main.engine.dispose()


def db_session(conn):
    """A session matching production's semantics, bound to `conn`.

    `expire_on_commit=False` matches `main.SessionLocal`. Without it, reading
    an attribute after a commit needs a lazy reload the async driver cannot do
    implicitly, and raises MissingGreenlet instead of returning the value.

    A FRESH call to this (a new session on the same `conn`) is what stands in
    for a new request getting a new session from `get_session` -- use it to
    mirror multiple ingest runs, or to check state after a session under test
    has been closed.
    """
    import main  # noqa: PLC0415

    return main.AsyncSession(
        bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False
    )
