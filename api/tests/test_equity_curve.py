"""Cumulative realised P&L over time, and the drawdown it went through.

Three decisions carry the weight here, and each is one plausible-looking
simplification away from a chart that lies:

  * Dated by EXIT, not entry. P&L lands in the account when the trade closes.
    Dating by entry draws a curve that recovers before the trade that
    recovered it.
  * Every calendar day gets a point. Plotting only trading days compresses a
    three-month pause into a single step, which makes "how fast did it
    recover" unanswerable.
  * The peak floors at zero. Measuring drawdown from a negative high-water
    mark reports an account that has only ever lost money as having recovered.
"""

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

os.environ.setdefault("CORS_ALLOW_ORIGINS", "https://trading-journal-test.vercel.app")

from services.analytics import (  # noqa: E402
    MARKET_TZ,
    ClosedPosition,
    build_equity_curve,
)


def position(pnl: str, exit_day: str, exit_hour: int = 15) -> ClosedPosition:
    """A closed round trip on a given Eastern-time date."""
    exit_at = datetime.fromisoformat(f"{exit_day}T{exit_hour:02d}:00:00").replace(
        tzinfo=MARKET_TZ
    )
    return ClosedPosition(
        realized_pnl=Decimal(pnl),
        entry_price=Decimal("100"),
        quantity=Decimal("1"),
        entry_time=exit_at - timedelta(days=1),
        exit_time=exit_at,
    )


def by_date(curve) -> dict[str, dict]:
    return {p["date"]: p for p in curve["points"]}


# ---------------------------------------------------------------------------
# Cumulative arithmetic
# ---------------------------------------------------------------------------


def test_the_curve_ends_on_net_pnl():
    """The last point must equal the number the dashboard headline shows.

    If these two ever disagree, one of them is wrong and the user has no way
    to tell which.
    """
    curve = build_equity_curve(
        [position("100", "2026-01-05"), position("-30", "2026-01-06")]
    )
    assert curve["points"][-1]["cumulative_pnl"] == 70.0
    assert curve["summary"]["net_pnl"] == 70.0


def test_same_day_closes_are_summed_into_one_point():
    """A day is one point on the curve, whatever it took to get there."""
    curve = build_equity_curve(
        [
            position("40", "2026-01-05", exit_hour=10),
            position("-15", "2026-01-05", exit_hour=15),
        ]
    )
    day = by_date(curve)["2026-01-05"]
    assert day["realized_pnl"] == 25.0
    assert day["trades"] == 2


def test_the_curve_starts_from_zero_the_day_before():
    """So the first close reads as a move, not as a starting level."""
    curve = build_equity_curve([position("50", "2026-01-05")])
    first = curve["points"][0]
    assert first["date"] == "2026-01-04"
    assert first["cumulative_pnl"] == 0.0
    assert first["trades"] == 0


# ---------------------------------------------------------------------------
# Time is proportional
# ---------------------------------------------------------------------------


def test_quiet_days_are_still_plotted():
    """A gap in trading is a gap on the chart, at its true width."""
    curve = build_equity_curve(
        [position("10", "2026-01-05"), position("10", "2026-01-15")]
    )
    points = by_date(curve)
    # 4th (anchor) through the 15th inclusive.
    assert len(curve["points"]) == 12
    assert "2026-01-10" in points
    assert points["2026-01-10"]["trades"] == 0
    # A day with no closes carries the balance forward rather than dropping it.
    assert points["2026-01-10"]["cumulative_pnl"] == 10.0


def test_summary_separates_trading_days_from_calendar_days():
    """Two closes ten days apart is not ten days of trading."""
    curve = build_equity_curve(
        [position("10", "2026-01-05"), position("10", "2026-01-15")]
    )
    assert curve["summary"]["trading_days"] == 2
    assert curve["summary"]["calendar_days"] == 11
    assert curve["summary"]["closed_trades"] == 2


def test_positions_are_dated_by_exit_not_entry():
    """The whole reason exit_time was added to ClosedPosition."""
    opened = datetime(2026, 1, 5, 10, tzinfo=MARKET_TZ)
    closed = datetime(2026, 3, 20, 10, tzinfo=MARKET_TZ)
    curve = build_equity_curve(
        [
            ClosedPosition(
                realized_pnl=Decimal("500"),
                entry_price=Decimal("100"),
                quantity=Decimal("1"),
                entry_time=opened,
                exit_time=closed,
            )
        ]
    )
    assert curve["summary"]["start_date"] == "2026-03-20"
    assert by_date(curve)["2026-03-20"]["cumulative_pnl"] == 500.0


def test_a_late_evening_close_is_dated_in_market_time():
    """UTC would roll a 20:00 ET close into the next day and misdate it."""
    curve = build_equity_curve([position("10", "2026-01-05", exit_hour=20)])
    assert curve["summary"]["end_date"] == "2026-01-05"


# ---------------------------------------------------------------------------
# Drawdown
# ---------------------------------------------------------------------------


def test_drawdown_measures_from_the_high_water_mark():
    curve = build_equity_curve(
        [
            position("100", "2026-01-05"),
            position("-40", "2026-01-06"),
            position("-20", "2026-01-07"),
        ]
    )
    points = by_date(curve)
    assert points["2026-01-05"]["drawdown"] == 0.0
    assert points["2026-01-06"]["drawdown"] == -40.0
    assert points["2026-01-07"]["drawdown"] == -60.0
    assert curve["summary"]["max_drawdown"] == -60.0


def test_a_new_high_resets_the_drawdown_but_not_the_worst_one():
    """Current drawdown is where you are; max drawdown is what you survived."""
    curve = build_equity_curve(
        [
            position("100", "2026-01-05"),
            position("-60", "2026-01-06"),
            position("120", "2026-01-07"),
        ]
    )
    assert curve["summary"]["current_drawdown"] == 0.0
    assert curve["summary"]["max_drawdown"] == -60.0
    assert curve["summary"]["peak_pnl"] == 160.0


def test_an_account_that_only_loses_is_never_recovering():
    """Peak floors at zero.

    Letting the peak track a negative cumulative would make every subsequent
    loss look like a fresh start, and the drawdown would read 0 on the worst
    day of the account's life.
    """
    curve = build_equity_curve(
        [position("-50", "2026-01-05"), position("-50", "2026-01-06")]
    )
    assert curve["summary"]["peak_pnl"] == 0.0
    assert curve["summary"]["max_drawdown"] == -100.0
    assert by_date(curve)["2026-01-06"]["drawdown"] == -100.0


def test_drawdown_is_signed_negative():
    """So deeper is lower, on the number as well as the chart."""
    curve = build_equity_curve(
        [position("10", "2026-01-05"), position("-4", "2026-01-06")]
    )
    assert all(p["drawdown"] <= 0 for p in curve["points"])


# ---------------------------------------------------------------------------
# Nothing to draw
# ---------------------------------------------------------------------------


def test_no_closed_trades_yields_an_empty_curve_not_a_fake_one():
    curve = build_equity_curve([])
    assert curve["points"] == []
    assert curve["summary"]["start_date"] is None
    assert curve["summary"]["net_pnl"] == 0.0
    assert curve["summary"]["closed_trades"] == 0


def test_a_position_with_no_exit_time_is_dropped_not_guessed():
    """Dating it by entry, or by today, would both invent a fact."""
    curve = build_equity_curve(
        [
            ClosedPosition(
                realized_pnl=Decimal("99"),
                entry_price=Decimal("100"),
                quantity=Decimal("1"),
                entry_time=datetime(2026, 1, 5, tzinfo=timezone.utc),
                exit_time=None,
            )
        ]
    )
    assert curve["points"] == []
    assert curve["summary"]["closed_trades"] == 0
