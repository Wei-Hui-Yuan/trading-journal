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
    def test_one_sell_closes_two_lots_fifo_order(self):
        r = match_executions(
            [
                ex("BUY", 50, 100, at(15, 9)),
                ex("BUY", 50, 110, at(15, 9, 30)),
                ex("SELL", 100, 120, at(15, 11)),
            ]
        )
        assert len(r.positions) == 2
        # Oldest lot pairs first.
        assert r.positions[0].entry_price == Decimal("100")
        assert r.positions[1].entry_price == Decimal("110")
        assert r.total_realized_pnl == Decimal("1500")
        assert r.open_quantity == 0

    def test_partial_close_leaves_remainder_open(self):
        r = match_executions(
            [ex("BUY", 100, 100, at(15, 9)), ex("SELL", 30, 105, at(15, 10))]
        )
        assert r.positions[0].quantity == 30
        assert r.open_quantity == 70

    def test_oversell_flips_to_short(self):
        r = match_executions(
            [ex("BUY", 40, 100, at(15, 9)), ex("SELL", 100, 105, at(15, 10))]
        )
        assert r.positions[0].quantity == 40
        assert r.open_quantity == 60
        assert r.open_lots[0].execution.direction == "SELL"


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
