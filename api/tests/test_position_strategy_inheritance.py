"""A strategy chosen at planning must survive onto the position the matching
engine creates for it, so Trade Review opens with it already filled in.

Two hops carry it: `_apply_plan_to_fills` copies a plan's strategy_id onto the
opening trade (PLAN_TO_TRADE_FIELDS, tested elsewhere), and `insert_positions`
must copy that trade's strategy_id onto the position at the moment it is
first created. This file is the second hop -- the one that was missing.

The companion property, tested here too: once a position EXISTS, re-matching
must never touch its strategy_id, or a choice the trader made by hand during
review would be silently replaced by the trade's the next time a fill landed
on the ticker (a repair, a late broker report). `insert_positions` guarantees
this by leaving `strategy_id` out of the ON CONFLICT `refreshed` columns.

Runs inside `db_transaction()` -- one connection, one outer transaction that
is always rolled back, even on a failing assertion (see conftest.py). Nothing
here is ever committed past the test.
"""

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

os.environ.setdefault("CORS_ALLOW_ORIGINS", "https://trading-journal-test.vercel.app")

import main  # noqa: E402
from sqlalchemy import select  # noqa: E402
from services import matching_engine as me  # noqa: E402

from conftest import db_session, db_transaction, requires_db  # noqa: E402

T0 = datetime.now(timezone.utc) - timedelta(days=5)
TICKER = "ZZSTRATEST"


def _fill(direction, qty, price, hours, strategy_id=None):
    return main.Trade(
        id=uuid.uuid4(),
        ibkr_exec_id=f"REPAIR-{uuid.uuid4()}",
        ticker=TICKER, direction=direction, style="Unclassified",
        quantity=Decimal(qty), actual_entry=Decimal(price),
        entry_date=T0 + timedelta(hours=hours), commission=Decimal("0"),
        source_tag="Repair", strategy_id=strategy_id,
    )


async def _one_position(session):
    return (await session.execute(
        select(main.Position).where(main.Position.symbol == TICKER)
    )).scalar_one()


@requires_db
def test_a_fresh_position_inherits_its_opening_trades_strategy():
    """The bug: a plan-attached strategy on the opening trade never reached
    the position the matcher built from it, so Trade Review always opened
    with the dropdown blank."""
    async def scenario():
        async with db_transaction() as conn:
            session = db_session(conn)
            strategy_id = uuid.uuid4()
            session.add(main.Strategy(id=strategy_id, name=f"ZZ Test {strategy_id}"))
            session.add_all([
                _fill("BUY", 10, 100, 0, strategy_id=strategy_id),
                _fill("SELL", 10, 110, 1),
            ])
            await session.commit()

            result = await me.run_matching_for_ticker(session, TICKER, persist=True)
            assert len(result.positions) == 1

            position = await _one_position(session)
            assert position.strategy_id == strategy_id
            await session.close()

    asyncio.run(scenario())


@requires_db
def test_a_fresh_position_with_no_strategy_on_its_trade_stays_null():
    """The negative control: nothing to inherit means nothing is invented."""
    async def scenario():
        async with db_transaction() as conn:
            session = db_session(conn)
            session.add_all([
                _fill("BUY", 10, 100, 0),
                _fill("SELL", 10, 110, 1),
            ])
            await session.commit()

            await me.run_matching_for_ticker(session, TICKER, persist=True)

            position = await _one_position(session)
            assert position.strategy_id is None
            await session.close()

    asyncio.run(scenario())


@requires_db
def test_rematching_never_overwrites_a_strategy_chosen_during_review():
    """The safety property: once a position exists, ON CONFLICT must leave
    strategy_id alone, or a trader's own review choice would be clobbered the
    next time a repair fill re-triggered matching on the ticker."""
    async def scenario():
        async with db_transaction() as conn:
            session = db_session(conn)
            plan_strategy_id = uuid.uuid4()
            chosen_id = uuid.uuid4()
            session.add_all([
                main.Strategy(id=plan_strategy_id, name=f"ZZ Plan {plan_strategy_id}"),
                main.Strategy(id=chosen_id, name=f"ZZ Reviewed {chosen_id}"),
            ])
            session.add_all([
                _fill("BUY", 10, 100, 0, strategy_id=plan_strategy_id),
                _fill("SELL", 10, 110, 1),
            ])
            await session.commit()
            await me.run_matching_for_ticker(session, TICKER, persist=True)

            # The trader reviews it and picks a DIFFERENT strategy than the
            # plan's.
            position = await _one_position(session)
            position.strategy_id = chosen_id
            position.review_status = "reviewed"
            await session.commit()

            # A late repair fill re-triggers matching for the same ticker.
            # FIFO pairs are unchanged, so this hits the ON CONFLICT path.
            session.add(_fill("BUY", 1, 50, -1, strategy_id=plan_strategy_id))
            await session.commit()
            await me.run_matching_for_ticker(session, TICKER, persist=True)

            refreshed = (await session.execute(
                select(main.Position).where(main.Position.symbol == TICKER)
            )).scalars().all()
            for p in refreshed:
                assert p.strategy_id == chosen_id, (
                    "re-matching overwrote the trader's own review choice"
                )
            await session.close()

    asyncio.run(scenario())
