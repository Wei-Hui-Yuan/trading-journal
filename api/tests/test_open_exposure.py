"""Cost basis of what is still held.

Open exposure has no `positions` row, so it is reconstructed from the fills FIFO
never paired off. This number has now been wrong twice, in different ways, so
the broker's own figure is pinned here rather than a convention argued from
first principles.

  1. It averaged every same-direction fill, counting shares already sold as
     though they were still held. A real MSFT position reported 415.45 -- the
     mean of all 17 shares ever bought -- when only 2 remained.

  2. It then used AVERAGE-COST over the remainder, on the stated grounds that
     average-cost is "what the broker reports". Nobody checked that against a
     statement. It is not: IBKR reports a FIFO basis. Average-cost said 409.40
     for those same 2 shares where the statement says 407.298903 -- two dollars
     a share, on the one figure the journal exists to let you reconcile.

So: FIFO, with the acquiring commission capitalised into the basis, because the
broker does both. `test_msft_matches_the_ibkr_statement` is the anchor -- it
holds the real fills and the real number off the Activity Statement for
1 January - 28 July 2026.
"""

import os
from decimal import Decimal

os.environ.setdefault("CORS_ALLOW_ORIGINS", "https://trading-journal-test.vercel.app")

from services.matching_engine import replay_open_exposure  # noqa: E402


def D(v) -> Decimal:
    return Decimal(str(v))


def replay(rows):
    """Price-only, for the cases that are about lot selection rather than cost."""
    return replay_open_exposure((d, D(q), D(p)) for d, q, p in rows)


def replay_with_commission(rows):
    return replay_open_exposure((d, D(q), D(p), D(c)) for d, q, p, c in rows)


# The real sequence, from the live ledger.
MSFT = [
    ("BUY", 4, 438.80),
    ("SELL", 2, 439.08),
    ("BUY", 6, 409.40),
    ("SELL", 6, 411.00),
    ("BUY", 3, 407.30),
    ("BUY", 4, 407.30),
    ("SELL", 7, 405.80),
]


# ---------------------------------------------------------------------------
# The anchor: agree with the statement
# ---------------------------------------------------------------------------


def test_msft_matches_the_ibkr_statement():
    """2 shares at 407.30 on a FIFO basis.

    IBKR's Activity Statement for the same period reads 407.298903 for this
    position -- the fill price plus the small commission REBATE on the parcel
    that survived, which is why the figure sits just below 407.30.
    """
    result = replay(MSFT)
    assert result.net_quantity == D(2)
    assert result.average_cost == D("407.3000")


def test_the_capitalised_commission_reproduces_the_statement_exactly():
    """With the rebate the broker actually applied, to four places."""
    priced = [
        (side, qty, price, "-0.0021944" if (side, qty, price) == ("BUY", 4, 407.30) else 0)
        for side, qty, price in MSFT
    ]
    assert replay_with_commission(priced).average_cost == D("407.2995")


def test_msft_is_not_the_average_cost_figure():
    """Regression 2. Average-cost said 409.40; the statement says it is not."""
    assert replay(MSFT).average_cost != D("409.4000")


def test_msft_is_not_the_mean_of_every_buy():
    """Regression 1. 7062.70 / 17 = 415.4529..."""
    assert replay(MSFT).average_cost != D("415.4529")


# ---------------------------------------------------------------------------
# Commission is part of what the shares cost
# ---------------------------------------------------------------------------


def test_commission_is_capitalised_into_the_basis():
    """A 10-share buy at 100 costing 5.00 in commission is 100.50 a share --
    which is how the broker reports it, and why the journal's open rows read a
    few cents below the statement before this."""
    result = replay_with_commission([("BUY", 10, 100, 5)])
    assert result.average_cost == D("100.5000")


def test_a_rebate_puts_the_basis_below_the_fill_price():
    """IBKR really does pay rebates -- 10 of this account's 329 fills carry
    one -- so the sign has to be honoured rather than absolute-valued."""
    result = replay_with_commission([("BUY", 10, 100, "-2.00")])
    assert result.average_cost == D("99.8000")


def test_only_the_acquiring_commission_is_capitalised():
    """The commission on a SALE is a cost of exiting, already inside that
    sale's realised P&L. Counting it here would charge it twice."""
    result = replay_with_commission([("BUY", 10, 100, 0), ("SELL", 5, 200, "99.00")])
    assert result.net_quantity == D(5)
    assert result.average_cost == D("100.0000")


def test_commission_travels_with_the_shares_it_bought():
    """Sell half and the surviving half keeps its own share of the cost, not
    all of it."""
    result = replay_with_commission([("BUY", 10, 100, "4.00"), ("SELL", 5, 200, 0)])
    assert result.average_cost == D("100.4000"), "0.40 a share, not 0.80"


def test_a_caller_with_no_commission_data_still_works():
    """Backtests and previews pass three-tuples; they get the price-only basis
    rather than a crash."""
    assert replay([("BUY", 10, 100)]).average_cost == D("100.0000")


# ---------------------------------------------------------------------------
# Lot selection
# ---------------------------------------------------------------------------


def test_selling_part_does_not_move_the_basis():
    """Selling half a position does not change what the other half cost."""
    before = replay([("BUY", 10, 100)])
    after = replay([("BUY", 10, 100), ("SELL", 5, 250)])
    assert before.average_cost == after.average_cost == D("100.0000")
    assert after.net_quantity == D(5)


def test_the_oldest_parcel_goes_first():
    """The FIFO claim, stated on its own. Buy at 100 then at 200, sell 10, and
    what remains is the 200 lot -- not a blend."""
    result = replay([("BUY", 10, 100), ("BUY", 10, 200), ("SELL", 10, 150)])
    assert result.net_quantity == D(10)
    assert result.average_cost == D("200.0000")


def test_scaling_in_weights_the_surviving_parcels():
    result = replay([("BUY", 10, 100), ("BUY", 10, 200)])
    assert result.net_quantity == D(20)
    assert result.average_cost == D("150.0000")


def test_a_sale_spanning_two_parcels_leaves_the_remainder_of_the_second():
    result = replay([("BUY", 6, 100), ("BUY", 4, 200), ("SELL", 8, 150)])
    assert result.net_quantity == D(2)
    assert result.average_cost == D("200.0000")


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


def test_shorts_are_selected_fifo_too():
    """Two short parcels, partly covered. FIFO retires the older one first, so
    5 of the 50 lot survive alongside all 10 of the 70 lot:
    (5*50 + 10*70) / 15 = 63.3333. Average-cost said 60.00."""
    result = replay([("SELL", 10, 50), ("SELL", 10, 70), ("BUY", 5, 40)])
    assert result.net_quantity == D(-15)
    assert result.average_cost == D("63.3333")


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


# ---------------------------------------------------------------------------
# The acquisition premium the caller supplies
# ---------------------------------------------------------------------------
#
# `_acquisition_premium` decides what goes into the 4th tuple element. It
# prefers IBKR's own `cost` on a BUY over our commission column, because IBKR
# Singapore charges 9% GST on commission and folds it into the basis while
# reporting it only as a separate "Sales Tax" line. Commission alone left NFLX
# reading 94.5299 against a statement figure of 94.540340167.


def _trade(direction, quantity, price, commission, broker_cost=None):
    import main

    return main.Trade(
        ticker="X", direction=direction, style="Unclassified",
        quantity=Decimal(str(quantity)), actual_entry=Decimal(str(price)),
        commission=Decimal(str(commission)),
        broker_cost_basis=None if broker_cost is None else Decimal(str(broker_cost)),
    )


def test_a_buy_prefers_the_brokers_all_in_cost():
    """10 shares at 100 with a broker cost of 1005.45 -> 5.45 of premium, which
    is commission plus the tax on it. No rate appears in our code."""
    import main

    t = _trade("BUY", 10, 100, commission="5.00", broker_cost="1005.45")
    assert main._acquisition_premium(t, Decimal("10")) == Decimal("5.45")


def test_the_premium_scales_to_the_unconsumed_remainder():
    """A fill half-eaten by a closed round trip contributes half its cost here
    and half to that round trip's P&L."""
    import main

    t = _trade("BUY", 10, 100, commission="5.00", broker_cost="1005.45")
    assert main._acquisition_premium(t, Decimal("4")) == Decimal("2.18")


def test_a_sell_falls_back_to_commission():
    """On a SELL, IBKR's `cost` is the basis RELIEVED, not proceeds -- it says
    nothing about what opening a short cost, so it must not be used as if it
    did."""
    import main

    t = _trade("SELL", 10, 100, commission="5.00", broker_cost="-990.00")
    assert main._acquisition_premium(t, Decimal("10")) == Decimal("5.00")


def test_a_missing_broker_cost_falls_back_to_commission():
    """REPAIR- fills and anything outside the Flex window have none."""
    import main

    t = _trade("BUY", 10, 100, commission="5.00", broker_cost=None)
    assert main._acquisition_premium(t, Decimal("10")) == Decimal("5.00")


def test_a_zero_quantity_fill_contributes_nothing():
    import main

    t = _trade("BUY", 0, 100, commission="5.00")
    assert main._acquisition_premium(t, Decimal("0")) == Decimal("0")
