"""A plan's risk has to agree with the plan's own numbers.

`risk_amount` was written once, from whatever the sizing calculator computed
when the plan was first saved, and PATCH applied only the fields it was sent.
So editing the quantity from 4 to 1 left the figure behind: the dock showed
"Risk $20.00" on a plan whose entry, stop and quantity -- printed on the line
directly below it -- risked $5.00.

Not only a display problem. Attaching a plan to a fill copies `risk_amount`
onto the trade, and that column is what turns an R-multiple back into money,
so the stale figure would have reported that trade's R in dollars four times
too large for good.

Deliberately unlike `trades.risk_amount`, which is a snapshot and must never be
recomputed -- a trade sized against a $2,500 account keeps reading as 1% of
$2,500 after the account grows. A plan is a live intention; it should describe
what it would do if taken now.
"""

import asyncio
import os
import uuid
from dataclasses import dataclass, field
from decimal import Decimal

import pytest

os.environ.setdefault("CORS_ALLOW_ORIGINS", "https://trading-journal-test.vercel.app")

import main  # noqa: E402

run = asyncio.run


@dataclass
class _Plan:
    """Stands in for the ORM row; only the risk inputs and outputs matter."""

    direction: str = "BUY"
    quantity: Decimal | None = Decimal("1")
    planned_entry: Decimal | None = Decimal("140")
    stop_loss: Decimal | None = Decimal("135")
    take_profit: Decimal | None = Decimal("200")
    risk_amount: Decimal | None = None
    risk_percent: Decimal | None = None


@dataclass
class _Scalars:
    value: object

    def first(self):
        return self.value


@dataclass
class _Session:
    """Answers the one query the helper makes: the account size."""

    account_size: Decimal | None = Decimal("2500")
    queries: int = 0

    async def execute(self, _stmt):
        self.queries += 1
        return _Result(self.account_size)


@dataclass
class _Result:
    value: object

    def scalars(self):
        return _Scalars(self.value)


def derive(plan, account_size=Decimal("2500")):
    session = _Session(account_size=account_size)
    run(main._derive_plan_risk(session, plan))
    return plan, session


# ---------------------------------------------------------------------------
# The case that was on screen
# ---------------------------------------------------------------------------


def test_the_plan_that_read_twenty_dollars_risks_five():
    """1 share, entry 140, stop 135. The stored figure said $20.00 because the
    plan had been sized for 4 shares before the quantity was edited down."""
    plan, _ = derive(_Plan(risk_amount=Decimal("20.00"), risk_percent=Decimal("0.80")))
    assert plan.risk_amount == Decimal("5.00")
    assert plan.risk_percent == Decimal("0.20")  # 5 / 2500


def test_editing_the_quantity_moves_the_risk_with_it():
    """The whole failure mode, in one assertion: risk follows size."""
    plan = _Plan(quantity=Decimal("4"))
    derive(plan)
    assert plan.risk_amount == Decimal("20.00")

    plan.quantity = Decimal("1")  # what the dock's edit form does
    derive(plan)
    assert plan.risk_amount == Decimal("5.00")


def test_widening_the_stop_moves_the_risk_with_it():
    """Prices are inputs too, not just size."""
    plan = _Plan(quantity=Decimal("2"), stop_loss=Decimal("135"))
    derive(plan)
    assert plan.risk_amount == Decimal("10.00")

    plan.stop_loss = Decimal("130")
    derive(plan)
    assert plan.risk_amount == Decimal("20.00")


# ---------------------------------------------------------------------------
# Direction
# ---------------------------------------------------------------------------


def test_a_short_risks_the_distance_up_to_its_stop():
    """A short's stop sits ABOVE the entry, so the subtraction reverses. Taking
    an absolute value would hide an inverted stop instead of refusing it."""
    plan, _ = derive(_Plan(
        direction="SELL",
        planned_entry=Decimal("140"),
        stop_loss=Decimal("145"),
        quantity=Decimal("3"),
    ))
    assert plan.risk_amount == Decimal("15.00")


@pytest.mark.parametrize(
    "direction,entry,stop",
    [
        ("BUY", Decimal("140"), Decimal("145")),   # long stopped above entry
        ("SELL", Decimal("140"), Decimal("135")),  # short stopped below entry
    ],
)
def test_a_stop_on_the_wrong_side_states_no_risk_at_all(direction, entry, stop):
    """Null, not a negative number. A negative risk reads as a guaranteed
    profit, and this is a data-entry error rather than a free trade."""
    plan, _ = derive(_Plan(
        direction=direction, planned_entry=entry, stop_loss=stop,
        risk_amount=Decimal("99.99"), risk_percent=Decimal("4.00"),
    ))
    assert plan.risk_amount is None
    assert plan.risk_percent is None


def test_a_stop_exactly_at_the_entry_is_not_a_zero_risk_trade():
    """Zero would claim the trade cannot lose. It cannot be sized either."""
    plan, _ = derive(_Plan(stop_loss=Decimal("140")))
    assert plan.risk_amount is None


# ---------------------------------------------------------------------------
# Incomplete plans
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("missing", ["quantity", "planned_entry", "stop_loss"])
def test_an_unsized_plan_states_no_risk(missing):
    """A plan is worth recording from the moment it has a ticker and a bias, so
    these arrive incomplete all the time. The stale figure has to be cleared
    rather than left standing next to fields that no longer support it."""
    plan = _Plan(risk_amount=Decimal("20.00"), risk_percent=Decimal("0.80"))
    setattr(plan, missing, None)
    derive(plan)
    assert plan.risk_amount is None
    assert plan.risk_percent is None


def test_a_zero_quantity_states_no_risk():
    plan, _ = derive(_Plan(quantity=Decimal("0")))
    assert plan.risk_amount is None


# ---------------------------------------------------------------------------
# The percentage
# ---------------------------------------------------------------------------


def test_the_percentage_is_of_the_account_it_is_a_percentage_of():
    plan, _ = derive(_Plan(quantity=Decimal("4")), account_size=Decimal("2500"))
    assert plan.risk_amount == Decimal("20.00")
    assert plan.risk_percent == Decimal("0.80")


def test_without_an_account_size_there_is_no_percentage():
    """Nulled rather than carried over. A percentage computed against a
    different risk_amount is worse than no percentage."""
    plan, _ = derive(
        _Plan(risk_percent=Decimal("0.80")), account_size=None
    )
    assert plan.risk_amount == Decimal("5.00")
    assert plan.risk_percent is None


def test_the_account_is_not_queried_when_there_is_no_risk_to_express():
    """An incomplete plan should not cost a round trip to the settings table
    just to be told there is nothing to divide."""
    _, session = derive(_Plan(quantity=None))
    assert session.queries == 0


def test_rounding_lands_on_cents():
    """risk_amount is NUMERIC(12,2); an unrounded value would be rounded by
    Postgres into something the UI never computed."""
    plan, _ = derive(_Plan(
        planned_entry=Decimal("140.005"),
        stop_loss=Decimal("135"),
        quantity=Decimal("3"),
    ))
    assert plan.risk_amount == Decimal("15.02")  # 5.005 x 3 = 15.015
    assert -plan.risk_amount.as_tuple().exponent <= 2
