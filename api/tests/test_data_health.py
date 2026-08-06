"""The audit must find real disagreement, and must not invent any.

A health check that cannot go red is decoration, and one that goes red on a
clean ledger gets ignored within a week. Both directions are pinned here.

The grain test is the important one. `_reconcile_to_broker` sets each closing
fill's all-in cost to `our_gross - broker_realized_pnl`, so realised P&L
equals the broker's figure BY CONSTRUCTION -- which means comparing the two
per LEG measures apportionment rather than agreement. On the real ledger that
mistake reports 13 divergences where the correct grain reports zero. If
someone ever "simplifies" the broker check down to a per-leg comparison, the
test below is what says so.
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
from services.data_health import (  # noqa: E402
    STATUS_CLEAN,
    STATUS_CRITICAL,
    run_audit,
)

from conftest import db_session, db_transaction, requires_db  # noqa: E402

T0 = datetime.now(timezone.utc) - timedelta(days=5)
TICKER = "ZZAUDIT"


def _fill(direction, qty, price, hours, broker_pnl=None):
    return main.Trade(
        id=uuid.uuid4(),
        ibkr_exec_id=f"REPAIR-{uuid.uuid4()}",
        ticker=TICKER, direction=direction, style="Unclassified",
        quantity=Decimal(qty), actual_entry=Decimal(price),
        entry_date=T0 + timedelta(hours=hours), commission=Decimal("0"),
        source_tag="Repair", broker_realized_pnl=broker_pnl,
    )


def _check(result, key):
    return next(c for c in result["checks"] if c["key"] == key)


async def _counts(session, key):
    """One check's counts for the WHOLE ledger.

    The audit is global by design -- "does anything disagree" is not a
    per-ticker question, and the money checks sum across every symbol. So
    these tests measure a BASELINE first and assert on the DELTA a synthetic
    ticker introduces, rather than on absolute numbers that depend on
    whatever the real ledger happens to contain today. Asserting `drifted ==
    1` would pass or fail based on data no test wrote.
    """
    return _check(await run_audit(session), key)["counts"]


@requires_db
def test_a_consistent_ledger_reports_clean():
    """The negative control, and the one that decides whether anyone reads the
    badge: a ledger that agrees with itself must not be flagged."""
    async def scenario():
        async with db_transaction() as conn:
            session = db_session(conn)
            before = await _counts(session, "fifo_integrity")

            session.add_all([_fill("BUY", 10, 100, 0), _fill("SELL", 10, 110, 1)])
            await session.commit()
            await me.run_matching_for_ticker(session, TICKER, persist=True)

            after = await _counts(session, "fifo_integrity")

            for key in ("stale", "missing", "drifted", "fills_mismatched"):
                assert after[key] == before[key], (
                    f"a self-consistent ticker was flagged as {key}"
                )
            await session.close()

    asyncio.run(scenario())


@requires_db
def test_a_position_whose_stored_pnl_was_tampered_with_is_caught():
    """DRIFT: the pair is right and the money is not -- exactly the shape that
    hid the gross-vs-net bug for the whole life of the journal, when a
    pairs-only check reported CLEAN throughout."""
    async def scenario():
        async with db_transaction() as conn:
            session = db_session(conn)
            session.add_all([_fill("BUY", 10, 100, 0), _fill("SELL", 10, 110, 1)])
            await session.commit()
            await me.run_matching_for_ticker(session, TICKER, persist=True)
            before = await _counts(session, "fifo_integrity")

            position = (await session.execute(
                select(main.Position).where(main.Position.symbol == TICKER)
            )).scalar_one()
            position.realized_pnl = (position.realized_pnl or Decimal("0")) + Decimal("5")
            await session.commit()

            result = await run_audit(session)
            integrity = _check(result, "fifo_integrity")

            assert integrity["counts"]["drifted"] == before["drifted"] + 1
            assert integrity["status"] == STATUS_CRITICAL
            assert result["status"] == STATUS_CRITICAL
            assert any(
                i["kind"] == "DRIFT" and i["ticker"] == TICKER for i in integrity["items"]
            )
            await session.close()

    asyncio.run(scenario())


@requires_db
def test_a_round_trip_fifo_no_longer_produces_is_caught():
    """STALE: a backdated fill re-partitions the FIFO queue, so a stored round
    trip stops being reproducible. It keeps counting toward P&L until noticed."""
    async def scenario():
        async with db_transaction() as conn:
            session = db_session(conn)
            session.add_all([_fill("BUY", 10, 100, 0), _fill("SELL", 10, 110, 1)])
            await session.commit()
            await me.run_matching_for_ticker(session, TICKER, persist=True)
            before = await _counts(session, "fifo_integrity")

            # Dated BEFORE both, so FIFO consumes the sell against it instead
            # and the stored pair is no longer produced. Written directly
            # WITHOUT re-matching, which is the state the audit exists to find.
            session.add(_fill("BUY", 10, 90, -5))
            await session.commit()

            result = await run_audit(session)

            integrity = _check(result, "fifo_integrity")
            assert integrity["counts"]["stale"] == before["stale"] + 1
            assert integrity["status"] == STATUS_CRITICAL
            await session.close()

    asyncio.run(scenario())


@requires_db
def test_the_broker_check_compares_at_closing_fill_grain_not_per_leg():
    """Two open lots closed by ONE fill produce two legs from one broker
    figure. Summed per closing fill they tie exactly; compared per leg they
    cannot, because the broker never reported at that grain.

    This is the regression guard: on the real ledger the per-leg mistake
    reports 13 false divergences and the correct grain reports zero.
    """
    async def scenario():
        async with db_transaction() as conn:
            session = db_session(conn)
            before = await _counts(session, "broker_reconciliation")

            # Two separate entries, one exit -- the multi-leg shape.
            #
            # Gross is 50 + 40 = 90, so the broker figure has to imply a plug
            # that reads as a FEE (89.47 -> 53c on $1,100 of notional).
            # A wilder number is refused by _plug_is_plausible, the legs fall
            # back to apportioned commission, and this would end up testing
            # the refusal path instead of the grain.
            session.add_all([
                _fill("BUY", 5, 100, 0),
                _fill("BUY", 5, 102, 1),
                _fill("SELL", 10, 110, 2, broker_pnl=Decimal("89.47")),
            ])
            await session.commit()
            await me.run_matching_for_ticker(session, TICKER, persist=True)

            legs = (await session.execute(
                select(main.RealizedLeg).where(main.RealizedLeg.symbol == TICKER)
            )).scalars().all()
            assert len(legs) == 2, "the multi-leg shape this test needs did not occur"

            # No single leg equals the broker's whole-fill figure...
            assert all(
                leg.realized_pnl != Decimal("89.47") for leg in legs
            ), "legs happen to equal the fill figure; the grain distinction is untested"
            # ...but together they do, which is what the audit checks.
            assert sum(leg.realized_pnl for leg in legs) == Decimal("89.47")

            after = await _counts(session, "broker_reconciliation")
            assert after["fills_disagreeing"] == before["fills_disagreeing"], (
                "a multi-leg fill that ties out exactly was reported as disagreeing "
                "-- the comparison is being made per leg, not per closing fill"
            )
            assert after["fills_audited"] == before["fills_audited"] + 1
            await session.close()

    asyncio.run(scenario())


@requires_db
def test_a_genuine_broker_disagreement_is_caught():
    """The positive control for the check above: when our legs really do not
    sum to IBKR's figure, it must fire. This is the only way a refused plug
    (`_plug_is_plausible`) is visible at all -- it is logged, never stored."""
    async def scenario():
        async with db_transaction() as conn:
            session = db_session(conn)
            before = await _counts(session, "broker_reconciliation")

            close = _fill("SELL", 10, 110, 1, broker_pnl=Decimal("999.00"))
            session.add_all([_fill("BUY", 10, 100, 0), close])
            await session.commit()
            await me.run_matching_for_ticker(session, TICKER, persist=True)

            broker = _check(await run_audit(session), "broker_reconciliation")
            assert broker["counts"]["fills_disagreeing"] == before["fills_disagreeing"] + 1
            assert broker["status"] == STATUS_CRITICAL
            await session.close()

    asyncio.run(scenario())


@requires_db
def test_the_audit_writes_nothing():
    """It runs matching with persist=False. If that ever changed, a diagnostic
    would start repairing the thing it was asked to describe -- silently, and
    on every click of a badge."""
    async def scenario():
        async with db_transaction() as conn:
            session = db_session(conn)
            session.add_all([_fill("BUY", 10, 100, 0), _fill("SELL", 10, 110, 1)])
            await session.commit()
            await me.run_matching_for_ticker(session, TICKER, persist=True)

            position = (await session.execute(
                select(main.Position).where(main.Position.symbol == TICKER)
            )).scalar_one()
            position.realized_pnl = Decimal("1234.5678")
            await session.commit()

            await run_audit(session)

            # The tampered figure is still exactly as left: the audit reported
            # the drift rather than quietly correcting it.
            after = (await session.execute(
                select(main.Position.realized_pnl).where(main.Position.symbol == TICKER)
            )).scalar_one()
            assert after == Decimal("1234.5678")
            await session.close()

    asyncio.run(scenario())
