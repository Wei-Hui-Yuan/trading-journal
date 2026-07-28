"""One execution can belong to two round trips, and deleting is not local.

`DELETE /api/positions/{id}` removes the executions under a position, because
deleting the position alone would leave its fills behind for the next rebuild
to pair up again. It collects those executions from `position_fills` -- and a
single fill can appear there twice.

An oversell that flips long to short is the shape: BUY 10, SELL 15 closes the
long with that sell and leaves 5 shares of it open, so the same execution is
the CLOSE of one round trip and the OPEN of the next. Deleting either one's
executions destroys the other -- `position_fills.trade_id` is ON DELETE
CASCADE, so the neighbour loses its fills, and `positions.open_trade_id` was
only SET NULL, so its row survived asserting a P&L with nothing underneath it.

The neighbour's review is the part that cannot be rebuilt. Quantity, prices and
P&L all re-match from the fills; a grade and a post-mortem do not. So the
delete refuses by default and the client is expected to have asked
/delete-impact first and shown the user what else goes.
"""

import asyncio
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi import HTTPException

os.environ.setdefault("CORS_ALLOW_ORIGINS", "https://trading-journal-test.vercel.app")

import main  # noqa: E402
from services.matching_engine import Execution, match_executions  # noqa: E402

run = asyncio.run

T0 = datetime(2026, 6, 1, 14, 0, tzinfo=timezone.utc)


def ex(trade_id, direction, qty, price, minutes):
    return Execution(
        trade_id=trade_id,
        ticker="ACME",
        direction=direction,
        quantity=Decimal(str(qty)),
        price=Decimal(str(price)),
        executed_at=T0 + timedelta(minutes=minutes),
    )


# ---------------------------------------------------------------------------
# The premise: FIFO really does hand one execution to two round trips
# ---------------------------------------------------------------------------


def test_an_oversell_puts_one_execution_in_two_round_trips():
    """The whole problem in three fills.

    The SELL closes the long AND opens the short. Nothing about it marks it as
    special, and `position_fills` stores it once per position it belongs to.
    """
    buy, sell, cover = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    result = match_executions([
        ex(buy, "BUY", 10, 100, 0),
        ex(sell, "SELL", 15, 110, 60),
        ex(cover, "BUY", 5, 105, 120),
    ])

    assert len(result.positions) == 2
    long_rt, short_rt = result.positions
    assert long_rt.direction == "LONG"
    assert short_rt.direction == "SHORT"

    # The same trade id, in both directions of the same flip.
    assert long_rt.close_trade_id == sell
    assert short_rt.open_trade_id == sell

    owners = {}
    for position in result.positions:
        for fill in position.fills:
            owners.setdefault(fill.trade_id, []).append(position.direction)
    assert owners[sell] == ["LONG", "SHORT"]

    # And the quantity is split, not duplicated: 10 shares closed the long, 5
    # opened the short, against one 15-share execution.
    attributed = [f.quantity for p in result.positions for f in p.fills
                  if f.trade_id == sell]
    assert sorted(attributed) == [Decimal("5"), Decimal("10")]


def test_a_flip_that_passes_exactly_through_flat_shares_nothing():
    """The boundary, and why this is rare rather than routine.

    Going flat on one execution and short on the next produces two round trips
    with no fill in common, so the delete has nothing to refuse. Only an
    execution that overshoots flat is shared.
    """
    buy, sell, short, cover = (uuid.uuid4() for _ in range(4))

    result = match_executions([
        ex(buy, "BUY", 10, 100, 0),
        ex(sell, "SELL", 10, 110, 60),     # exactly flat
        ex(short, "SELL", 5, 112, 120),
        ex(cover, "BUY", 5, 108, 180),
    ])

    ids = [{f.trade_id for f in p.fills} for p in result.positions]
    assert len(ids) == 2
    assert ids[0].isdisjoint(ids[1])


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------


@dataclass
class _Position:
    """Stands in for the ORM row. Only what the handler reads."""

    id: uuid.UUID
    symbol: str = "ACME"
    quantity: Decimal = Decimal("5")
    realized_pnl: Decimal = Decimal("25")
    entry_time: datetime = T0
    exit_time: datetime = T0 + timedelta(hours=2)
    review_status: str = "pending"
    review_went_well: str | None = None
    review_went_wrong: str | None = None
    review_lessons: str | None = None
    notes: str | None = None


@dataclass
class _Scalars:
    rows: list

    def all(self):
        return self.rows


@dataclass
class _Result:
    rows: list

    def scalars(self):
        return _Scalars(self.rows)


@dataclass
class _StubSession:
    """Answers the two lookups the guard makes, and nothing else.

    Statements are told apart by what they select, which is also a check that
    the handler asks the questions it is supposed to: the executions behind
    this position, then the OTHER positions built on them.
    """

    target: _Position
    trade_ids: list[uuid.UUID] = field(default_factory=list)
    shared: list[_Position] = field(default_factory=list)
    # Anything past the guard is a bug in the refusing case; recorded so the
    # test can assert nothing was deleted rather than assume it.
    statements: list[str] = field(default_factory=list)

    async def get(self, _model, key):
        return self.target if key == self.target.id else None

    async def execute(self, stmt):
        selected = stmt.column_descriptions[0]["name"]
        self.statements.append(selected)
        if selected == "trade_id":
            return _Result(self.trade_ids)
        if selected == "position_id":
            return _Result([p.id for p in self.shared])
        if selected == "Position":
            return _Result(self.shared)
        raise AssertionError(f"unexpected statement selecting {selected!r}")

    async def delete(self, _obj):
        raise AssertionError("the guard must refuse before deleting anything")

    async def commit(self):
        raise AssertionError("the guard must refuse before committing anything")


def _session_with_one_shared_neighbour(**position_kwargs):
    target = _Position(id=uuid.uuid4())
    neighbour = _Position(id=uuid.uuid4(), **position_kwargs)
    return _StubSession(
        target=target,
        trade_ids=[uuid.uuid4(), uuid.uuid4()],
        shared=[neighbour],
    ), target, neighbour


def test_deleting_a_round_trip_that_shares_a_fill_is_refused():
    """409, not a silent success. The default has to be the safe one: a client
    that does not know about shared fills must not be able to destroy a
    reviewed round trip by accident."""
    session, target, _ = _session_with_one_shared_neighbour()

    with pytest.raises(HTTPException) as caught:
        run(main.delete_position(position_id=target.id, session=session))

    assert caught.value.status_code == 409
    assert "1 other round trip" in caught.value.detail
    assert "ACME" in caught.value.detail


def test_the_refusal_says_how_to_answer_it():
    """A refusal that does not say what to do next is a dead end. It points at
    the preflight endpoint AND at deleting the individual fills, which is the
    route that keeps the neighbour."""
    session, target, _ = _session_with_one_shared_neighbour()

    with pytest.raises(HTTPException) as caught:
        run(main.delete_position(position_id=target.id, session=session))

    detail = caught.value.detail
    assert "delete-impact" in detail
    assert "include_shared=true" in detail
    assert "individual fills" in detail


def test_the_refusal_happens_before_anything_is_written():
    """Ordering is the point. The stub raises on delete() and commit(), so
    reaching either fails the test rather than passing quietly."""
    session, target, _ = _session_with_one_shared_neighbour()

    with pytest.raises(HTTPException):
        run(main.delete_position(position_id=target.id, session=session))

    # It looked up the fills and the neighbouring positions, and stopped.
    assert session.statements == ["trade_id", "position_id", "Position"]


def test_nothing_is_shared_means_nothing_to_refuse():
    """The ordinary delete must not acquire a new way to fail. With no shared
    fill the guard passes and the handler proceeds -- here into the stub's
    unstubbed territory, which is the proof it got past."""
    target = _Position(id=uuid.uuid4())
    session = _StubSession(target=target, trade_ids=[], shared=[])

    with pytest.raises(AssertionError, match="before deleting anything"):
        run(main.delete_position(position_id=target.id, session=session))


def test_a_missing_position_is_still_a_404():
    session = _StubSession(target=_Position(id=uuid.uuid4()))

    with pytest.raises(HTTPException) as caught:
        run(main.delete_position(position_id=uuid.uuid4(), session=session))

    assert caught.value.status_code == 404


# ---------------------------------------------------------------------------
# The preflight
# ---------------------------------------------------------------------------


def test_the_impact_endpoint_names_the_neighbour_it_would_destroy():
    """What the confirmation dialog is built from. A count is not enough: the
    user is deciding whether that OTHER trade matters, so it needs the symbol,
    the size, the date and the P&L."""
    session, target, neighbour = _session_with_one_shared_neighbour(
        symbol="ACME", quantity=Decimal("5"), realized_pnl=Decimal("-42.5")
    )

    impact = run(main.position_delete_impact(position_id=target.id, session=session))

    assert impact.executions_deleted == 2
    assert len(impact.shared_round_trips) == 1
    shared = impact.shared_round_trips[0]
    assert shared.position_id == neighbour.id
    assert shared.symbol == "ACME"
    assert shared.quantity == Decimal("5")
    assert shared.realized_pnl == Decimal("-42.5")
    assert shared.has_review is False
    assert impact.reviews_at_risk == 0


def test_the_impact_endpoint_flags_a_reviewed_neighbour():
    """The one loss that re-matching cannot undo, called out separately from
    the count of round trips."""
    session, target, _ = _session_with_one_shared_neighbour(
        review_status="reviewed", notes="held through the retest, sized right"
    )

    impact = run(main.position_delete_impact(position_id=target.id, session=session))

    assert impact.shared_round_trips[0].has_review is True
    assert impact.reviews_at_risk == 1


def test_the_preflight_reports_a_clean_delete_as_clean():
    """Most deletes touch nothing else, and the dialog must not imply
    otherwise."""
    target = _Position(id=uuid.uuid4())
    session = _StubSession(target=target, trade_ids=[uuid.uuid4()], shared=[])

    impact = run(main.position_delete_impact(position_id=target.id, session=session))

    assert impact.shared_round_trips == []
    assert impact.reviews_at_risk == 0
    assert impact.executions_deleted == 1


# ---------------------------------------------------------------------------
# One definition of "has a review", everywhere
# ---------------------------------------------------------------------------


def test_a_graded_round_trip_counts_as_reviewed():
    assert main._position_has_review(_Position(id=uuid.uuid4(), review_status="reviewed"))


@pytest.mark.parametrize(
    "field_name",
    ["review_went_well", "review_went_wrong", "review_lessons", "notes"],
)
def test_written_work_counts_even_while_the_review_is_unfinished(field_name):
    """Half a post-mortem is still something the user typed and cannot get
    back, so a status of 'pending' does not make it disposable."""
    position = _Position(id=uuid.uuid4(), **{field_name: "something written"})
    assert main._position_has_review(position)


def test_an_untouched_round_trip_does_not():
    assert not main._position_has_review(_Position(id=uuid.uuid4()))


# ---------------------------------------------------------------------------
# What the response has to be able to say
# ---------------------------------------------------------------------------


def test_the_delete_result_reports_collateral():
    """`positions_removed` counts round trips the user did NOT ask to delete --
    neighbours plus anything the rebuild found stale. Without it the only trace
    of a lost trade was a changed headline figure."""
    result = main.PositionDeleteResult(
        position_id=uuid.uuid4(),
        ticker="ACME",
        executions_deleted=2,
        positions_rebuilt=0,
        suppressed_from_future_syncs=2,
        positions_removed=1,
        reviews_discarded=1,
    )
    assert result.positions_removed == 1
    assert result.reviews_discarded == 1


def test_the_delete_result_defaults_the_collateral_counters_to_zero():
    """The ordinary delete reports clean zeros rather than nulls."""
    result = main.PositionDeleteResult(
        position_id=uuid.uuid4(),
        ticker="ACME",
        executions_deleted=1,
        positions_rebuilt=1,
        suppressed_from_future_syncs=0,
    )
    assert result.positions_removed == 0
    assert result.reviews_discarded == 0


# ---------------------------------------------------------------------------
# The schema backstop (migration 019)
# ---------------------------------------------------------------------------


def test_a_position_may_not_exist_without_its_executions():
    """uq_positions_open_close is only meaningful while both columns are set:
    Postgres treats NULLs as distinct, so a row keyed (NULL, ...) collides with
    nothing and the next rebuild inserts the same round trip a second time.

    Every handler already deletes affected positions before the executions.
    This is the constraint for the one added later that forgets -- with the
    foreign keys still ON DELETE SET NULL, such a delete now fails loudly
    instead of leaving a round trip with no fills underneath it.
    """
    columns = main.Position.__table__.c
    assert columns.open_trade_id.nullable is False
    assert columns.close_trade_id.nullable is False
