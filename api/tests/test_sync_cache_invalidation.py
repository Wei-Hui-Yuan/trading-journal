"""What a sync that changed nothing is allowed to invalidate.

`trades` carries the statement-level `trades_bump_data_version` trigger from
migration 030, and `data_version` is the ETag validator for the three most
expensive endpoints in the app -- `list_round_trips`, `/api/analytics/dashboard`
and `/api/analytics/advanced`. Moving it throws away every browser-cached
analytics payload and makes the next request rebuild from a full table scan.

Step 3b of the ingest refreshes the broker's realised P&L onto the ledger. It
ran unconditionally, so an ordinary no-op sync -- the same fills IBKR has
already reported, nothing new, nothing changed -- invalidated all three caches
every time. Under a daily cron that is once a day, unattended, for nothing.

Why a SELECT guard and not just a narrower UPDATE: a statement-level trigger
fires once per STATEMENT regardless of row count, including zero. Verified
directly against Postgres -- `UPDATE trades ... WHERE 1=0` reports `UPDATE 0`
and still increments the counter. Adding `IS DISTINCT FROM` to the UPDATE alone
would write fewer rows and invalidate exactly as much. The statement has to not
be issued at all, and reads fire no trigger.

Every test here needs real Postgres: statement-level triggers and IS DISTINCT
FROM have no equivalent in a stub session. They run inside a transaction that
is always rolled back (see `tests/conftest.py::db_transaction`), so nothing
here reaches the live ledger.
"""

import asyncio
import os
import uuid
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select, text

os.environ.setdefault("CORS_ALLOW_ORIGINS", "https://trading-journal-test.vercel.app")

import main  # noqa: E402
from services.ibkr_parser import ParsedExecution  # noqa: E402

from conftest import db_session, db_transaction, requires_db  # noqa: E402


def _patch_ibkr(monkeypatch, executions_by_call):
    """Feed `_run_ibkr_ingest` synthetic fills, one list per sync."""
    import services.ibkr_client as ibkr_client_mod
    import services.ibkr_parser as ibkr_parser_mod

    calls = iter(executions_by_call)

    async def fake_fetch_statements():
        return [("Q1", next(calls))], []

    monkeypatch.setattr(ibkr_client_mod, "fetch_statements", fake_fetch_statements)
    monkeypatch.setattr(ibkr_parser_mod, "parse_statement", lambda execs: execs)
    monkeypatch.setattr(ibkr_parser_mod, "count_non_tradeable", lambda _e: 0)


async def _version(conn) -> int:
    """The journal cache validator, read straight from the table."""
    return await conn.scalar(
        text("SELECT version FROM data_version WHERE scope = 'journal'")
    )


def _fill(tid, symbol, *, pnl=None, cost=None, when=None):
    return ParsedExecution(
        transaction_id=tid,
        symbol=symbol,
        quantity=Decimal("10"),
        price=Decimal("100"),
        commission=Decimal("-1"),
        execution_time=when,
        fifo_pnl_realized=pnl,
        broker_cost=cost,
    )


@requires_db
def test_a_repeat_sync_does_not_invalidate_the_cache(monkeypatch):
    """The regression. Same fills twice; the second must move nothing.

    This is the shape of every scheduled sync on a day with no trading: IBKR's
    rolling window re-reports fills already in the ledger, all of them carrying
    the same broker figures they carried yesterday.
    """
    async def scenario():
        async with db_transaction() as conn:
            now = main.datetime.now(main.timezone.utc)
            sym = f"CACHE{uuid.uuid4().hex[:4].upper()}"
            tid = f"cv-{uuid.uuid4().hex[:12]}"

            def fills():
                return [
                    _fill(tid, sym, pnl=Decimal("12.34"), cost=Decimal("990.00"),
                          when=now - timedelta(days=2))
                ]

            # --- sync 1: genuinely new work, so it SHOULD invalidate --------
            _patch_ibkr(monkeypatch, [fills(), fills()])
            before_first = await _version(conn)
            await main._run_ibkr_ingest(session=db_session(conn))
            after_first = await _version(conn)

            assert after_first > before_first, (
                "a sync that promoted a new fill must invalidate the cache -- "
                "the guard has been made too strict"
            )

            # --- sync 2: byte-identical payload, nothing to do --------------
            after_second_start = await _version(conn)
            await main._run_ibkr_ingest(session=db_session(conn))
            after_second = await _version(conn)

            assert after_second == after_second_start, (
                f"a sync that changed nothing bumped data_version "
                f"{after_second - after_second_start} time(s), discarding every "
                "cached analytics payload for no reason"
            )

    asyncio.run(scenario())


@requires_db
def test_a_rechanged_broker_figure_is_still_written(monkeypatch):
    """The guard must not cost the ledger a real correction.

    IBKR re-lots occasionally, which is the whole reason step 3b refreshes
    rather than writing once. A second sync carrying a DIFFERENT realised P&L
    for the same transaction has to land -- and, because the ledger genuinely
    changed, invalidate the cache.
    """
    async def scenario():
        async with db_transaction() as conn:
            now = main.datetime.now(main.timezone.utc)
            sym = f"RELOT{uuid.uuid4().hex[:4].upper()}"
            tid = f"cv-{uuid.uuid4().hex[:12]}"

            first = [_fill(tid, sym, pnl=Decimal("10.00"), cost=Decimal("500.00"),
                           when=now - timedelta(days=2))]
            # Same fill, re-lotted by the broker.
            second = [_fill(tid, sym, pnl=Decimal("77.77"), cost=Decimal("501.50"),
                            when=now - timedelta(days=2))]

            _patch_ibkr(monkeypatch, [first, second])
            await main._run_ibkr_ingest(session=db_session(conn))

            before = await _version(conn)
            await main._run_ibkr_ingest(session=db_session(conn))
            after = await _version(conn)

            stored = (
                await db_session(conn).execute(
                    select(
                        main.Trade.broker_realized_pnl,
                        main.Trade.broker_cost_basis,
                    ).where(main.Trade.ibkr_exec_id == f"IBKR-{tid}")
                )
            ).first()

            assert stored is not None, "the fill was never promoted"
            assert stored[0] == Decimal("77.77"), (
                f"the re-lotted P&L was not written: ledger holds {stored[0]}"
            )
            assert stored[1] == Decimal("501.50"), (
                f"the re-lotted cost basis was not written: ledger holds {stored[1]}"
            )
            assert after > before, (
                "the ledger changed, so the cache MUST be invalidated -- a stale "
                "dashboard would keep serving the old P&L"
            )

    asyncio.run(scenario())


@requires_db
def test_a_fill_carrying_no_broker_figures_writes_nothing(monkeypatch):
    """Both sides NULL is agreement, not a difference.

    IS DISTINCT FROM is load-bearing here. With a plain `!=`, `NULL != NULL`
    evaluates to NULL rather than true, so the guard would never fire -- but the
    reverse mistake matters more: a naive "always rewrite" treats two absent
    values as a change and churns the row on every sync forever.
    """
    async def scenario():
        async with db_transaction() as conn:
            now = main.datetime.now(main.timezone.utc)
            sym = f"NOFIG{uuid.uuid4().hex[:4].upper()}"
            tid = f"cv-{uuid.uuid4().hex[:12]}"

            # No fifo_pnl_realized, no broker_cost -- an ordinary opening buy.
            def fills():
                return [_fill(tid, sym, when=now - timedelta(days=2))]

            _patch_ibkr(monkeypatch, [fills(), fills()])
            await main._run_ibkr_ingest(session=db_session(conn))

            before = await _version(conn)
            await main._run_ibkr_ingest(session=db_session(conn))
            after = await _version(conn)

            assert after == before, (
                "a fill with no broker figures on either side was treated as a "
                "difference and rewritten"
            )

    asyncio.run(scenario())


@requires_db
def test_a_pnl_that_does_not_fit_the_ledger_still_settles(monkeypatch):
    """The trap the obvious version of this guard falls into.

    `ibkr_executions.fifo_pnl_realized` is NUMERIC(14,6); the ledger's
    `broker_realized_pnl` is NUMERIC(12,4). Writing one into the other rounds
    it, so a raw `t.broker_realized_pnl IS DISTINCT FROM e.fifo_pnl_realized`
    reports a difference on every row that has ever been written -- the ledger
    holds -7.4052 precisely BECAUSE it rounded staging's -7.405154, and would
    round it to the same value again.

    Caught on the real database, where 175 of 348 rows sat in exactly this
    state. A guard comparing raw values fires on every sync, writes 175 rows
    that do not change, and invalidates every cache -- while reading as though
    it were working.

    Six decimals here, deliberately, so the value cannot survive the round trip
    unrounded.
    """
    async def scenario():
        async with db_transaction() as conn:
            now = main.datetime.now(main.timezone.utc)
            sym = f"ROUND{uuid.uuid4().hex[:4].upper()}"
            tid = f"cv-{uuid.uuid4().hex[:12]}"

            def fills():
                return [
                    _fill(tid, sym, pnl=Decimal("-7.405154"),
                          cost=Decimal("-1106.91066400"),
                          when=now - timedelta(days=2))
                ]

            _patch_ibkr(monkeypatch, [fills(), fills()])
            await main._run_ibkr_ingest(session=db_session(conn))

            stored = await conn.scalar(
                text("SELECT broker_realized_pnl FROM trades "
                     f"WHERE ibkr_exec_id = 'IBKR-{tid}'")
            )
            assert stored == Decimal("-7.4052"), (
                f"expected the ledger to round to 4dp, got {stored}"
            )

            before = await _version(conn)
            await main._run_ibkr_ingest(session=db_session(conn))
            after = await _version(conn)

            assert after == before, (
                "a value the ledger rounded on the way in was read back as a "
                "disagreement and rewritten -- the guard is comparing raw "
                "values instead of what the column can actually hold"
            )

    asyncio.run(scenario())


@requires_db
def test_a_zero_row_update_still_fires_the_trigger():
    """The premise the guard rests on, asserted rather than assumed.

    If Postgres ever stopped firing statement-level triggers for zero-row
    writes, the SELECT guard would be unnecessary complexity and this test is
    where that shows up. It is also the cheapest possible demonstration of why
    `WHERE ... IS DISTINCT FROM` alone would not have been enough.
    """
    async def scenario():
        async with db_transaction() as conn:
            before = await _version(conn)
            await conn.execute(
                text("UPDATE trades SET broker_realized_pnl = broker_realized_pnl "
                     "WHERE 1 = 0")
            )
            after = await _version(conn)

            assert after > before, (
                "a zero-row UPDATE no longer bumps data_version -- the SELECT "
                "guard in step 3b can be simplified to a WHERE clause"
            )

    asyncio.run(scenario())
