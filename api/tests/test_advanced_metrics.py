"""Advanced performance metrics: R-multiple, slippage, expectancy."""

from decimal import Decimal

import pytest

from services.analytics import (
    ReviewedTrade,
    compute_advanced_metrics,
    compute_expectancy,
    compute_r_multiple,
    compute_slippage,
)


def make_trade(
    direction="BUY", entry="100", exit_=None, stop=None, planned=None, mistakes=None
):
    return ReviewedTrade(
        trade_id="t",
        ticker="AAA",
        direction=direction,
        quantity=100,
        actual_entry=Decimal(entry),
        exit_price=Decimal(exit_) if exit_ is not None else None,
        planned_entry=Decimal(planned) if planned is not None else None,
        stop_loss=Decimal(stop) if stop is not None else None,
        mistakes=mistakes or [],
        review_status="reviewed",
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
