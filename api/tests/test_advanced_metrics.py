"""Advanced performance metrics: R-multiple, slippage, expectancy."""

import dataclasses
import inspect
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from services.analytics import (
    ClosedPosition,
    RealizedLeg,
    ReviewedTrade,
    compute_advanced_metrics,
    compute_core_stats,
    compute_expectancy,
    compute_journal_lag,
    compute_r_multiple,
    compute_slippage,
    load_reviewed_trades,
)


def make_trade(
    direction="BUY",
    entry="100",
    exit_=None,
    stop=None,
    planned=None,
    mistakes=None,
    exit_time=None,
    journaled_at=None,
):
    # Unique per call -- compute_advanced_metrics joins scored R values back
    # onto trades by trade_id (see compute_strategy_breakdown and, since the
    # #6 audit fix, the mistake breakdown too), and every trade in a real
    # ledger has its own id. A shared literal here would collapse that join
    # in any test with more than one trade, silently mixing up their R.
    return ReviewedTrade(
        trade_id=str(uuid.uuid4()),
        ticker="AAA",
        direction=direction,
        quantity=100,
        actual_entry=Decimal(entry),
        exit_price=Decimal(exit_) if exit_ is not None else None,
        planned_entry=Decimal(planned) if planned is not None else None,
        stop_loss=Decimal(stop) if stop is not None else None,
        mistakes=mistakes or [],
        review_status="reviewed",
        exit_time=exit_time,
        journaled_at=journaled_at,
    )


class TestRMultiple:
    @pytest.mark.parametrize(
        "direction,entry,exit_,stop,expected",
        [
            # Long: entry 100, stop 98 -> risk 2
            ("BUY", "100", "106", "98", 3.0),
            ("BUY", "100", "98", "98", -1.0),  # stopped out
            ("BUY", "100", "101", "98", 0.5),
            # Short: entry 100, stop 102 -> risk 2
            ("SELL", "100", "94", "102", 3.0),
            ("SELL", "100", "102", "102", -1.0),
            ("SELL", "100", "104", "102", -2.0),
        ],
    )
    def test_computes_reward_over_risk(self, direction, entry, exit_, stop, expected):
        assert compute_r_multiple(make_trade(direction, entry, exit_, stop)) == expected

    def test_scratch_is_zero_not_none(self):
        """0R is a real outcome and must stay distinguishable from unscoreable."""
        assert compute_r_multiple(make_trade("BUY", "100", "100", "98")) == 0.0

    @pytest.mark.parametrize(
        "direction,entry,exit_,stop",
        [
            ("BUY", "100", "110", "100"),  # stop at entry -> zero risk
            ("BUY", "100", "110", None),  # no stop
            ("BUY", "100", None, "98"),  # still open
            ("BUY", "100", "110", "105"),  # long stop above entry
            ("SELL", "100", "90", "95"),  # short stop below entry
        ],
    )
    def test_unscoreable_returns_none(self, direction, entry, exit_, stop):
        """None, never 0.0 -- conflating them would corrupt every aggregate."""
        assert compute_r_multiple(make_trade(direction, entry, exit_, stop)) is None


class TestSlippage:
    @pytest.mark.parametrize(
        "direction,actual,planned,expected",
        [
            ("BUY", "100.25", "100.00", 0.25),  # paid up = worse
            ("BUY", "99.90", "100.00", -0.10),  # better fill
            ("SELL", "99.75", "100.00", 0.25),  # sold lower = worse
            ("SELL", "100.10", "100.00", -0.10),
        ],
    )
    def test_signed_against_the_trader(self, direction, actual, planned, expected):
        trade = make_trade(direction, actual, planned=planned)
        assert compute_slippage(trade) == expected

    def test_no_plan_returns_none(self):
        assert compute_slippage(make_trade("BUY", "100")) is None


class TestJournalLag:
    def test_hours_between_close_and_the_review_that_completed_it(self):
        exit_time = datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)
        journaled_at = datetime(2026, 6, 3, 15, 0, tzinfo=timezone.utc)  # +54h
        trade = make_trade(exit_time=exit_time, journaled_at=journaled_at)
        assert compute_journal_lag(trade) == 54.0

    def test_not_yet_journaled_returns_none_not_zero(self):
        """Covers both the never-reviewed and dismissed-not-reviewed cases --
        `journaled_at` is None either way, and this must not read as an
        instant journal entry."""
        trade = make_trade(exit_time=datetime(2026, 6, 1, tzinfo=timezone.utc))
        assert compute_journal_lag(trade) is None

    def test_no_exit_time_returns_none(self):
        trade = make_trade(journaled_at=datetime(2026, 6, 1, tzinfo=timezone.utc))
        assert compute_journal_lag(trade) is None

    def test_a_negative_gap_returns_none_rather_than_flattering_the_average(self):
        """journaled_at before exit_time cannot happen through the real write
        path, but a wrong answer here would be a negative "lag" rather than a
        loud failure -- guarded the same as any other invariant no valid
        input can violate but a bug still could."""
        exit_time = datetime(2026, 6, 3, tzinfo=timezone.utc)
        journaled_at = datetime(2026, 6, 1, tzinfo=timezone.utc)
        trade = make_trade(exit_time=exit_time, journaled_at=journaled_at)
        assert compute_journal_lag(trade) is None


class TestExpectancy:
    def test_balanced_sample(self):
        # 2 wins @2R, 2 losses @1R -> (0.5*2) - (0.5*1)
        assert compute_expectancy([2.0, 2.0, -1.0, -1.0]) == 0.5

    def test_all_wins(self):
        assert compute_expectancy([1.0, 3.0]) == 2.0

    def test_all_losses(self):
        assert compute_expectancy([-1.0, -1.0]) == -1.0

    def test_empty_returns_none(self):
        assert compute_expectancy([]) is None


class TestAggregate:
    @pytest.fixture
    def metrics(self):
        return compute_advanced_metrics(
            [
                make_trade("BUY", "100", "106", "98", planned="99.50", mistakes=["FOMO"]),
                make_trade(
                    "BUY", "100", "98", "98", planned="100.00", mistakes=["Chased", "FOMO"]
                ),
                make_trade("SELL", "100", "94", "102", planned="100.00"),
                make_trade("BUY", "100", "110", "100"),  # unscoreable
                make_trade("BUY", "100", None, "98"),  # open
            ]
        )

    def test_scored_vs_unscored(self, metrics):
        assert metrics["scored_trades"] == 3
        assert metrics["unscored_trades"] == 2

    def test_totals(self, metrics):
        assert metrics["total_r"] == 5.0
        assert metrics["win_rate_pct"] == 66.67
        assert metrics["profit_factor_r"] == 6.0

    def test_mistake_breakdown_overlaps(self, metrics):
        by = {b["mistake"]: b for b in metrics["mistake_breakdown"]}
        # One trade carrying two tags counts toward both.
        assert by["FOMO"]["trade_count"] == 2
        assert by["FOMO"]["total_r"] == 2.0
        assert by["Chased"]["total_r"] == -1.0

    def test_worst_mistake_sorted_first(self, metrics):
        assert metrics["mistake_breakdown"][0]["mistake"] == "Chased"

    def test_journal_lag_averages_only_what_was_actually_journaled(self):
        """A never-journaled and a dismissed trade look identical here --
        both are `journaled_at=None` -- and both must be left OUT of the
        average, not counted as zero lag. Only the two genuinely journaled
        trades feed it."""
        exit_time = datetime(2026, 6, 1, tzinfo=timezone.utc)
        metrics = compute_advanced_metrics([
            make_trade(
                "BUY", "100", "106", "98",
                exit_time=exit_time,
                journaled_at=datetime(2026, 6, 2, tzinfo=timezone.utc),  # 24h
            ),
            make_trade(
                "BUY", "100", "106", "98",
                exit_time=exit_time,
                journaled_at=datetime(2026, 6, 4, tzinfo=timezone.utc),  # 72h
            ),
            # Never journaled -- open review queue or a dismissal, either way
            # journaled_at is None.
            make_trade("BUY", "100", "106", "98", exit_time=exit_time),
        ])
        assert metrics["journal_lag_sample"] == 2
        assert metrics["avg_journal_lag_hours"] == 48.0

    def test_a_mistake_tagged_on_an_unscoreable_trade_is_not_invisible(self):
        """Issue #6 of the calculation audit: tagging "Oversized" on 3 trades
        where 1 lacks a stop used to report "2 trades" with nothing on
        screen hinting a third existed -- because the old loop iterated only
        `scored`, so an unscoreable trade's mistake tags were never even
        counted. Mirrors compute_strategy_breakdown's own unscored handling."""
        metrics = compute_advanced_metrics([
            make_trade("BUY", "100", "106", "98", mistakes=["Oversized"]),  # R=3.0
            make_trade("BUY", "100", "101", "98", mistakes=["Oversized"]),  # R=0.5
            # No stop -> unscoreable, but still a real instance of the tag.
            make_trade("BUY", "100", "110", None, mistakes=["Oversized"]),
        ])
        by = {b["mistake"]: b for b in metrics["mistake_breakdown"]}
        assert by["Oversized"]["trade_count"] == 3
        assert by["Oversized"]["scored"] == 2
        assert by["Oversized"]["unscored"] == 1
        # total_r/avg_r/win_rate still come from the 2 scoreable trades only
        # -- the fix discloses the gap, it does not invent a score for it.
        assert by["Oversized"]["total_r"] == 3.5
        assert by["Oversized"]["avg_r"] == 1.75
        assert by["Oversized"]["win_rate_pct"] == 100.0

    def test_a_mistake_tagged_only_on_unscoreable_trades_still_appears(self):
        """The tag must surface even with nothing to score at all -- a
        trader tagging "No Plan" on their only unreviewed-stop trades should
        still see the count, not have the mistake vanish entirely."""
        metrics = compute_advanced_metrics([
            make_trade("BUY", "100", "110", None, mistakes=["No Plan"]),
            make_trade("BUY", "100", "90", None, mistakes=["No Plan"]),
        ])
        by = {b["mistake"]: b for b in metrics["mistake_breakdown"]}
        assert by["No Plan"]["trade_count"] == 2
        assert by["No Plan"]["scored"] == 0
        assert by["No Plan"]["unscored"] == 2
        assert by["No Plan"]["total_r"] == 0.0
        assert by["No Plan"]["avg_r"] is None
        assert by["No Plan"]["win_rate_pct"] is None

    def test_r_distribution_sums_to_scored(self, metrics):
        assert sum(metrics["r_distribution"].values()) == metrics["scored_trades"]


class TestJsonSafety:
    def test_no_loss_profit_factor_is_none_not_infinity(self):
        """float('inf') is not valid JSON; Starlette's encoder rejects it."""
        m = compute_advanced_metrics([make_trade("BUY", "100", "106", "98")])
        assert m["profit_factor_r"] is None

        import json

        payload = json.dumps(m)
        assert "Infinity" not in payload and "NaN" not in payload

    def test_empty_input_returns_none_not_zero(self):
        m = compute_advanced_metrics([])
        assert m["scored_trades"] == 0
        assert m["avg_r"] is None
        assert m["expectancy_r"] is None
        assert m["avg_slippage"] is None
        assert m["avg_journal_lag_hours"] is None
        assert m["journal_lag_sample"] == 0


def make_position(pnl, entry_price, quantity, exit_day=601):
    at = datetime(2026, exit_day // 100, exit_day % 100, 19, 0, tzinfo=timezone.utc)
    return ClosedPosition(
        realized_pnl=Decimal(str(pnl)), gross_pnl=Decimal(str(pnl)),
        commission=Decimal("0"), entry_price=Decimal(str(entry_price)),
        quantity=Decimal(str(quantity)), entry_time=at, exit_time=at,
    )


def make_leg(pnl, exit_day=610, position_id=None):
    return RealizedLeg(
        realized_pnl=Decimal(str(pnl)), gross_pnl=Decimal(str(pnl)),
        commission=Decimal("0"),
        exit_time=datetime(2026, exit_day // 100, exit_day % 100, 19, 0, tzinfo=timezone.utc),
        position_id=position_id,
    )


class TestCapitalWeightedROI:
    """avg_roi_pct has to weight by capital, not average the percentages.

    A $1 position at +300% and a $1,000 position at +5% are not equally
    informative about what happened to the money -- the unweighted mean of
    the two percentages reports 152.5%, a number nobody's account returned.
    """

    def test_a_tiny_position_cannot_dominate_a_large_one(self):
        tiny = make_position(pnl="3", entry_price="1", quantity="1")  # +300%
        large = make_position(pnl="50", entry_price="1000", quantity="1")  # +5%

        stats = compute_core_stats([tiny, large])

        old_unweighted_mean = (300.0 + 5.0) / 2  # what the bug reported: 152.5
        assert stats["avg_roi_pct"] != pytest.approx(old_unweighted_mean, abs=1.0)
        # (3 + 50) / (1 + 1000) * 100
        assert stats["avg_roi_pct"] == pytest.approx(5.29, abs=0.01)

    def test_weighting_uses_position_pnl_not_leg_grain_net_pnl(self):
        """The grain mismatch this fix has to avoid introducing.

        net_pnl is leg-grain when legs are supplied (the dashboard's normal
        case) and includes open_run_pnl -- money banked scaling out of a
        position that is still open, and therefore has no `positions` row and
        no entry in the cost-basis sum at all. Dividing that money by a
        denominator that never counted it would make "capital-weighted" ROI
        depend on capital it never measured. avg_roi_pct must come from the
        same positions total_cost is built from, not from net_pnl.
        """
        position = make_position(pnl="50", entry_price="100", quantity="1")  # cost basis 100
        # Money banked on a DIFFERENT, still-open position: position_id=None
        # is exactly what marks a leg as carrying no round trip.
        open_leg = make_leg(pnl="-30", position_id=None)
        closing_leg = make_leg(pnl="50", position_id=uuid.uuid4())

        stats = compute_core_stats([position], [closing_leg, open_leg])

        assert stats["net_pnl"] == 20.0, "leg-grain: 50 booked - 30 banked against"
        assert stats["open_run_pnl"] == -30.0
        # Using net_pnl here instead of the position's own pnl would give
        # (50 - 30) / 100 * 100 == 20.0. The correct figure reflects only the
        # one position total_cost actually measured: 50 / 100 * 100.
        assert stats["avg_roi_pct"] == 50.0

    def test_zero_cost_basis_positions_are_excluded_both_sides(self):
        """A position with no entry price contributes to neither sum -- not a
        divide-by-zero, and not a phantom 0% dragging the average down."""
        free = make_position(pnl="10", entry_price="0", quantity="5")
        priced = make_position(pnl="20", entry_price="50", quantity="1")

        stats = compute_core_stats([free, priced])
        assert stats["avg_roi_pct"] == 40.0  # 20 / 50 * 100 -- `free` excluded entirely

    def test_all_zero_cost_basis_reports_zero_not_a_crash(self):
        stats = compute_core_stats([make_position(pnl="10", entry_price="0", quantity="5")])
        assert stats["avg_roi_pct"] == 0.0

    def test_weighting_is_the_same_with_or_without_legs(self):
        """The ROI sums are built in the loop that runs before the legs /
        no-legs branch, so which grain net_pnl ends up on must not change
        what avg_roi_pct reports."""
        position = make_position(pnl="50", entry_price="100", quantity="1")
        closing_leg = make_leg(pnl="50", position_id=uuid.uuid4())

        with_legs = compute_core_stats([position], [closing_leg])
        without_legs = compute_core_stats([position])
        assert with_legs["avg_roi_pct"] == without_legs["avg_roi_pct"] == 50.0


class TestWinLossSplit:
    """wins/losses/scratches: the population behind win_rate_pct.

    Split out so the dashboard can say "47W / 89L" rather than a bare
    percentage. The case worth testing is the one a percentage cannot
    express -- a round trip that closed at exactly break-even is neither a
    win nor a loss, so the two counts are not required to sum to
    total_trades.
    """

    def test_counts_both_populations(self):
        stats = compute_core_stats([
            make_position(pnl="100", entry_price="10", quantity="10"),
            make_position(pnl="50", entry_price="10", quantity="10"),
            make_position(pnl="-30", entry_price="10", quantity="10"),
        ])

        assert stats["wins"] == 2
        assert stats["losses"] == 1
        assert stats["scratches"] == 0

    def test_a_break_even_round_trip_is_a_scratch_not_a_loss(self):
        """The whole reason `scratches` is reported rather than derived.

        Absorbing it into losses would report a loss that never happened;
        leaving it unnamed would make wins + losses silently disagree with
        total_trades on screen.
        """
        stats = compute_core_stats([
            make_position(pnl="100", entry_price="10", quantity="10"),
            make_position(pnl="0", entry_price="10", quantity="10"),
            make_position(pnl="-30", entry_price="10", quantity="10"),
        ])

        assert stats["wins"] == 1
        assert stats["losses"] == 1
        assert stats["scratches"] == 1
        assert stats["wins"] + stats["losses"] != stats["total_trades"]

    def test_the_three_always_partition_total_trades(self):
        """Every round trip lands in exactly one bucket, so the sum is exact."""
        stats = compute_core_stats([
            make_position(pnl=pnl, entry_price="10", quantity="10")
            for pnl in ("100", "0", "-30", "0", "7", "-1")
        ])

        assert stats["wins"] + stats["losses"] + stats["scratches"] == stats["total_trades"]
        assert stats["scratches"] == 2

    def test_scratches_dilute_win_rate_rather_than_leaving_the_denominator(self):
        """win_rate_pct is wins/total_trades -- a scratch is not a free pass.

        Pinned because the alternative reading (wins / (wins + losses)) would
        report 100% here, and the card shows the percentage and the split
        side by side where any disagreement is visible.
        """
        stats = compute_core_stats([
            make_position(pnl="100", entry_price="10", quantity="10"),
            make_position(pnl="0", entry_price="10", quantity="10"),
        ])

        assert stats["win_rate_pct"] == 50.0
        assert stats["scratches"] == 1

    def test_empty_input_carries_the_keys_rather_than_omitting_them(self):
        """The early return is a second, hand-maintained copy of the shape."""
        stats = compute_core_stats([])

        assert stats["wins"] == 0
        assert stats["losses"] == 0
        assert stats["scratches"] == 0

    def test_legs_without_positions_still_reports_the_keys(self):
        """Money banked out of a position still open: no round trips to count.

        Skips the early return (legs are truthy) but never enters the
        position loop, which is the one path where the counters and
        total_trades are all zero for different reasons.
        """
        stats = compute_core_stats([], [make_leg("-30")])

        assert stats["total_trades"] == 0
        assert stats["wins"] == 0
        assert stats["losses"] == 0
        assert stats["scratches"] == 0
        assert stats["open_run_pnl"] == -30.0


class TestReviewedTradeQuantity:
    """quantity has to survive a fractional share, not truncate it to 0."""

    def test_the_field_is_a_decimal_not_an_int(self):
        """analytics.py uses `from __future__ import annotations`, so a
        dataclass field's annotation is stored as the string it was written
        as, not the evaluated type object."""
        matched = next(f for f in dataclasses.fields(ReviewedTrade) if f.name == "quantity")
        assert matched.type == "Decimal"

    def test_a_fractional_quantity_survives_construction(self):
        trade = dataclasses.replace(make_trade(), quantity=Decimal("0.65"))
        assert trade.quantity == Decimal("0.65")

    def test_load_reviewed_trades_converts_without_truncating(self):
        """Pins the exact expression load_reviewed_trades uses. This file has
        no DB session to drive the function end to end, so the conversion is
        pinned structurally here and exercised directly below: `int(x or 0)`
        truncates 0.65 to 0; `Decimal(str(x or 0))` does not."""
        source = inspect.getsource(load_reviewed_trades)
        assert "quantity=Decimal(str(position.quantity or 0))" in source
        assert "quantity=int(" not in source

    @pytest.mark.parametrize(
        "raw,expected",
        [
            (Decimal("0.65"), Decimal("0.65")),
            (Decimal("10"), Decimal("10")),
            (None, Decimal("0")),
            (Decimal("0"), Decimal("0")),
        ],
    )
    def test_the_conversion_itself_for_every_shape_of_input(self, raw, expected):
        """The expression load_reviewed_trades uses, exercised directly."""
        assert Decimal(str(raw or 0)) == expected
