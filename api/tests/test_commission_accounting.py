"""Realised P&L is what reached the account, not what the price did.

The engine computed (exit - entry) x quantity and stored it as `realized_pnl`.
That is GROSS. Commission was parsed off every Flex statement, staged into
ibkr_executions.commission on all 328 fills, and then read by nothing -- so net
P&L, profit factor, expectancy, drawdown, the equity curve and win rate were
every one of them computed before costs.

Win rate is the one that misleads rather than merely overstates: a trade that
made $0.40 on the price and paid $0.36 to trade counted as a full win. On this
account $102.56 of commission sits against $190.13 of gross losses, and a
0.1-share FUTU fill paid 1.00% of notional.

Two things below are worth more than the arithmetic. The sign, because IBKR
reports a charge as negative but reports a REBATE as positive, and abs() would
book those backwards. And the identity gross - commission = net, because three
figures on screen that do not add up are worse than one figure that was wrong
quietly.
"""

import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

os.environ.setdefault("CORS_ALLOW_ORIGINS", "https://trading-journal-test.vercel.app")

from services.ibkr_parser import ParsedExecution  # noqa: E402
from services.matching_engine import Execution, match_executions  # noqa: E402

T0 = datetime(2026, 6, 1, 14, 0, tzinfo=timezone.utc)


def ex(trade_id, direction, qty, price, minutes, commission="0"):
    return Execution(
        trade_id=trade_id,
        ticker="ACME",
        direction=direction,
        quantity=Decimal(str(qty)),
        price=Decimal(str(price)),
        executed_at=T0 + timedelta(minutes=minutes),
        commission=Decimal(str(commission)),
    )


def only(result):
    assert len(result.positions) == 1, f"expected 1 round trip, got {len(result.positions)}"
    return result.positions[0]


# ---------------------------------------------------------------------------
# The sign, which is where this goes wrong quietly
# ---------------------------------------------------------------------------


def test_a_charge_arrives_negative_and_becomes_a_positive_cost():
    """IBKR reports ibCommission as a debit. The journal wants the number it
    subtracts, so the sign flips exactly once, at the parser."""
    parsed = ParsedExecution(
        transaction_id="1", symbol="ACME", quantity=Decimal("10"),
        price=Decimal("100"), commission=Decimal("-0.35"), execution_time=T0,
    )
    assert parsed.commission_cost == Decimal("0.35")


def test_a_rebate_arrives_positive_and_stays_a_credit():
    """The reason this is negation and not abs().

    10 of the 328 fills on this account report a POSITIVE ibCommission -- a
    rebate, passed through from the exchange under tiered pricing. abs() books
    each of them as a charge, so $0.96 of money received becomes $0.96 of money
    paid: a $1.92 error, in the wrong direction, on the exact figure this
    change exists to make honest.
    """
    parsed = ParsedExecution(
        transaction_id="2", symbol="ACME", quantity=Decimal("90"),
        price=Decimal("16.36"), commission=Decimal("0.088020"), execution_time=T0,
    )
    assert parsed.commission_cost == Decimal("-0.088020")
    assert parsed.commission_cost < 0, "a rebate must not read as a cost"


def test_an_unpriced_fill_costs_nothing_rather_than_nothing_known():
    """None would propagate through the engine as a null. Zero is the same
    arithmetic and stays a number."""
    parsed = ParsedExecution(
        transaction_id="3", symbol="ACME", quantity=Decimal("1"),
        price=Decimal("10"), commission=None, execution_time=T0,
    )
    assert parsed.commission_cost == Decimal("0")


def test_a_rebate_makes_a_round_trip_worth_more_than_its_price_move():
    """End to end: a credit has to raise net P&L above gross, or the sign is
    still being swallowed somewhere downstream of the parser."""
    b, s = uuid.uuid4(), uuid.uuid4()
    position = only(match_executions([
        ex(b, "BUY", 10, 100, 0, commission="-0.05"),   # rebate received
        ex(s, "SELL", 10, 101, 60, commission="-0.03"),  # rebate received
    ]))
    assert position.gross_pnl == Decimal("10")
    assert position.commission == Decimal("-0.0800")
    assert position.realized_pnl == Decimal("10.0800")
    assert position.realized_pnl > position.gross_pnl


# ---------------------------------------------------------------------------
# The arithmetic
# ---------------------------------------------------------------------------


def test_both_sides_are_charged():
    """A round trip pays to get in and to get out. Charging one side halves
    the cost of every trade in the journal."""
    b, s = uuid.uuid4(), uuid.uuid4()
    position = only(match_executions([
        ex(b, "BUY", 10, 100, 0, commission="1.00"),
        ex(s, "SELL", 10, 110, 60, commission="1.00"),
    ]))
    assert position.gross_pnl == Decimal("100")
    assert position.commission == Decimal("2.0000")
    assert position.realized_pnl == Decimal("98.0000")


def test_a_gross_win_can_be_a_net_loss():
    """The case that corrupts win rate, at this account's actual fill sizes.

    A quarter share moving $1.60 makes 40 cents. Two commissions at a dollar
    take it to a $1.60 loss -- and the old code recorded it as a winning trade
    at full weight in every ratio derived from wins.
    """
    b, s = uuid.uuid4(), uuid.uuid4()
    position = only(match_executions([
        ex(b, "BUY", 0.25, 100, 0, commission="1.00"),
        ex(s, "SELL", 0.25, 101.60, 60, commission="1.00"),
    ]))
    assert position.gross_pnl == Decimal("0.4000")
    assert position.commission == Decimal("2.0000")
    assert position.realized_pnl == Decimal("-1.6000")
    assert position.gross_pnl > 0 and position.realized_pnl < 0


def test_commission_is_apportioned_by_the_fraction_of_the_fill_consumed():
    """A 20-share buy closed by two 10-share sells pays half its commission to
    each. Charging the whole fill to the first leg would make scaling out look
    progressively cheaper as the position wound down."""
    b, s1, s2 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    position = only(match_executions([
        ex(b, "BUY", 20, 100, 0, commission="2.00"),
        ex(s1, "SELL", 10, 110, 60, commission="1.00"),
        ex(s2, "SELL", 10, 110, 120, commission="1.00"),
    ]))
    assert position.gross_pnl == Decimal("200")
    assert position.commission == Decimal("4.0000")
    assert position.realized_pnl == Decimal("196.0000")


def test_an_uneven_split_still_totals_the_whole_commission():
    """Thirds do not divide exactly in decimal. The residue has to land below
    the money column rather than at the cent, which is what quantizing once
    per round trip -- instead of once per leg -- buys."""
    b, s1, s2, s3 = (uuid.uuid4() for _ in range(4))
    position = only(match_executions([
        ex(b, "BUY", 3, 100, 0, commission="1.00"),
        ex(s1, "SELL", 1, 110, 60, commission="0"),
        ex(s2, "SELL", 1, 110, 120, commission="0"),
        ex(s3, "SELL", 1, 110, 180, commission="0"),
    ]))
    assert position.commission == Decimal("1.0000")


def test_only_the_shares_actually_closed_are_charged():
    """Half a position closed pays half the entry commission. The rest belongs
    to the shares still open and is charged when they close -- otherwise a
    scale-out books the full cost of a trade that is not finished."""
    b, s = uuid.uuid4(), uuid.uuid4()
    result = match_executions([
        ex(b, "BUY", 10, 100, 0, commission="2.00"),
        ex(s, "SELL", 5, 110, 60, commission="1.00"),
    ])
    assert result.positions == [], "nothing is flat, so nothing is emitted"
    # 5 of 10 shares closed: half the entry commission plus all of the exit.
    assert result.open_round_trip_realized_pnl == Decimal("48")  # 50 - 1.00 - 1.00
    assert result.open_quantity == Decimal("5")


def test_deferred_pnl_is_net_like_everything_else():
    """P&L banked by scaling out of a position that has not gone flat is still
    money that moved, and it was still charged."""
    b, s = uuid.uuid4(), uuid.uuid4()
    gross = match_executions([
        ex(b, "BUY", 10, 100, 0),
        ex(s, "SELL", 5, 110, 60),
    ]).open_round_trip_realized_pnl
    net = match_executions([
        ex(b, "BUY", 10, 100, 0, commission="2.00"),
        ex(s, "SELL", 5, 110, 60, commission="1.00"),
    ]).open_round_trip_realized_pnl
    assert gross == Decimal("50")
    assert net < gross


def test_a_fill_shared_by_two_round_trips_splits_its_commission():
    """The oversell that flips long to short closes one round trip and opens
    the next with the same execution, so its cost belongs to both -- 10 shares
    to the long, 5 to the short."""
    b, s, cover = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    result = match_executions([
        ex(b, "BUY", 10, 100, 0, commission="0"),
        ex(s, "SELL", 15, 110, 60, commission="1.50"),   # $0.10/share
        ex(cover, "BUY", 5, 105, 120, commission="0"),
    ])
    assert len(result.positions) == 2
    long_rt, short_rt = result.positions
    assert long_rt.commission == Decimal("1.0000")   # 10/15 of 1.50
    assert short_rt.commission == Decimal("0.5000")  # 5/15 of 1.50
    assert long_rt.commission + short_rt.commission == Decimal("1.5000")


# ---------------------------------------------------------------------------
# The identity, which is what makes three figures on screen trustworthy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "buy_commission,sell_commission",
    [("0", "0"), ("1.00", "1.00"), ("0.333333", "0.166667"), ("-0.05", "0.35")],
)
def test_gross_minus_commission_is_exactly_net(buy_commission, sell_commission):
    """Not approximately. These land in three separate NUMERIC(12,4) columns
    and are rendered side by side; a cent of disagreement between them reads as
    a bug in the journal, and would be one."""
    b, s = uuid.uuid4(), uuid.uuid4()
    position = only(match_executions([
        ex(b, "BUY", 7, 100, 0, commission=buy_commission),
        ex(s, "SELL", 7, 103.33, 60, commission=sell_commission),
    ]))
    assert position.gross_pnl - position.commission == position.realized_pnl


def test_a_fractional_fill_puts_digits_below_the_money_column_and_still_adds_up():
    """The case the identity is actually at risk in.

    0.33 shares moving $1.4444 makes $0.476652 -- six decimals into a
    NUMERIC(12,4) column. Rounding gross, commission and net independently on
    the way into the database can leave the three disagreeing by a hundredth of
    a cent; rounding gross and commission once and deriving net from the pair
    cannot.
    """
    b, s = uuid.uuid4(), uuid.uuid4()
    position = only(match_executions([
        ex(b, "BUY", 0.33, 100.1234, 0, commission="0.170000"),
        ex(s, "SELL", 0.33, 101.5678, 60, commission="0.130000"),
    ]))
    assert position.gross_pnl == Decimal("0.4767")  # 0.476652 rounded once
    assert position.commission == Decimal("0.3000")
    assert position.realized_pnl == Decimal("0.1767")
    assert position.gross_pnl - position.commission == position.realized_pnl
    # And each is exactly representable in the column it is stored in.
    for value in (position.gross_pnl, position.commission, position.realized_pnl):
        assert -value.as_tuple().exponent <= 4


def test_zero_commission_leaves_the_old_arithmetic_untouched():
    """Every caller that predates commissions -- backtests, the existing engine
    tests -- must keep getting exactly what it got before."""
    b, s = uuid.uuid4(), uuid.uuid4()
    position = only(match_executions([
        ex(b, "BUY", 10, 100, 0),
        ex(s, "SELL", 10, 110, 60),
    ]))
    assert position.realized_pnl == Decimal("100")
    assert position.gross_pnl == Decimal("100")
    assert position.commission == Decimal("0.0000")


def test_the_execution_default_keeps_commission_optional():
    """The dataclass field has to default, or every existing construction site
    -- and every test written before this -- breaks at once."""
    execution = Execution(
        trade_id=uuid.uuid4(), ticker="ACME", direction="BUY",
        quantity=Decimal("1"), price=Decimal("100"), executed_at=T0,
    )
    assert execution.commission == Decimal("0")
