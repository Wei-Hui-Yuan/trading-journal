"""FIFO matching engine: pairing, partial fills, and style classification."""

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from services.matching_engine import (
    STYLE_DAY_TRADE,
    STYLE_SCALP,
    STYLE_SWING_TRADE,
    Execution,
    classify_style,
    match_executions,
)


def at(day, hour=0, minute=0):
    return datetime(2025, 1, day, hour, minute, tzinfo=timezone.utc)


def ex(direction, qty, price, when):
    return Execution(uuid.uuid4(), "AAPL", direction, qty, Decimal(str(price)), when)


class TestStyleClassification:
    @pytest.mark.parametrize(
        "entry,exit_,expected",
        [
            (at(15, 9), at(15, 9, 30), STYLE_SCALP),
            (at(15, 9), at(15, 10, 59), STYLE_SCALP),
            (at(15, 9), at(15, 11), STYLE_DAY_TRADE),  # exactly 2h
            (at(15, 9), at(15, 15), STYLE_DAY_TRADE),
            (at(15, 9), at(17, 15), STYLE_SWING_TRADE),
        ],
    )
    def test_buckets(self, entry, exit_, expected):
        assert classify_style(entry, exit_) == expected

    def test_duration_beats_session_boundary(self):
        """A 90-minute hold across midnight is a scalp, not a swing."""
        assert classify_style(at(15, 23), at(16, 0, 30)) == STYLE_SCALP


class TestRoundTrips:
    def test_simple_long(self):
        r = match_executions(
            [ex("BUY", 100, 150, at(15, 9)), ex("SELL", 100, 155, at(15, 10))]
        )
        assert len(r.positions) == 1
        p = r.positions[0]
        assert p.direction == "LONG"
        assert p.realized_pnl == Decimal("500")
        assert r.open_quantity == 0

    def test_short_sell_first(self):
        r = match_executions(
            [ex("SELL", 50, 200, at(15, 9)), ex("BUY", 50, 190, at(15, 12))]
        )
        assert r.positions[0].direction == "SHORT"
        assert r.positions[0].realized_pnl == Decimal("500")

    def test_losing_trade_is_negative(self):
        r = match_executions(
            [ex("BUY", 100, 150, at(15, 9)), ex("SELL", 100, 145, at(15, 9, 45))]
        )
        assert r.positions[0].realized_pnl == Decimal("-500")


class TestPartialFills:
    def test_scaling_in_then_out_is_one_position(self):
        """Two entries and one exit is one idea, so one position.

        Previously this emitted a position per FIFO pairing, which counted a
        single trade twice in trade count and win rate.
        """
        r = match_executions(
            [
                ex("BUY", 50, 100, at(15, 9)),
                ex("BUY", 50, 110, at(15, 9, 30)),
                ex("SELL", 100, 120, at(15, 11)),
            ]
        )
        assert len(r.positions) == 1
        p = r.positions[0]
        assert p.quantity == 100
        assert p.entry_price == Decimal("105")  # quantity-weighted
        assert p.exit_price == Decimal("120")
        assert p.realized_pnl == Decimal("1500")
        assert r.open_quantity == 0

    def test_aggregation_does_not_change_the_money(self):
        """Weighted averages are for display; P&L still sums the legs."""
        r = match_executions(
            [
                ex("BUY", 3, "10.3333", at(15, 9)),
                ex("SELL", 1, "11.1111", at(15, 10)),
                ex("SELL", 2, "12.2222", at(15, 10, 30)),
            ]
        )
        expected = (Decimal("11.1111") - Decimal("10.3333")) * 1 + (
            Decimal("12.2222") - Decimal("10.3333")
        ) * 2
        assert r.positions[0].realized_pnl == expected

    def test_every_fill_is_retained_for_drill_down(self):
        r = match_executions(
            [
                ex("BUY", 3, 890, at(15, 9)),
                ex("SELL", 1, 885, at(15, 9, 10)),
                ex("SELL", 2, 886, at(15, 9, 20)),
            ]
        )
        fills = r.positions[0].fills
        assert [f.role for f in fills] == ["OPEN", "CLOSE", "CLOSE"]
        assert [f.quantity for f in fills] == [3, 1, 2]
        # Individual exit prices survive aggregation.
        assert {f.price for f in fills if f.role == "CLOSE"} == {
            Decimal("885"),
            Decimal("886"),
        }

    def test_partial_close_stays_open_and_defers_pnl(self):
        """Scaling out is not finishing: the trade is still on.

        Emitting a position here would put a half-finished idea in the review
        queue, so it waits -- but the banked P&L is reported rather than lost.
        """
        r = match_executions(
            [ex("BUY", 100, 100, at(15, 9)), ex("SELL", 30, 105, at(15, 10))]
        )
        assert r.positions == []
        assert r.open_quantity == 70
        assert r.open_round_trip_realized_pnl == Decimal("150")

    def test_fifo_order_decides_which_lot_closes(self):
        """A partial exit must consume the oldest lot, not the cheapest."""
        r = match_executions(
            [
                ex("BUY", 50, 100, at(15, 9)),
                ex("BUY", 50, 110, at(15, 9, 30)),
                ex("SELL", 50, 120, at(15, 11)),
            ]
        )
        # FIFO closes the 100 lot: (120-100)*50. LIFO would give 500.
        assert r.open_round_trip_realized_pnl == Decimal("1000")

    def test_reopening_after_flat_is_a_separate_position(self):
        """Flat resets the trade; the next entry is a new idea."""
        r = match_executions(
            [
                ex("BUY", 10, 100, at(15, 9)),
                ex("SELL", 10, 105, at(15, 10)),
                ex("BUY", 10, 106, at(15, 12)),
                ex("SELL", 10, 108, at(15, 13)),
            ]
        )
        assert len(r.positions) == 2
        assert [p.realized_pnl for p in r.positions] == [Decimal("50"), Decimal("20")]

    def test_oversell_flips_to_short(self):
        r = match_executions(
            [ex("BUY", 40, 100, at(15, 9)), ex("SELL", 100, 105, at(15, 10))]
        )
        assert r.positions[0].quantity == 40
        assert r.open_quantity == 60
        assert r.open_lots[0].execution.direction == "SELL"


class TestFractionalQuantities:
    """Most fills on this account are fractions of a share."""

    def test_fractional_round_trip(self):
        r = match_executions(
            [
                ex("BUY", Decimal("0.25"), "100.00", at(15, 9)),
                ex("SELL", Decimal("0.25"), "120.00", at(15, 12)),
            ]
        )
        p = r.positions[0]
        assert p.quantity == Decimal("0.25")
        assert p.realized_pnl == (Decimal("120.00") - Decimal("100.00")) * Decimal("0.25")

    def test_scaling_in_fractionally_weights_the_entry(self):
        r = match_executions(
            [
                ex("BUY", Decimal("0.1"), 1000, at(15, 9)),
                ex("BUY", Decimal("0.1"), 1200, at(16, 9)),
                ex("SELL", Decimal("0.2"), 1150, at(17, 9)),
            ]
        )
        assert len(r.positions) == 1
        p = r.positions[0]
        assert p.quantity == Decimal("0.2")
        assert p.entry_price == Decimal("1100")  # (1000 + 1200) / 2

    def test_partial_fractional_exit_leaves_a_fractional_remainder(self):
        r = match_executions(
            [
                ex("BUY", Decimal("0.5"), 100, at(15, 9)),
                ex("SELL", Decimal("0.2"), 110, at(15, 10)),
            ]
        )
        assert r.positions == []
        assert r.open_quantity == Decimal("0.3")

    def test_no_binary_float_drift(self):
        """0.1 + 0.2 must be 0.3 exactly; this is why quantity is Decimal."""
        r = match_executions(
            [
                ex("BUY", Decimal("0.1"), 100, at(15, 9)),
                ex("BUY", Decimal("0.2"), 100, at(15, 9, 30)),
                ex("SELL", Decimal("0.3"), 100, at(15, 11)),
            ]
        )
        assert r.positions[0].quantity == Decimal("0.3")
        assert r.open_quantity == Decimal("0")


class TestOrdering:
    def test_unsorted_input_is_sorted_chronologically(self):
        later = ex("SELL", 100, 155, at(15, 10))
        earlier = ex("BUY", 100, 150, at(15, 9))
        r = match_executions([later, earlier])
        assert r.positions[0].entry_price == Decimal("150")
        assert r.positions[0].realized_pnl == Decimal("500")


class TestEdgeCases:
    def test_empty_input(self):
        r = match_executions([])
        assert r.positions == []
        assert r.total_realized_pnl == Decimal("0")

    def test_decimal_precision_no_float_drift(self):
        r = match_executions(
            [ex("BUY", 3, "0.1", at(15, 9)), ex("SELL", 3, "0.3", at(15, 9, 30))]
        )
        assert r.positions[0].realized_pnl == Decimal("0.6")
