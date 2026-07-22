"""Cost basis of what is still held.

Open exposure has no `positions` row, so it is reconstructed from the fills
FIFO never paired off. The bug this replaces averaged every same-direction
fill, which treats already-sold shares as though they were still held: a real
MSFT position reported 415.45 -- the mean of all 17 shares ever bought -- when
only 2 remained, at 409.40.

The method here is average-cost, matching what the broker reports. That
deliberately differs from the FIFO basis used to realise P&L; the two answer
different questions, and `test_average_cost_differs_from_fifo` pins the
difference so nobody "fixes" one into the other.
"""

import os
from decimal import Decimal

os.environ.setdefault("CORS_ALLOW_ORIGINS", "https://trading-journal-test.vercel.app")

from services.matching_engine import replay_open_exposure  # noqa: E402


def D(v) -> Decimal:
    return Decimal(str(v))


def replay(rows):
    return replay_open_exposure((d, D(q), D(p)) for d, q, p in rows)


# The real sequence, from the live ledger.
MSFT = [
    ("BUY", 4, 438.80),
    ("SELL", 2, 439.08),
    ("BUY", 6, 409.40),
    ("SELL", 6, 411.00),
    ("BUY", 4, 407.30),
    ("BUY", 3, 407.30),
    ("SELL", 7, 405.80),
]


def test_msft_reports_what_is_actually_held():
    result = replay(MSFT)
    assert result.net_quantity == D(2)
    assert result.average_cost == D("409.4000")


def test_msft_is_not_the_mean_of_every_buy():
    """Guards the exact regression: 7062.70 / 17 = 415.4529..."""
    assert replay(MSFT).average_cost != D("415.4529")


def test_average_cost_differs_from_fifo_and_that_is_intended():
    """FIFO would say the 2 remaining shares are the last 2 bought, at 407.30.

    Average-cost says 409.40. Both are defensible; the app shows the broker's
    convention so the number reconciles against IBKR at a glance.
    """
    assert replay(MSFT).average_cost == D("409.4000")
    assert replay(MSFT).average_cost != D("407.3000")


def test_selling_part_does_not_move_the_basis():
    """Selling half a position does not change what the other half cost."""
    before = replay([("BUY", 10, 100)])
    after = replay([("BUY", 10, 100), ("SELL", 5, 250)])
    assert before.average_cost == after.average_cost == D("100.0000")
    assert after.net_quantity == D(5)


def test_scaling_in_moves_the_basis():
    result = replay([("BUY", 10, 100), ("BUY", 10, 200)])
    assert result.net_quantity == D(20)
    assert result.average_cost == D("150.0000")


def test_flat_reports_zero_not_a_stale_average():
    result = replay([("BUY", 5, 100), ("SELL", 5, 120)])
    assert result.net_quantity == D(0)
    assert result.average_cost == D(0)


def test_crossing_through_flat_starts_a_fresh_basis():
    """Selling more than held flips to short; the long's cost must not carry."""
    result = replay([("BUY", 5, 100), ("SELL", 8, 90)])
    assert result.net_quantity == D(-3)
    # The 3 short shares were opened at 90, not at some blend involving 100.
    assert result.average_cost == D("90.0000")


def test_short_positions_average_the_same_way():
    result = replay([("SELL", 10, 50), ("SELL", 10, 70), ("BUY", 5, 40)])
    assert result.net_quantity == D(-15)
    assert result.average_cost == D("60.0000")


def test_fractional_quantities_survive():
    """trades.quantity is NUMERIC(18,8); 0.2-share fills are real here."""
    result = replay([("BUY", "0.2", 1555), ("BUY", "0.3", 1655)])
    assert result.net_quantity == D("0.5")
    assert result.average_cost == D("1615.0000")


def test_empty_is_flat():
    assert replay([]).net_quantity == D(0)


def test_zero_quantity_fills_are_ignored():
    result = replay([("BUY", 0, 100), ("BUY", 5, 200)])
    assert result.net_quantity == D(5)
    assert result.average_cost == D("200.0000")
