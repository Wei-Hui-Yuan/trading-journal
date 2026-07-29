"""An execution can be half consumed, and the other half is a live position.

`list_round_trips` decided what was still open by asking whether an execution
appeared in `position_fills` at all. That reads as "has it been matched", and
for most fills it is the same question.

An oversell is not most fills. BUY 10 then SELL 15 closes the long with that
sell AND opens a 5-share short with what is left over, so the sell is recorded
as a 10-share CLOSE while five of its shares are a live position. Present in
the table, therefore skipped, therefore the short appeared nowhere in the app
-- not as a row, not in exposure, not in any fill list -- while `/api/trades`
reported it as matched.

The matcher had it the whole time in `MatchingResult.open_lots`. The endpoint
just never asked, and membership cannot answer the question anyway: the same
sell is recorded once while the short is open (CLOSE 10) and twice once it is
covered (CLOSE 10 + OPEN 5). Only the quantity tells those apart.
"""

import asyncio
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

os.environ.setdefault("CORS_ALLOW_ORIGINS", "https://trading-journal-test.vercel.app")

import main  # noqa: E402
from services.matching_engine import Execution, match_executions  # noqa: E402

run = asyncio.run

T0 = datetime(2026, 6, 1, 14, 0, tzinfo=timezone.utc)
A, B, C = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


def ex(trade_id, direction, qty, price, minutes):
    return Execution(
        trade_id=trade_id, ticker="ACME", direction=direction,
        quantity=Decimal(str(qty)), price=Decimal(str(price)),
        executed_at=T0 + timedelta(minutes=minutes),
    )


@dataclass
class _Trade:
    id: uuid.UUID
    quantity: Decimal


@dataclass
class _Rows:
    rows: list

    def all(self):
        return self.rows


@dataclass
class _Session:
    """Returns (trade_id, summed quantity) pairs, as the grouped query does."""

    sums: list = field(default_factory=list)

    async def execute(self, _stmt):
        return _Rows(self.sums)


# ---------------------------------------------------------------------------
# The premise: the matcher already knows
# ---------------------------------------------------------------------------


def test_the_oversell_leaves_a_live_short_the_matcher_tracks():
    result = match_executions([
        ex(A, "BUY", 10, 100, 0),
        ex(B, "SELL", 15, 110, 60),
    ])
    assert len(result.positions) == 1, "the long closed"
    assert result.open_quantity == Decimal("5"), "and five shares are short"
    assert result.open_lots[0].execution.trade_id == B
    assert result.open_lots[0].execution.direction == "SELL"


def test_the_closed_round_trip_only_claims_the_shares_it_closed():
    """Which is why the remainder has to be found somewhere else: the position
    row is right, it is simply not the whole story of that execution."""
    position = match_executions([
        ex(A, "BUY", 10, 100, 0), ex(B, "SELL", 15, 110, 60),
    ]).positions[0]
    close = next(f for f in position.fills if f.role == "CLOSE")
    assert close.trade_id == B
    assert close.quantity == Decimal("10"), "10 of the sell's 15 shares"


# ---------------------------------------------------------------------------
# Quantity, not membership
# ---------------------------------------------------------------------------


def test_consumed_quantity_is_summed_per_execution():
    session = _Session(sums=[(A, Decimal("10")), (B, Decimal("10"))])
    assert run(main._consumed_quantity_by_trade(session)) == {
        A: Decimal("10"), B: Decimal("10")
    }


def test_an_execution_no_round_trip_touched_is_wholly_open():
    trade = _Trade(id=A, quantity=Decimal("10"))
    assert main._unmatched_quantity(trade, {}) == Decimal("10")
    assert main._is_fully_matched(trade, {}) is False


def test_a_fully_consumed_execution_is_matched_and_not_open():
    trade = _Trade(id=A, quantity=Decimal("10"))
    consumed = {A: Decimal("10")}
    assert main._unmatched_quantity(trade, consumed) == Decimal("0")
    assert main._is_fully_matched(trade, consumed) is True


def test_the_flipping_sell_is_open_for_its_remainder():
    """The bug, stated as an assertion. Membership said matched; the honest
    answer is that five of its fifteen shares are still a position."""
    trade = _Trade(id=B, quantity=Decimal("15"))
    consumed = {B: Decimal("10")}
    assert main._unmatched_quantity(trade, consumed) == Decimal("5")
    assert main._is_fully_matched(trade, consumed) is False


def test_once_the_short_is_covered_the_sell_is_fully_matched():
    """The other half of why this must be a quantity. Covering the short adds
    an OPEN 5 row for the SAME execution, so the sum reaches 15 and nothing is
    left open -- a state membership reported identically to the one above."""
    covered = match_executions([
        ex(A, "BUY", 10, 100, 0),
        ex(B, "SELL", 15, 110, 60),
        ex(C, "BUY", 5, 105, 120),
    ])
    total_for_b = sum(
        f.quantity
        for position in covered.positions
        for f in position.fills
        if f.trade_id == B
    )
    assert total_for_b == Decimal("15")
    assert main._is_fully_matched(_Trade(id=B, quantity=Decimal("15")),
                                  {B: total_for_b}) is True
    assert covered.open_quantity == Decimal("0")


def test_over_consumption_never_reports_negative_exposure():
    """Defensive: a remainder below zero would render as a position held the
    wrong way round rather than as no position."""
    trade = _Trade(id=A, quantity=Decimal("10"))
    assert main._unmatched_quantity(trade, {A: Decimal("12")}) == Decimal("0")
    assert main._is_fully_matched(trade, {A: Decimal("12")}) is True


@pytest.mark.parametrize("size,consumed,expected", [
    ("0.25", "0", "0.25"),        # fractional, untouched
    ("0.25", "0.1", "0.15"),      # fractional, part closed
    ("15", "10", "5"),            # the oversell
    ("15", "15", "0"),            # covered
])
def test_remainders_are_exact_at_fractional_sizes(size, consumed, expected):
    """Fractional fills are the norm on this account, so the arithmetic has to
    hold in Decimal rather than drift through float."""
    trade = _Trade(id=A, quantity=Decimal(size))
    assert main._unmatched_quantity(trade, {A: Decimal(consumed)}) == Decimal(expected)
