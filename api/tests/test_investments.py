"""Deriving a position from a ledger, and merging two sets of DCF inputs.

The two pieces of arithmetic the investment book cannot get wrong. Every
figure the portfolio table shows -- market value, unrealised P&L, weight,
discount to intrinsic value -- is built on `_derive_position`, and every
intrinsic value is built on `_merge_inputs`. Neither reaches a database or a
network, which is what makes them testable here rather than only in staging.

The valuation engine itself is covered in test_valuation.py; these tests care
about what gets HANDED to it.
"""

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("CORS_ALLOW_ORIGINS", "https://trading-journal-test.vercel.app")

import main  # noqa: E402

JAN = datetime(2026, 1, 15, tzinfo=timezone.utc)


def tx(kind, quantity=None, price=None, total=None, fees=0.0, day_offset=0):
    """One ledger row, with the same sign convention the endpoint writes."""
    if total is None and quantity is not None and price is not None:
        gross = quantity * price
        total = gross - fees if kind == "SELL" else -(gross + fees)
    return main.InvestmentTransaction(
        id=uuid.uuid4(),
        ticker="TEST",
        transaction_type=kind,
        quantity=quantity,
        price=price,
        total_amount=total,
        fees=fees,
        transaction_date=JAN + timedelta(days=day_offset),
        created_at=JAN + timedelta(days=day_offset),
        listed_currency="USD",
        exchange_rate=1,
        source="MANUAL",
    )


def derive(rows):
    return main._derive_position("TEST", rows)


# ---------------------------------------------------------------------------
# Accumulating a position
# ---------------------------------------------------------------------------


def test_a_single_purchase_costs_what_was_paid_including_fees():
    """The fee is part of what the shares cost. Excluding it understates the
    basis and overstates every gain computed from it afterwards."""
    position = derive([tx("BUY", 10, 100.0, fees=1.50)])
    assert position.quantity == pytest.approx(10)
    assert position.cost_basis == pytest.approx(1001.50)
    assert position.average_cost == pytest.approx(100.15)


def test_buying_twice_averages_the_cost():
    position = derive([
        tx("BUY", 10, 100.0, day_offset=0),
        tx("BUY", 10, 200.0, day_offset=30),
    ])
    assert position.quantity == pytest.approx(20)
    assert position.average_cost == pytest.approx(150.0)


def test_a_purchase_adds_to_cost_rather_than_subtracting_from_it():
    """total_amount is NEGATIVE on a buy -- money leaving the account. Summing
    it as signed would make every purchase reduce the basis, and a portfolio
    of nothing but buys would report a negative cost."""
    position = derive([tx("BUY", 10, 100.0)])
    assert position.cost_basis > 0


# ---------------------------------------------------------------------------
# Selling, and what it does to the basis
# ---------------------------------------------------------------------------


def test_a_partial_sale_leaves_the_average_cost_unchanged():
    """This is what makes it average-cost rather than FIFO. Selling half a
    position must not silently reprice the half still held."""
    position = derive([
        tx("BUY", 10, 100.0, day_offset=0),
        tx("BUY", 10, 200.0, day_offset=10),
        tx("SELL", 5, 250.0, day_offset=20),
    ])
    assert position.quantity == pytest.approx(15)
    assert position.average_cost == pytest.approx(150.0)
    assert position.cost_basis == pytest.approx(2250.0)


def test_a_sale_realises_the_gain_over_the_average_cost():
    position = derive([
        tx("BUY", 10, 100.0, day_offset=0),
        tx("SELL", 4, 150.0, day_offset=10),
    ])
    # 4 shares sold at 150 = 600 received, against 4 x 100 = 400 of basis.
    assert position.realized_pnl == pytest.approx(200.0)


def test_selling_everything_leaves_no_basis_attached_to_no_shares():
    """A rounding residue on a full exit would show a cost basis for a
    position that no longer exists, and an infinite average cost."""
    position = derive([
        tx("BUY", 3, 33.33, day_offset=0),
        tx("SELL", 3, 40.0, day_offset=10),
    ])
    assert position.quantity == 0.0
    assert position.cost_basis == 0.0
    assert position.average_cost is None


def test_fees_on_a_sale_reduce_what_was_realised():
    no_fee = derive([tx("BUY", 10, 100.0, day_offset=0),
                     tx("SELL", 10, 120.0, day_offset=5)])
    with_fee = derive([tx("BUY", 10, 100.0, day_offset=0),
                       tx("SELL", 10, 120.0, fees=5.0, day_offset=5)])
    assert with_fee.realized_pnl == pytest.approx(no_fee.realized_pnl - 5.0)


# ---------------------------------------------------------------------------
# Dividends and transfers
# ---------------------------------------------------------------------------


def test_a_dividend_is_income_not_a_purchase():
    """It moves cash without moving shares. Counting it as a buy would inflate
    both the quantity and the cost basis of the holding."""
    position = derive([
        tx("BUY", 10, 100.0, day_offset=0),
        tx("DIVIDEND", total=25.0, day_offset=40),
    ])
    assert position.quantity == pytest.approx(10)
    assert position.cost_basis == pytest.approx(1000.0)
    assert position.dividends == pytest.approx(25.0)


def test_a_transfer_arrives_with_its_cost_basis_intact():
    """A holding moved from another broker was bought somewhere this ledger
    cannot see. It is a purchase whose cash left the account elsewhere."""
    position = derive([tx("TRANSFER", 50, 20.0)])
    assert position.quantity == pytest.approx(50)
    assert position.cost_basis == pytest.approx(1000.0)
    assert position.average_cost == pytest.approx(20.0)


# ---------------------------------------------------------------------------
# Order
# ---------------------------------------------------------------------------


def test_transactions_are_applied_in_date_order_not_insertion_order():
    """A back-dated correction entered today still belongs where it happened.
    Applying it last would relieve cost at an average that did not exist yet."""
    ordered = derive([
        tx("BUY", 10, 100.0, day_offset=0),
        tx("BUY", 10, 200.0, day_offset=10),
        tx("SELL", 5, 250.0, day_offset=20),
    ])
    shuffled = derive([
        tx("SELL", 5, 250.0, day_offset=20),
        tx("BUY", 10, 200.0, day_offset=10),
        tx("BUY", 10, 100.0, day_offset=0),
    ])
    assert shuffled.quantity == pytest.approx(ordered.quantity)
    assert shuffled.cost_basis == pytest.approx(ordered.cost_basis)
    assert shuffled.realized_pnl == pytest.approx(ordered.realized_pnl)


def test_an_empty_ledger_is_a_flat_position_not_an_error():
    position = derive([])
    assert position.quantity == 0.0
    assert position.average_cost is None


# ---------------------------------------------------------------------------
# The cash figure the endpoint derives
# ---------------------------------------------------------------------------


def make(kind, **kw):
    body = {"ticker": "test", "transaction_type": kind,
            "transaction_date": JAN, **kw}
    return main.TransactionCreate(**body)


def test_a_buy_is_money_leaving_and_a_sell_is_money_arriving():
    assert make("BUY", quantity=10, price=100.0).resolved_total() == pytest.approx(-1000.0)
    assert make("SELL", quantity=10, price=100.0).resolved_total() == pytest.approx(1000.0)


def test_fees_push_both_directions_against_the_trader():
    assert make("BUY", quantity=10, price=100.0, fees=2.0).resolved_total() == pytest.approx(-1002.0)
    assert make("SELL", quantity=10, price=100.0, fees=2.0).resolved_total() == pytest.approx(998.0)


def test_a_supplied_total_is_authoritative_over_the_arithmetic():
    """A broker's cash figure is what actually moved; reproducing it from
    quantity and price will not always agree to the cent."""
    given = make("BUY", quantity=3, price=33.33, total_amount=-100.00)
    assert given.resolved_total() == pytest.approx(-100.00)


def test_the_ticker_is_normalised_so_one_holding_is_not_two():
    assert make("BUY", quantity=1, price=1.0).ticker == "TEST"


@pytest.mark.parametrize("kind", ["BUY", "SELL", "TRANSFER"])
def test_a_trade_without_a_quantity_is_refused(kind):
    with pytest.raises(ValueError):
        make(kind, price=100.0)


def test_a_dividend_carrying_a_quantity_is_refused():
    """A share dividend is not modelled, and treating it as a cash one would
    silently misstate the position."""
    with pytest.raises(ValueError):
        make("DIVIDEND", quantity=5, total_amount=25.0)


def test_a_dividend_needs_an_amount():
    with pytest.raises(ValueError):
        make("DIVIDEND")


def test_an_unknown_transaction_type_names_the_allowed_ones():
    with pytest.raises(ValueError) as caught:
        make("GIFT", quantity=1, price=1.0)
    assert "BUY" in str(caught.value)


# ---------------------------------------------------------------------------
# Merging fetched inputs with the user's corrections
# ---------------------------------------------------------------------------


def row(variant, **kw):
    return main.InvestmentValuationInput(
        ticker="TEST", variant=variant, region=kw.pop("region", "US"), **kw
    )


def test_with_no_override_the_fetched_values_stand():
    merged, overridden = main._merge_inputs(
        row("auto", base_flow=1000, growth_1_5=0.12), None
    )
    assert merged["base_flow"] == 1000
    assert merged["growth_1_5"] == 0.12
    assert overridden == []


def test_an_override_wins_field_by_field():
    """The point of the split: correcting the growth rate must not discard a
    freshly fetched cash flow."""
    merged, overridden = main._merge_inputs(
        row("auto", base_flow=1000, growth_1_5=0.12, beta=1.1),
        row("override", growth_1_5=0.08),
    )
    assert merged["growth_1_5"] == 0.08
    assert merged["base_flow"] == 1000
    assert merged["beta"] == 1.1
    assert overridden == ["growth_1_5"]


def test_a_null_in_the_override_falls_back_rather_than_blanking():
    """NULL means "I did not touch this", not "set this to nothing" -- so a
    partially filled override cannot erase the rest of the model's inputs."""
    merged, overridden = main._merge_inputs(
        row("auto", base_flow=1000, shares_outstanding=500),
        row("override", base_flow=None, shares_outstanding=600),
    )
    assert merged["base_flow"] == 1000
    assert merged["shares_outstanding"] == 600
    assert "base_flow" not in overridden


def test_an_unchanged_region_does_not_count_as_an_override():
    """region is NOT NULL in the table, so every override row carries one.
    Without this the UI would mark every ticker as user-modified."""
    _merged, overridden = main._merge_inputs(
        row("auto", base_flow=1000, region="US"),
        row("override", growth_1_5=0.08, region="US"),
    )
    assert "region" not in overridden
    assert overridden == ["growth_1_5"]


def test_a_genuinely_changed_region_is_reported():
    _merged, overridden = main._merge_inputs(
        row("auto", base_flow=1000, region="US"),
        row("override", region="HK"),
    )
    assert "region" in overridden


def test_an_override_with_no_auto_row_still_values():
    """A ticker the refresh has never reached, valued entirely by hand."""
    merged, overridden = main._merge_inputs(
        None, row("override", base_flow=500, shares_outstanding=100, growth_1_5=0.1)
    )
    assert merged["base_flow"] == 500
    assert set(overridden) >= {"base_flow", "shares_outstanding", "growth_1_5"}


# ---------------------------------------------------------------------------
# Valuing, and declining to
# ---------------------------------------------------------------------------


def holding(**kw):
    return main.InvestmentHolding(
        ticker="TEST", exchange_rate=kw.pop("exchange_rate", 1),
        current_price=kw.pop("current_price", None),
        is_valuable=kw.pop("is_valuable", True), **kw
    )


def test_a_complete_set_of_inputs_produces_a_value():
    result = main._value_holding(
        holding(current_price=100.0),
        {"base_flow": 1000.0, "shares_outstanding": 100.0, "growth_1_5": 0.10,
         "beta": 1.2, "total_debt": 0.0, "cash_and_st": 0.0, "region": "US",
         "discount_rate": None, "metric": "free_cash_flow"},
        [],
    )
    assert result["available"] is True
    assert result["average_intrinsic_value"] > 0
    assert result["premium_pct"] is not None


def test_a_missing_input_declines_to_value_and_names_what_is_missing():
    """Silence, not a zero. A zero intrinsic value renders as "worth nothing",
    which is an assertion about the business rather than about the data."""
    result = main._value_holding(
        holding(current_price=100.0),
        {"base_flow": 1000.0, "shares_outstanding": 100.0, "growth_1_5": None,
         "beta": None, "total_debt": 0.0, "cash_and_st": 0.0, "region": "US",
         "discount_rate": None, "metric": None},
        [],
    )
    assert result["available"] is False
    assert "growth_1_5" in result["missing"]


def test_without_a_price_there_is_no_premium_rather_than_zero_percent():
    """0% would read as "fairly priced" -- a claim, where None is the truth."""
    result = main._value_holding(
        holding(current_price=None),
        {"base_flow": 1000.0, "shares_outstanding": 100.0, "growth_1_5": 0.10,
         "beta": 1.2, "total_debt": 0.0, "cash_and_st": 0.0, "region": "US",
         "discount_rate": None, "metric": "free_cash_flow"},
        [],
    )
    assert result["available"] is True
    assert result["premium_pct"] is None


def test_a_pinned_discount_rate_overrides_the_derived_one():
    inputs = {"base_flow": 1000.0, "shares_outstanding": 100.0,
              "growth_1_5": 0.10, "beta": 1.2, "total_debt": 0.0,
              "cash_and_st": 0.0, "region": "US", "metric": "free_cash_flow"}
    derived = main._value_holding(holding(), {**inputs, "discount_rate": None}, [])
    pinned = main._value_holding(holding(), {**inputs, "discount_rate": 0.09}, [])
    assert pinned["discount_rate"] == pytest.approx(0.09)
    assert pinned["discount_rate"] != derived["discount_rate"]
    # A higher rate discounts future flows harder, so it must value lower.
    assert pinned["average_intrinsic_value"] < derived["average_intrinsic_value"]


def test_debt_and_cash_move_the_value_in_opposite_directions():
    base = {"base_flow": 1000.0, "shares_outstanding": 100.0, "growth_1_5": 0.10,
            "beta": 1.2, "region": "US", "discount_rate": None,
            "metric": "free_cash_flow", "total_debt": 0.0, "cash_and_st": 0.0}
    plain = main._value_holding(holding(), base, [])
    indebted = main._value_holding(holding(), {**base, "total_debt": 5000.0}, [])
    flush = main._value_holding(holding(), {**base, "cash_and_st": 5000.0}, [])
    assert indebted["average_intrinsic_value"] < plain["average_intrinsic_value"]
    assert flush["average_intrinsic_value"] > plain["average_intrinsic_value"]


# ---------------------------------------------------------------------------
# The guardrail
# ---------------------------------------------------------------------------


TRADING_NAMES = {
    "Trade", "Position", "PositionFill", "RealizedLeg", "PlannedTrade",
    "IBKRExecution", "SuppressedExecution", "PositionDiscipline",
    "ingest_ibkr", "run_matching_for_ticker",
}

INVESTMENT_NAMES = {
    "InvestmentHolding", "InvestmentTransaction", "InvestmentValuationInput",
    "_derive_position", "_derive_all_positions", "_merge_inputs",
    "_value_holding", "valuation_engine",
}


def _names_by_line() -> tuple[list[tuple[str, int]], int]:
    """Every identifier main.py actually references, with its line.

    Parsed rather than grepped. A substring search reports `DerivedPosition`
    as a reference to `Position`, and flags any comment that mentions the
    trading side -- but comments explaining the separation are exactly what
    should be encouraged. Only real name and attribute references count.
    """
    import ast
    import inspect

    source = inspect.getsource(main)
    boundary = next(
        i for i, line in enumerate(source.splitlines(), 1)
        if "# THE INVESTMENT BOOK" in line
    )

    found: list[tuple[str, int]] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Name):
            found.append((node.id, node.lineno))
        elif isinstance(node, ast.Attribute):
            found.append((node.attr, node.lineno))
    return found, boundary


def test_the_investment_section_touches_no_trading_table():
    """The whole point of migration 026's separate tables. If this fails, the
    investment book has grown a dependency on the journal and the two can no
    longer fail independently.
    """
    found, boundary = _names_by_line()
    offenders = [
        (name, line) for name, line in found
        if line >= boundary and name in TRADING_NAMES
    ]
    assert not offenders, f"investment section references trading models: {offenders}"


def test_no_trading_endpoint_calls_into_the_investment_book():
    """The other direction, and the one that would actually break the journal:
    nothing above the section may depend on anything inside it."""
    found, boundary = _names_by_line()
    offenders = [
        (name, line) for name, line in found
        if line < boundary and name in INVESTMENT_NAMES
    ]
    assert not offenders, f"trading journal references investment code: {offenders}"


def test_the_boundary_test_can_actually_fail():
    """A guardrail that cannot fire is decoration. Confirms both name sets
    appear somewhere in the file, so the two tests above are checking a real
    partition rather than passing on empty sets."""
    found, boundary = _names_by_line()
    names = {name for name, _ in found}
    assert TRADING_NAMES & names, "no trading models found at all"
    assert INVESTMENT_NAMES & names, "no investment models found at all"
    assert any(line >= boundary for _, line in found), "section boundary is at EOF"
