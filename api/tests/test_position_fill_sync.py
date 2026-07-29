"""A round trip's fills must describe the round trip above them.

A position is keyed by its FIRST open and LAST close, so a fill landing between
them joins it WITHOUT changing the pair. BUY 10 then SELL 15 stores a 10-share
round trip (A,B); a backdated BUY 5 makes it 15 shares -- still (A,B), so
nothing is stale, and the upsert refreshes quantity and P&L in place.

Its fills were not refreshed. They were written once, when the position was
created, and skipped ever after because re-inserting them "would conflict
anyway" -- true of the rows that already existed, and silent about the ones
that should now exist. So the repair fill got no row, and the closing fill kept
a quantity of 10 beneath a position asserting 15.

The money stayed right. What broke was everything derived from fill membership:
the drill-down contradicted its own position, `is_matched` read false for the
repair fill, and open exposure -- reconstructed from executions with no fill
row -- showed it a second time as a phantom open trade. Which made this the
ordinary outcome of the repair feature's entire purpose.
"""

import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

os.environ.setdefault("CORS_ALLOW_ORIGINS", "https://trading-journal-test.vercel.app")

import asyncio  # noqa: E402

from services.matching_engine import (  # noqa: E402
    Execution,
    MatchedPosition,
    PositionFill,
    _sync_position_fills,
    match_executions,
)

run = asyncio.run

T0 = datetime(2026, 6, 1, 14, 0, tzinfo=timezone.utc)
A, B, C = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
POSITION_ID = uuid.uuid4()


def ex(trade_id, direction, qty, price, minutes):
    return Execution(
        trade_id=trade_id, ticker="ACME", direction=direction,
        quantity=Decimal(str(qty)), price=Decimal(str(price)),
        executed_at=T0 + timedelta(minutes=minutes),
    )


def composition(position):
    return {(f.trade_id, f.role): f.quantity for f in position.fills}


# ---------------------------------------------------------------------------
# The premise: same pair, different contents
# ---------------------------------------------------------------------------


def test_a_fill_between_the_endpoints_joins_without_changing_the_pair():
    """Why staleness detection cannot catch this. The pair is the identity of
    the round trip, and a repair fill lands inside it by definition."""
    before = match_executions([
        ex(A, "BUY", 10, 100, 0),
        ex(B, "SELL", 15, 110, 60),
    ]).positions[0]
    after = match_executions([
        ex(A, "BUY", 10, 100, 0),
        ex(C, "BUY", 5, 90, 30),      # the repair fill, dated between them
        ex(B, "SELL", 15, 110, 60),
    ]).positions[0]

    assert (before.open_trade_id, before.close_trade_id) == (A, B)
    assert (after.open_trade_id, after.close_trade_id) == (A, B), "same pair"
    assert before.quantity == Decimal("10")
    assert after.quantity == Decimal("15"), "different trade"


def test_the_fills_change_even_though_the_pair_does_not():
    """Both halves of the change: a row appears, and an existing row's quantity
    moves. Writing only the missing one would still leave the sum wrong."""
    before = match_executions([
        ex(A, "BUY", 10, 100, 0), ex(B, "SELL", 15, 110, 60),
    ]).positions[0]
    after = match_executions([
        ex(A, "BUY", 10, 100, 0), ex(C, "BUY", 5, 90, 30),
        ex(B, "SELL", 15, 110, 60),
    ]).positions[0]

    assert composition(before) == {(A, "OPEN"): Decimal("10"), (B, "CLOSE"): Decimal("10")}
    assert composition(after) == {
        (A, "OPEN"): Decimal("10"),
        (C, "OPEN"): Decimal("5"),
        (B, "CLOSE"): Decimal("15"),
    }


def test_the_fills_of_a_round_trip_sum_to_its_quantity():
    """The invariant the drill-down relies on, on both sides of the repair."""
    for executions in (
        [ex(A, "BUY", 10, 100, 0), ex(B, "SELL", 15, 110, 60)],
        [ex(A, "BUY", 10, 100, 0), ex(C, "BUY", 5, 90, 30), ex(B, "SELL", 15, 110, 60)],
    ):
        position = match_executions(executions).positions[0]
        opens = sum(f.quantity for f in position.fills if f.role == "OPEN")
        closes = sum(f.quantity for f in position.fills if f.role == "CLOSE")
        assert opens == closes == position.quantity


# ---------------------------------------------------------------------------
# The sync itself
# ---------------------------------------------------------------------------


@dataclass
class _StoredFill:
    id: uuid.UUID
    position_id: uuid.UUID
    trade_id: uuid.UUID
    role: str
    quantity: Decimal
    price: Decimal


@dataclass
class _Rows:
    rows: list

    def all(self):
        return self.rows


@dataclass
class _Session:
    """Returns the stored fills once, then records what is written."""

    stored: list = field(default_factory=list)
    kinds: list = field(default_factory=list)

    async def execute(self, stmt):
        kind = type(stmt).__name__
        self.kinds.append(kind)
        return _Rows(self.stored if kind == "Select" else [])


def _position(fills):
    return MatchedPosition(
        ticker="ACME", direction="LONG",
        quantity=sum((f.quantity for f in fills if f.role == "OPEN"), Decimal("0")),
        open_trade_id=A, close_trade_id=B,
        entry_price=Decimal("100"), exit_price=Decimal("110"),
        entry_date=T0, exit_date=T0 + timedelta(minutes=60),
        realized_pnl=Decimal("0"), style="Scalp", fills=fills,
    )


def _fill(trade_id, role, qty, price="100"):
    return PositionFill(
        trade_id=trade_id, role=role, quantity=Decimal(str(qty)),
        price=Decimal(price), executed_at=T0,
    )


IDS = {(A, B): POSITION_ID}


def test_a_new_round_trip_gets_all_of_its_fills():
    session = _Session(stored=[])
    written, deleted = run(_sync_position_fills(
        session, [_position([_fill(A, "OPEN", 10), _fill(B, "CLOSE", 10)])], IDS
    ))
    assert (written, deleted) == (2, 0)
    assert "Insert" in session.kinds


def test_an_unchanged_round_trip_costs_one_read_and_no_writes():
    """The common case, and the reason this is affordable on every sync. One
    SELECT -- which was already being issued to decide what to skip -- and
    nothing else."""
    session = _Session(stored=[
        _StoredFill(uuid.uuid4(), POSITION_ID, A, "OPEN", Decimal("10"), Decimal("100")),
        _StoredFill(uuid.uuid4(), POSITION_ID, B, "CLOSE", Decimal("10"), Decimal("110")),
    ])
    written, deleted = run(_sync_position_fills(
        session,
        [_position([_fill(A, "OPEN", 10, "100"), _fill(B, "CLOSE", 10, "110")])],
        IDS,
    ))
    assert (written, deleted) == (0, 0)
    assert session.kinds == ["Select"], session.kinds


def test_the_repair_fill_is_added_and_the_close_is_corrected():
    """The reported bug. C had no row at all and B's quantity was stale, under
    a position that had already been refreshed to 15."""
    session = _Session(stored=[
        _StoredFill(uuid.uuid4(), POSITION_ID, A, "OPEN", Decimal("10"), Decimal("100")),
        _StoredFill(uuid.uuid4(), POSITION_ID, B, "CLOSE", Decimal("10"), Decimal("110")),
    ])
    written, deleted = run(_sync_position_fills(
        session,
        [_position([
            _fill(A, "OPEN", 10, "100"),
            _fill(C, "OPEN", 5, "90"),
            _fill(B, "CLOSE", 15, "110"),
        ])],
        IDS,
    ))
    assert written == 2, "the missing fill AND the corrected close"
    assert deleted == 0


def test_a_fill_that_left_the_round_trip_is_removed():
    """Otherwise the sum drifts the other way and the drill-down over-counts."""
    session = _Session(stored=[
        _StoredFill(uuid.uuid4(), POSITION_ID, A, "OPEN", Decimal("10"), Decimal("100")),
        _StoredFill(uuid.uuid4(), POSITION_ID, C, "OPEN", Decimal("5"), Decimal("90")),
        _StoredFill(uuid.uuid4(), POSITION_ID, B, "CLOSE", Decimal("15"), Decimal("110")),
    ])
    written, deleted = run(_sync_position_fills(
        session,
        [_position([_fill(A, "OPEN", 10, "100"), _fill(B, "CLOSE", 10, "110")])],
        IDS,
    ))
    assert deleted == 1
    assert written == 1, "B's quantity drops back to 10"
    assert "Delete" in session.kinds


def test_a_price_correction_rewrites_the_fill():
    """Editing a fill's price changes what the drill-down should show, even
    when the quantities are untouched."""
    session = _Session(stored=[
        _StoredFill(uuid.uuid4(), POSITION_ID, A, "OPEN", Decimal("10"), Decimal("100")),
        _StoredFill(uuid.uuid4(), POSITION_ID, B, "CLOSE", Decimal("10"), Decimal("110")),
    ])
    written, _ = run(_sync_position_fills(
        session,
        [_position([_fill(A, "OPEN", 10, "101.25"), _fill(B, "CLOSE", 10, "110")])],
        IDS,
    ))
    assert written == 1


def test_scale_matters_not_representation():
    """NUMERIC comes back as Decimal('10.00000000'); the engine computes
    Decimal('10'). Treating those as different would rewrite every fill on
    every sync forever."""
    session = _Session(stored=[
        _StoredFill(uuid.uuid4(), POSITION_ID, A, "OPEN",
                    Decimal("10.00000000"), Decimal("100.0000")),
        _StoredFill(uuid.uuid4(), POSITION_ID, B, "CLOSE",
                    Decimal("10.00000000"), Decimal("110.0000")),
    ])
    written, deleted = run(_sync_position_fills(
        session,
        [_position([_fill(A, "OPEN", 10, "100"), _fill(B, "CLOSE", 10, "110")])],
        IDS,
    ))
    assert (written, deleted) == (0, 0)


def test_nothing_upserted_means_nothing_to_sync():
    """No positions, no query. An empty IN () is not worth a round trip."""
    session = _Session(stored=[])
    assert run(_sync_position_fills(session, [], {})) == (0, 0)
    assert session.kinds == []
