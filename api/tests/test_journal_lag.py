"""`positions.journaled_at` (migration 038): when a round trip was actually
written up, as distinct from when its review_status merely flipped.

Three properties need a real database rather than an assertion about the
migration file, because they are about the ORDER operations happen in and
what `review_position`/`dismiss_position` write to the same row across more
than one call -- exactly what a stub session cannot stand in for:

  * The first genuine review sets it; review_status alone cannot say WHEN a
    trade was journaled, only whether the queue has been cleared for it.
  * A later edit -- correcting a typo, adding a discipline answer -- must
    never move it forward. First write wins, or the metric would report how
    recently a trade was last TOUCHED rather than how long it sat unwritten.
  * `dismiss_position` never sets it, on purpose: dismissing is documented as
    "the user simply has nothing to write about it", and letting it feed the
    same column a real review does would let dismissing pass as journaling.
    A trade dismissed and LATER genuinely reviewed still gets it stamped at
    that later moment -- first genuine write, not first review_status flip.

`load_reviewed_trades` is exercised end to end too: the column has to survive
the actual SELECT, not just exist on the model.
"""

import asyncio
import os
import uuid
from datetime import datetime, timedelta

import pytest

os.environ.setdefault("CORS_ALLOW_ORIGINS", "https://trading-journal-test.vercel.app")

import main  # noqa: E402
from services.analytics import load_reviewed_trades  # noqa: E402

from conftest import db_session, db_transaction, requires_db  # noqa: E402

run = asyncio.run


async def _pending_position(session):
    """A closed round trip, still awaiting review -- same shape as the fixture
    test_plan_disciplines.py builds, without the plan linkage this file has
    no use for."""
    sym = f"ZZ{uuid.uuid4().hex[:6].upper()}"[:10]
    now = datetime.now(main.MARKET_TZ)
    open_id, close_id, pos_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    session.add_all([
        main.Trade(
            id=open_id, ibkr_exec_id=f"IBKR-{uuid.uuid4().hex[:12]}", ticker=sym,
            direction="BUY", style="Unclassified", quantity=main.Decimal("10"),
            actual_entry=main.Decimal("100"), entry_date=now - timedelta(days=2),
            source_tag="IBKR",
        ),
        main.Trade(
            id=close_id, ibkr_exec_id=f"IBKR-{uuid.uuid4().hex[:12]}", ticker=sym,
            direction="SELL", style="Unclassified", quantity=main.Decimal("10"),
            actual_entry=main.Decimal("110"), entry_date=now - timedelta(days=1),
            source_tag="IBKR",
        ),
    ])
    await session.flush()
    position = main.Position(
        id=pos_id, symbol=sym, direction="LONG", style="Unclassified",
        quantity=main.Decimal("10"), entry_price=main.Decimal("100"),
        exit_price=main.Decimal("110"), entry_time=now - timedelta(days=2),
        exit_time=now - timedelta(days=1), realized_pnl=main.Decimal("100"),
        gross_pnl=main.Decimal("100"), commission=main.Decimal("0"),
        open_trade_id=open_id, close_trade_id=close_id,
        review_status=main.ReviewStatus.pending.value,
    )
    session.add(position)
    await session.flush()
    return position


@requires_db
def test_a_genuine_review_stamps_journaled_at():
    async def scenario():
        async with db_transaction() as conn:
            session = db_session(conn)
            position = await _pending_position(session)

            out = await main.review_position(
                position.id, main.PositionReviewUpdate(notes="Chased the open"), session
            )

            assert out.review_status == main.ReviewStatus.reviewed.value
            refreshed = await session.get(main.Position, position.id)
            assert refreshed.journaled_at is not None
            # Stamped after the trade closed, not before -- the ordinary case,
            # pinned so a timezone mistake in the write path would fail loudly
            # rather than pass on a coincidence.
            assert refreshed.journaled_at >= refreshed.exit_time

    run(scenario())


@requires_db
def test_a_later_edit_does_not_move_it_forward():
    async def scenario():
        async with db_transaction() as conn:
            session = db_session(conn)
            position = await _pending_position(session)

            await main.review_position(
                position.id, main.PositionReviewUpdate(notes="first pass"), session
            )
            first_stamp = (await session.get(main.Position, position.id)).journaled_at

            # A second, genuine edit -- still mark_reviewed=True, the Trade
            # Inbox/Trade Ledger default -- must not push the timestamp on.
            await main.review_position(
                position.id,
                main.PositionReviewUpdate(notes="corrected a typo"),
                session,
            )
            second_stamp = (await session.get(main.Position, position.id)).journaled_at

            assert second_stamp == first_stamp

    run(scenario())


@requires_db
def test_mark_reviewed_false_never_sets_it():
    """The Analytics drawer's own opt-out (see test_review_completion_opt_out.py)
    must not silently start the journal-lag clock either -- jotting a note on
    a trade nobody has reviewed yet is not journaling it."""
    async def scenario():
        async with db_transaction() as conn:
            session = db_session(conn)
            position = await _pending_position(session)

            await main.review_position(
                position.id,
                main.PositionReviewUpdate(notes="just a note", mark_reviewed=False),
                session,
            )

            refreshed = await session.get(main.Position, position.id)
            assert refreshed.review_status == main.ReviewStatus.pending.value
            assert refreshed.journaled_at is None

    run(scenario())


@requires_db
def test_dismiss_never_sets_it():
    async def scenario():
        async with db_transaction() as conn:
            session = db_session(conn)
            position = await _pending_position(session)

            out = await main.dismiss_position(position.id, session)

            assert out.review_status == main.ReviewStatus.reviewed.value
            refreshed = await session.get(main.Position, position.id)
            assert refreshed.journaled_at is None

    run(scenario())


@requires_db
def test_a_dismissed_trade_later_genuinely_reviewed_is_stamped_then():
    """Dismissing does not burn the one chance to be counted -- writing a
    real review afterwards is still journaling it, at the moment it actually
    happens."""
    async def scenario():
        async with db_transaction() as conn:
            session = db_session(conn)
            position = await _pending_position(session)

            await main.dismiss_position(position.id, session)
            assert (await session.get(main.Position, position.id)).journaled_at is None

            await main.review_position(
                position.id, main.PositionReviewUpdate(notes="changed my mind"), session
            )
            refreshed = await session.get(main.Position, position.id)
            assert refreshed.journaled_at is not None

    run(scenario())


@requires_db
def test_load_reviewed_trades_reads_the_column_back():
    """Exercises the actual SELECT `load_reviewed_trades` runs, not just the
    ORM column declaration -- the analytics endpoint reads through this
    function, and a typo in the column list would not show up any other way."""
    async def scenario():
        async with db_transaction() as conn:
            session = db_session(conn)
            position = await _pending_position(session)
            await main.review_position(
                position.id, main.PositionReviewUpdate(notes="for the read-back"), session
            )

            trades = await load_reviewed_trades(session)
            mine = next(t for t in trades if t.trade_id == str(position.id))

            assert mine.journaled_at is not None

    run(scenario())
