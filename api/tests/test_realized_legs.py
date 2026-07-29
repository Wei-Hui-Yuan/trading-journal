"""Money is realised by a closing FILL, not by a round trip.

THE BUG. `positions` gets a row only when a ticker returns to flat. That is the
right grain for counting ideas and the wrong grain for cash: selling half a
position banks that P&L whatever happens to the other half. Every dollar
realised on the way out of a position still held was therefore recorded
nowhere, and no figure in the app could see it.

Measured against this account's real fills, 2026 year-to-date: the dashboard
reported +66.20 gross where the broker's own executions say -6.14. The whole
72.34 difference was two positions still open -- MSFT, scaled in and out five
times since 29 May while still holding 2 shares, and VRT, one of two shares
sold. The all-time figure was wrong by the same money.

THE SECOND BUG, same cause. A round trip's P&L was dated entirely by its FINAL
exit, so scaling out in December and closing in January put every dollar in
January. Harmless on today's data by luck; guaranteed to corrupt a windowed
total the moment a scale-out straddles a boundary, which the timeframe filter
invites.

The engine had computed every one of these legs all along and thrown them away.
`open_round_trip_realized_pnl` summed them, was asserted in tests, and was read
by nothing else.
"""

import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

os.environ.setdefault("CORS_ALLOW_ORIGINS", "https://trading-journal-test.vercel.app")

from services.analytics import (  # noqa: E402
    ClosedPosition,
    RealizedLeg,
    Window,
    build_equity_curve,
    compute_core_stats,
    filter_legs_by_window,
)
from services.matching_engine import Execution, match_executions  # noqa: E402

T0 = datetime(2026, 5, 29, 13, 30, tzinfo=timezone.utc)


def ex(i, direction, qty, price, minutes, commission="0"):
    return Execution(
        trade_id=uuid.UUID(int=i),
        ticker="MSFT",
        direction=direction,
        quantity=Decimal(str(qty)),
        price=Decimal(str(price)),
        executed_at=T0 + timedelta(minutes=minutes),
        commission=Decimal(commission),
    )


def leg(pnl, day, position_id=None):
    return RealizedLeg(
        realized_pnl=Decimal(str(pnl)),
        gross_pnl=Decimal(str(pnl)),
        commission=Decimal("0"),
        exit_time=datetime(2026, day // 100, day % 100, 19, 0, tzinfo=timezone.utc),
        position_id=position_id,
    )


def closed(pnl, exit_day):
    at = datetime(2026, exit_day // 100, exit_day % 100, 19, 0, tzinfo=timezone.utc)
    return ClosedPosition(
        realized_pnl=Decimal(str(pnl)), gross_pnl=Decimal(str(pnl)),
        commission=Decimal("0"), entry_price=Decimal("100"),
        quantity=Decimal("1"), entry_time=at, exit_time=at,
    )


# ---------------------------------------------------------------------------
# The engine keeps the legs it used to discard
# ---------------------------------------------------------------------------


def test_a_still_open_run_exposes_its_realised_legs():
    """The MSFT shape, reduced: buy, sell part, still holding.

    No round trip closes, so `positions` gets nothing -- and before migration
    022 that meant the realised money vanished with it.
    """
    result = match_executions([ex(1, "BUY", 4, 100, 0), ex(2, "SELL", 3, 90, 60)])

    assert result.positions == [], "nothing went flat, so no round trip"
    assert result.open_quantity == Decimal("1")
    assert len(result.open_legs) == 1
    assert result.open_legs[0].realized_pnl == Decimal("-30")
    # The convenience total must keep agreeing with the legs behind it.
    assert result.open_round_trip_realized_pnl == sum(
        l.realized_pnl for l in result.open_legs
    )


def test_a_completed_round_trip_carries_the_legs_it_was_folded_from():
    """Scaling out twice is one position and two realisation events."""
    result = match_executions([
        ex(1, "BUY", 10, 100, 0),
        ex(2, "SELL", 4, 110, 60),
        ex(3, "SELL", 6, 120, 120),
    ])

    position = result.positions[0]
    assert len(position.legs) == 2
    # Legs sum to the position: one number, two dates.
    assert sum(l.realized_pnl for l in position.legs) == position.realized_pnl
    assert {l.exit_date for l in position.legs} == {
        T0 + timedelta(minutes=60), T0 + timedelta(minutes=120)
    }


def test_legs_of_a_scaled_out_run_carry_different_dates():
    """The dating bug, stated directly. The position reports one exit; the
    money happened on two days, and only the legs know that."""
    result = match_executions([
        ex(1, "BUY", 10, 100, 0),
        ex(2, "SELL", 5, 110, 60),
        ex(3, "SELL", 5, 90, 60 * 24 * 40),      # 40 days later
    ])
    position = result.positions[0]
    assert len({l.exit_date.date() for l in position.legs}) == 2
    assert position.exit_date == max(l.exit_date for l in position.legs)


def test_the_open_run_legs_are_the_ones_with_no_round_trip():
    """A ticker that closes one round trip and then re-opens: the second run's
    realised P&L is real money with no position to hang it on."""
    result = match_executions([
        ex(1, "BUY", 5, 100, 0), ex(2, "SELL", 5, 110, 60),      # closes
        ex(3, "BUY", 10, 200, 120), ex(4, "SELL", 4, 180, 180),  # still open
    ])
    assert len(result.positions) == 1
    assert sum(l.realized_pnl for l in result.positions[0].legs) == Decimal("50")
    assert sum(l.realized_pnl for l in result.open_legs) == Decimal("-80")


# ---------------------------------------------------------------------------
# Two grains: money from legs, counts from round trips
# ---------------------------------------------------------------------------


def test_core_stats_money_comes_from_legs_and_counts_from_positions():
    """The heart of the fix. One completed trade, plus money banked out of a
    position still running -- P&L reflects both, trade count only the one."""
    stats = compute_core_stats(
        [closed("50", 601)],
        [leg("50", 601, position_id=uuid.uuid4()), leg("-30", 610)],
    )
    assert stats["net_pnl"] == 20.0, "50 booked + 30 banked against"
    assert stats["total_trades"] == 1, "a partial exit is not a finished idea"
    assert stats["open_run_pnl"] == -30.0


def test_open_run_pnl_names_the_gap_between_the_two_grains():
    """Without it, net_pnl and the round trips under it differ for no stated
    reason, which reads as a bug in the journal."""
    stats = compute_core_stats([closed("50", 601)], [leg("50", 601, uuid.uuid4()), leg("-30", 610)])
    round_trip_total = 50.0
    assert stats["net_pnl"] - stats["open_run_pnl"] == round_trip_total


def test_win_rate_ignores_partial_exits_entirely():
    """A losing scale-out of a position still open must not create a loss in
    the win rate -- the idea has not finished, so it has not lost yet."""
    stats = compute_core_stats([closed("50", 601)], [leg("50", 601, uuid.uuid4()), leg("-30", 610)])
    assert stats["win_rate_pct"] == 100.0
    assert stats["total_trades"] == 1


def test_without_legs_it_falls_back_to_summing_round_trips():
    """The old behaviour, kept for callers with no legs to give. Understates by
    exactly the open-run money, which is the bug -- so nothing on the dashboard
    path may use it."""
    stats = compute_core_stats([closed("50", 601)])
    assert stats["net_pnl"] == 50.0
    assert stats["open_run_pnl"] == 0.0


# ---------------------------------------------------------------------------
# Windowing, which is where this became visible
# ---------------------------------------------------------------------------


def test_a_window_selects_money_by_when_it_was_realised():
    from datetime import date
    legs = [leg("100", 601), leg("-40", 710)]
    kept = filter_legs_by_window(legs, Window(date(2026, 7, 1), date(2026, 7, 31)))
    assert [l.realized_pnl for l in kept] == [Decimal("-40")]


def test_a_scale_out_either_side_of_a_boundary_lands_in_both_windows():
    """The second bug. Round-trip attribution put all of this in July; the
    money was realised in two different months and now says so."""
    from datetime import date
    legs = [leg("100", 601), leg("-40", 710)]     # one round trip, two dates
    june = filter_legs_by_window(legs, Window(date(2026, 6, 1), date(2026, 6, 30)))
    july = filter_legs_by_window(legs, Window(date(2026, 7, 1), date(2026, 7, 31)))
    assert sum(l.realized_pnl for l in june) == Decimal("100")
    assert sum(l.realized_pnl for l in july) == Decimal("-40")


# ---------------------------------------------------------------------------
# The equity curve
# ---------------------------------------------------------------------------


def test_the_curve_plots_legs_when_it_has_them():
    """Money on the day it was realised, including out of an open position --
    which is the whole point of a chart of when the account moved."""
    curve = build_equity_curve([closed("100", 601)],
                               legs=[leg("100", 601, uuid.uuid4()), leg("-30", 605)])
    assert curve["summary"]["net_pnl"] == 70.0
    by_date = {p["date"]: p for p in curve["points"]}
    assert by_date["2026-06-05"]["realized_pnl"] == -30.0


def test_the_curve_counts_trades_from_round_trips_not_legs():
    """The tooltip says "N trades". Scaling out of one position four times is
    one trade, and counting legs would report four."""
    curve = build_equity_curve(
        [closed("100", 601)],
        legs=[leg("60", 601, uuid.uuid4()), leg("40", 601, uuid.uuid4())],
    )
    assert curve["summary"]["closed_trades"] == 1
    by_date = {p["date"]: p for p in curve["points"]}
    assert by_date["2026-06-01"]["trades"] == 1
    assert by_date["2026-06-01"]["realized_pnl"] == 100.0


def test_the_curve_without_legs_still_works():
    curve = build_equity_curve([closed("100", 601)])
    assert curve["summary"]["net_pnl"] == 100.0


# ---------------------------------------------------------------------------
# Reconciling costs to the broker's own realised figure
# ---------------------------------------------------------------------------
#
# Our FIFO agrees with IBKR's lot matching exactly -- compared fill by fill
# over a year, all 166 closing fills landed within $0.50 and the ledger within
# $9.14. What differed was COST: `ibCommission` is the commission alone, while
# IBKR's realised P&L also nets exchange, clearing and regulatory charges,
# which the Flex feed folds into cost basis. About 5.3c per closing fill.
#
# So the all-in cost is taken from the broker rather than reconstructed from a
# fee schedule: all_in = our_gross - broker_realized_pnl.


def bex(i, direction, qty, price, minutes, commission="0", broker=None):
    return Execution(
        trade_id=uuid.UUID(int=i), ticker="MSFT", direction=direction,
        quantity=Decimal(str(qty)), price=Decimal(str(price)),
        executed_at=T0 + timedelta(minutes=minutes),
        commission=Decimal(commission),
        broker_realized_pnl=None if broker is None else Decimal(str(broker)),
    )


def test_the_all_in_cost_comes_from_the_brokers_figure():
    """Gross +100, broker says +97.50 -> the trade cost 2.50 all in, even
    though ibCommission only accounts for 2.00 of it."""
    result = match_executions([
        bex(1, "BUY", 10, 100, 0, commission="1.00"),
        bex(2, "SELL", 10, 110, 60, commission="1.00", broker="97.50"),
    ])
    position = result.positions[0]
    assert position.gross_pnl == Decimal("100")
    assert position.commission == Decimal("2.50"), "all-in, not just commission"
    assert position.realized_pnl == Decimal("97.50"), "ties to the broker exactly"


def test_the_raw_commission_is_preserved_beside_it():
    """Provenance. "What did I pay IBKR" and "what did this cost me" stay two
    separate answerable questions."""
    result = match_executions([
        bex(1, "BUY", 10, 100, 0, commission="1.00"),
        bex(2, "SELL", 10, 110, 60, commission="1.00", broker="97.50"),
    ])
    leg = result.positions[0].legs[0]
    assert leg.ib_commission == Decimal("2.00"), "raw ibCommission, untouched"
    assert leg.commission == Decimal("2.50"), "all-in"
    assert leg.broker_realized_pnl == Decimal("97.50")


def test_a_fill_with_no_broker_figure_falls_back_to_commission():
    """A REPAIR- fill has no broker figure by definition, and the Flex window
    reaches back only 365 days. Those must not silently cost zero."""
    result = match_executions([
        bex(1, "BUY", 10, 100, 0, commission="1.00"),
        bex(2, "SELL", 10, 110, 60, commission="1.00", broker=None),
    ])
    leg = result.positions[0].legs[0]
    assert leg.commission == Decimal("2.00"), "apportioned ibCommission"
    assert leg.broker_realized_pnl is None, "flagged as unverified"


def test_the_broker_cost_is_not_added_on_top_of_the_commission():
    """The regression that would double-count. IBKR's figure already has the
    commission subtracted, so the all-in cost REPLACES it rather than adding."""
    result = match_executions([
        bex(1, "BUY", 10, 100, 0, commission="1.00"),
        bex(2, "SELL", 10, 110, 60, commission="1.00", broker="97.50"),
    ])
    # 100 - 2.50, never 100 - 2.00 - 2.50.
    assert result.positions[0].realized_pnl == Decimal("97.50")


def test_the_cost_splits_across_every_leg_the_fill_closed():
    """One sell closing two lots gets one broker figure, and the parts must sum
    back to it -- a cent lost here is a cent the period total is off by."""
    result = match_executions([
        bex(1, "BUY", 6, 100, 0),
        bex(2, "BUY", 4, 100, 30),
        bex(3, "SELL", 10, 110, 60, broker="97.50"),
    ])
    legs = result.positions[0].legs
    assert len(legs) == 2
    assert sum(l.commission for l in legs) == Decimal("2.50")
    assert sum(l.realized_pnl for l in legs) == Decimal("97.50")


def test_an_open_run_is_reconciled_too():
    """Money banked out of a position still held is realised money, and IBKR
    reports it -- so it gets the same treatment, not the fallback."""
    result = match_executions([
        bex(1, "BUY", 10, 100, 0, commission="1.00"),
        bex(2, "SELL", 4, 90, 60, commission="1.00", broker="-42.00"),
    ])
    assert result.positions == []
    assert sum(l.realized_pnl for l in result.open_legs) == Decimal("-42.00")
