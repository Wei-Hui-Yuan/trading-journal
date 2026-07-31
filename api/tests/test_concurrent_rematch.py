"""Two matchers on one ticker must not overwrite each other.

THE BUG. `run_matching_for_ticker` is a read-modify-write across three tables:
it reads every fill for the ticker, decides which stored round trips FIFO no
longer produces, deletes those, and replaces `realized_legs` for the symbol
outright. Nothing about that was atomic against a SECOND matcher.

The failure was NOT a visible gap -- the delete and the re-insert share one
commit, so no reader can ever observe positions deleted-but-not-reinserted.
It was a lost update, which is worse: silent and durable.

Measured on real concurrent transactions before the fix:

    start:   one round trip, +100 over 10 shares
    B reads the ledger (2 fills)
    A lands a backdated repair fill, re-matches -> +400 over 20 shares
    B finishes on its stale snapshot, deletes A's row, writes its own back
    FINAL:   +100 over 10 shares

$300 of realised P&L gone, A's fills demoted to unmatched open exposure, and
nothing raised. `realized_legs` -- where every windowed money figure comes
from -- was replaced wholesale by the stale version.

Two concurrent matchers are not exotic here: a broker sync runs for up to
ibkr_client.TOTAL_BUDGET_SECONDS, and fills can be edited, deleted or repaired
from the journal throughout that window.

These tests use REAL independent transactions, because that is the only thing
that can observe the property. Everything is scoped to one synthetic ticker and
removed in a finally.
"""

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

os.environ.setdefault("CORS_ALLOW_ORIGINS", "https://trading-journal-test.vercel.app")

import main  # noqa: E402
from sqlalchemy import delete, select  # noqa: E402
from services import matching_engine as me  # noqa: E402

from conftest import requires_db  # noqa: E402

T0 = datetime.now(timezone.utc) - timedelta(days=5)


def _fill(ticker, direction, qty, price, hours):
    return main.Trade(
        id=uuid.uuid4(),
        ibkr_exec_id=f"REPAIR-{uuid.uuid4()}",
        ticker=ticker, direction=direction, style="Unclassified",
        quantity=Decimal(qty), actual_entry=Decimal(price),
        entry_date=T0 + timedelta(hours=hours), commission=Decimal("0"),
        source_tag="Repair",
    )


async def _positions(ticker):
    """What a separate request would see. Its own session, its own snapshot."""
    async with main.SessionLocal() as reader:
        return sorted(
            (await reader.execute(
                select(main.Position.realized_pnl, main.Position.quantity)
                .where(main.Position.symbol == ticker)
            )).all()
        )


async def _purge(ticker):
    """Remove the synthetic ticker, and CHECK that it went.

    These tests commit for real -- that is the only way to observe a
    cross-transaction property -- so this is the only thing standing between a
    fixture and the user's actual P&L. An earlier draft drove each phase on its
    own `asyncio.run`, and the purge died on a connection bound to an
    already-closed event loop: the delete never ran, and two synthetic round
    trips worth +400 each were left sitting in `positions`, counted by the
    dashboard. `run_isolated` keeps everything on one loop now; this verifies
    the outcome rather than trusting it, because a cleanup that fails silently
    is worse than no cleanup at all.

    Positions before trades: `positions.open_trade_id` is NOT NULL with
    ON DELETE SET NULL, so removing a trade still referenced by a position
    fails rather than orphaning it.
    """
    async with main.SessionLocal() as s:
        await s.execute(delete(main.RealizedLeg).where(main.RealizedLeg.symbol == ticker))
        await s.execute(delete(main.Position).where(main.Position.symbol == ticker))
        await s.execute(delete(main.Trade).where(main.Trade.ticker == ticker))
        await s.commit()

        left = (
            len((await s.execute(
                select(main.Trade.id).where(main.Trade.ticker == ticker)
            )).all()),
            len((await s.execute(
                select(main.Position.id).where(main.Position.symbol == ticker)
            )).all()),
            len((await s.execute(
                select(main.RealizedLeg.id).where(main.RealizedLeg.symbol == ticker)
            )).all()),
        )
    assert left == (0, 0, 0), (
        f"test fixture {ticker} survived cleanup as {left} "
        "(trades, positions, legs) and is now polluting the real ledger"
    )


async def _seed(ticker):
    """BUY 10 @ 100 then SELL 10 @ 110 -> one closed round trip, +100."""
    async with main.SessionLocal() as setup:
        setup.add_all([
            _fill(ticker, "BUY", "10", "100", 0),
            _fill(ticker, "SELL", "10", "110", 1),
        ])
        await setup.commit()
        await me.run_matching_for_ticker(setup, ticker, persist=True)


def run_isolated(body, ticker=None):
    """Drive one coroutine, then purge and dispose -- all on ONE event loop.

    The suite has no pytest-asyncio, so each test drives its own
    `asyncio.run(...)` and the loop is torn down when that call returns.
    asyncpg connections are bound to the loop that opened them, so splitting
    the scenario, the cleanup and the dispose across separate `asyncio.run`
    calls hands a dead connection to the next one and raises "Event loop is
    closed" from inside SQLAlchemy's own teardown. Same reasoning as
    conftest.db_transaction, which cannot be reused here: these tests need
    genuinely independent transactions, which a single shared connection
    wrapped in one outer transaction cannot provide.
    """
    async def wrapper():
        try:
            await body()
        finally:
            if ticker is not None:
                await _purge(ticker)
            await main.engine.dispose()

    asyncio.run(wrapper())


@requires_db
def test_a_stale_matcher_cannot_overwrite_a_fresher_one():
    """The regression, replayed exactly.

    B reads before A's repair fill lands and writes after. Without the ticker
    lock B's stale snapshot won that race; with it, B cannot start until A has
    committed, so B reads the corrected ledger and agrees with it.
    """
    ticker = f"ZZ{uuid.uuid4().hex[:6].upper()}"[:10]

    async def scenario():
        await _seed(ticker)
        assert await _positions(ticker) == [(Decimal("100.0000"), Decimal("10.00000000"))]

        # Hold the FIRST matcher between its read and its writes -- the exact
        # window the race lives in. Without this the whole rebuild finishes in
        # a few milliseconds and the two never actually overlap, which is how a
        # test like this passes against the unfixed code and proves nothing.
        gate = asyncio.Event()
        arrived = asyncio.Event()
        real_load = me.load_executions_for_ticker
        calls = {"n": 0}

        async def load_then_wait(session, tkr, **kwargs):
            executions = await real_load(session, tkr, **kwargs)
            calls["n"] += 1
            if calls["n"] == 1:
                arrived.set()
                await gate.wait()
            return executions

        me.load_executions_for_ticker = load_then_wait
        session_b = main.SessionLocal()
        session_a = main.SessionLocal()
        try:
            # B (a sync) reads the ledger -- two fills -- and stops there.
            b_task = asyncio.create_task(
                me.run_matching_for_ticker(session_b, ticker, persist=True)
            )
            await asyncio.wait_for(arrived.wait(), timeout=30)

            # A (a repair fill) backdates a buy and adds the sell that takes the
            # ticker flat again. FIFO now pairs the 90 lot against the 110 sell
            # and the 100 lot against the 120: +400 over 20 shares.
            session_a.add_all([
                _fill(ticker, "BUY", "10", "90", -1),
                _fill(ticker, "SELL", "10", "120", 2),
            ])
            await session_a.commit()
            a_task = asyncio.create_task(
                me.run_matching_for_ticker(session_a, ticker, persist=True)
            )
            await asyncio.sleep(0.4)

            # Unfixed, A sails past here and commits +400. Then B wakes on its
            # stale two-fill snapshot, deletes A's row and writes +100 back.
            # Fixed, A is still parked on the ticker lock B is holding.
            gate.set()
            await asyncio.wait_for(asyncio.gather(b_task, a_task), timeout=60)
            await session_b.commit()
            await session_a.commit()
        finally:
            me.load_executions_for_ticker = real_load
            await session_a.close()
            await session_b.close()

        # Four fills, flat to flat, is +400 over 20 shares. The +100 below is
        # the signature of a stale two-fill snapshot winning the race.
        assert await _positions(ticker) == [(Decimal("400.0000"), Decimal("20.00000000"))]

    run_isolated(scenario, ticker)


@requires_db
def test_control_the_same_interleaving_loses_the_update_without_the_lock():
    """The control, and the reason the test above is worth having.

    Same scenario with `lock_ticker` neutered, reproducing the unfixed
    behaviour: A's corrected +400 is committed and then overwritten by B's
    stale +100. Without this, a passing test above would not distinguish "the
    lock works" from "the interleaving never actually happened" -- which is
    exactly what the first draft of it did, because a rebuild finishes in
    milliseconds and the two matchers never overlapped at all.
    """
    ticker = f"ZZ{uuid.uuid4().hex[:6].upper()}"[:10]

    async def scenario():
        await _seed(ticker)

        gate = asyncio.Event()
        arrived = asyncio.Event()
        real_load = me.load_executions_for_ticker
        real_lock = me.lock_ticker
        calls = {"n": 0}

        async def load_then_wait(session, tkr, **kwargs):
            executions = await real_load(session, tkr, **kwargs)
            calls["n"] += 1
            if calls["n"] == 1:
                arrived.set()
                await gate.wait()
            return executions

        async def no_lock(session, tkr):
            return None

        me.load_executions_for_ticker = load_then_wait
        me.lock_ticker = no_lock
        session_b = main.SessionLocal()
        session_a = main.SessionLocal()
        try:
            b_task = asyncio.create_task(
                me.run_matching_for_ticker(session_b, ticker, persist=True)
            )
            await asyncio.wait_for(arrived.wait(), timeout=30)

            session_a.add_all([
                _fill(ticker, "BUY", "10", "90", -1),
                _fill(ticker, "SELL", "10", "120", 2),
            ])
            await session_a.commit()
            a_task = asyncio.create_task(
                me.run_matching_for_ticker(session_a, ticker, persist=True)
            )
            await asyncio.sleep(0.4)

            # Nothing is holding A back now, so it commits the correct answer
            # before B has written anything.
            assert await _positions(ticker) == [
                (Decimal("400.0000"), Decimal("20.00000000"))
            ], "A should have landed its correction while B was parked"

            gate.set()
            await asyncio.wait_for(asyncio.gather(b_task, a_task), timeout=60)
            await session_b.commit()
            await session_a.commit()
        finally:
            me.load_executions_for_ticker = real_load
            me.lock_ticker = real_lock
            await session_a.close()
            await session_b.close()

        # B's stale snapshot won. $300 of realised P&L gone, silently.
        assert await _positions(ticker) == [
            (Decimal("100.0000"), Decimal("10.00000000"))
        ], "the probe can no longer observe the bug it was written for"

    run_isolated(scenario, ticker)


@requires_db
def test_no_reader_ever_sees_the_rebuild_half_done():
    """The failure mode this was FIRST suspected to be, pinned as a non-issue.

    The stale-delete and the re-insert share one commit, so MVCC keeps the
    intermediate state private to the writing transaction. A reader sees the
    old round trip or the new one, never neither.
    """
    ticker = f"ZZ{uuid.uuid4().hex[:6].upper()}"[:10]
    seen = {}
    real_insert = me.insert_positions

    async def spy(session, positions):
        # Between the stale DELETE and the re-INSERT, inside the matcher's
        # still-uncommitted transaction.
        seen["mid_rebuild"] = await _positions(ticker)
        return await real_insert(session, positions)

    async def scenario():
        await _seed(ticker)
        me.insert_positions = spy
        try:
            async with main.SessionLocal() as a:
                a.add_all([
                    _fill(ticker, "BUY", "10", "90", -1),
                    _fill(ticker, "SELL", "10", "120", 2),
                ])
                await a.commit()
                await me.run_matching_for_ticker(a, ticker, persist=True)
        finally:
            me.insert_positions = real_insert

        assert "mid_rebuild" in seen, "no rebuild happened; the fixture is wrong"
        assert seen["mid_rebuild"] == [(Decimal("100.0000"), Decimal("10.00000000"))], (
            "a concurrent reader saw the rebuild half applied"
        )
        assert await _positions(ticker) == [(Decimal("400.0000"), Decimal("20.00000000"))]

    run_isolated(scenario, ticker)


@requires_db
def test_a_preview_takes_no_lock():
    """persist=False writes nothing, so it has no business making a sync wait.

    Proven by holding the ticker lock in another transaction and checking the
    preview still completes rather than blocking on it.
    """
    ticker = f"ZZ{uuid.uuid4().hex[:6].upper()}"[:10]

    async def scenario():
        await _seed(ticker)
        holder = main.SessionLocal()
        try:
            await me.lock_ticker(holder, ticker)
            async with main.SessionLocal() as reader:
                result = await asyncio.wait_for(
                    me.run_matching_for_ticker(reader, ticker, persist=False),
                    timeout=10,
                )
            assert len(result.positions) == 1
            await holder.commit()
        finally:
            await holder.close()

    run_isolated(scenario, ticker)


@requires_db
def test_the_lock_is_released_by_the_transaction_not_the_session():
    """Supabase is reached through the transaction-mode pooler on 6543, which
    hands the connection to a different client at COMMIT. A session-scoped
    `pg_advisory_lock` taken there would never be released and would wedge the
    pool; `pg_advisory_xact_lock` is released server-side. This is what makes
    the choice safe, so it is pinned rather than assumed."""
    ticker = f"ZZ{uuid.uuid4().hex[:6].upper()}"[:10]

    async def scenario():
        holder = main.SessionLocal()
        waiter = main.SessionLocal()
        try:
            await me.lock_ticker(holder, ticker)

            blocked = asyncio.create_task(me.lock_ticker(waiter, ticker))
            await asyncio.sleep(0.4)
            assert not blocked.done(), "a second holder got in while it was held"

            await holder.commit()
            await asyncio.wait_for(blocked, timeout=10)
            await waiter.commit()
        finally:
            await holder.close()
            await waiter.close()

    run_isolated(scenario)


@requires_db
def test_re_acquiring_it_in_one_transaction_is_free():
    """`delete_trade`, `update_execution` and `delete_position` take the lock
    up front for lock-ORDER reasons, and then the rebuild takes it again.
    Advisory locks are re-entrant, so that must not self-deadlock."""
    ticker = f"ZZ{uuid.uuid4().hex[:6].upper()}"[:10]

    async def scenario():
        async with main.SessionLocal() as s:
            await asyncio.wait_for(me.lock_ticker(s, ticker), timeout=10)
            await asyncio.wait_for(me.lock_ticker(s, ticker), timeout=10)
            await asyncio.wait_for(me.lock_ticker(s, ticker), timeout=10)
            await s.commit()

    run_isolated(scenario)
