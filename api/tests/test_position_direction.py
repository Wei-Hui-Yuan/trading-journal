"""A round trip's direction is computed once and stored, not guessed back.

`MatchedPosition.direction` is set from the lot that opened the round trip -- a
queue of BUY lots closed by a SELL is LONG, the reverse is SHORT -- and then
`insert_positions` discarded it, because there was no column for it. Both
readers rebuilt it from the opening execution and guessed when that lookup came
back empty:

    direction = (opening.direction if opening else "BUY") or "BUY"

The guess is always long. R degrades safely under it -- scoring a short as a
long produces negative risk, and the helper returns None rather than a number.
Entry slippage does not: it flips sign, so a short filled a dollar BETTER than
planned is reported as a dollar worse.
"""

import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

os.environ.setdefault("CORS_ALLOW_ORIGINS", "https://trading-journal-test.vercel.app")

import main  # noqa: E402
from services.matching_engine import Execution, match_executions  # noqa: E402

T0 = datetime(2026, 6, 1, 14, 0, tzinfo=timezone.utc)


def ex(trade_id, direction, qty, price, minutes):
    return Execution(
        trade_id=trade_id, ticker="ACME", direction=direction,
        quantity=Decimal(str(qty)), price=Decimal(str(price)),
        executed_at=T0 + timedelta(minutes=minutes),
    )


@dataclass
class _Position:
    direction: str


# ---------------------------------------------------------------------------
# The engine knows, and now hands it over
# ---------------------------------------------------------------------------


def test_the_matcher_labels_a_short_correctly():
    a, b = uuid.uuid4(), uuid.uuid4()
    position = match_executions([
        ex(a, "SELL", 10, 100, 0),
        ex(b, "BUY", 10, 90, 60),
    ]).positions[0]
    assert position.direction == "SHORT"
    assert position.realized_pnl == Decimal("100"), "a short profits as price falls"


def test_the_matcher_labels_a_long_correctly():
    a, b = uuid.uuid4(), uuid.uuid4()
    position = match_executions([
        ex(a, "BUY", 10, 100, 0),
        ex(b, "SELL", 10, 110, 60),
    ]).positions[0]
    assert position.direction == "LONG"


def test_the_column_exists_and_refuses_a_null():
    """Without NOT NULL the old fallback quietly comes back for any row that
    misses the write."""
    column = main.Position.__table__.c.direction
    assert column.nullable is False
    assert column.type.length == 5, "must fit SHORT"


def test_direction_is_written_and_refreshed_like_every_other_derived_column():
    """It has to be in the upsert's refresh list too. A round trip that flips
    from long to short after a backdated fill re-partitions the queue keeps its
    (open, close) pair, so the row is UPDATED rather than inserted."""
    import inspect

    from services import matching_engine

    source = inspect.getsource(matching_engine.insert_positions)
    assert '"direction": position.direction' in source
    assert '"symbol", "direction"' in source, "must be in the refreshed tuple"


# ---------------------------------------------------------------------------
# The mapping to the API's vocabulary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stored,expected", [
    ("LONG", "BUY"),
    ("SHORT", "SELL"),
    ("long", "BUY"),
    ("short", "SELL"),
])
def test_the_stored_direction_maps_to_the_api_vocabulary(stored, expected):
    """A position is LONG or SHORT; the execution that opened it was a BUY or a
    SELL. The journal and the frontend speak the latter."""
    assert main._position_side(_Position(direction=stored)) == expected


# ---------------------------------------------------------------------------
# What the guess cost
# ---------------------------------------------------------------------------


def test_a_good_short_fill_reads_as_good():
    """The measurable damage.

    A short is entered by SELLING, so filling ABOVE the planned entry is the
    good outcome -- you sold higher for the same trade. Planned 100, filled
    101: a dollar better. Scored as a long, the same numbers mean you paid a
    dollar up, and the trade is graded a discipline failure instead of a win.
    """
    as_short = main._entry_slippage("SELL", 100.0, 101.0)
    as_long_by_mistake = main._entry_slippage("BUY", 100.0, 101.0)

    assert as_short == pytest.approx(1.0), "sold higher than planned: better"
    assert as_long_by_mistake == pytest.approx(-1.0), "the sign the guess gave"
    assert as_short == -as_long_by_mistake


def test_a_bad_short_fill_reads_as_bad():
    """The other direction, so the test cannot pass on a sign convention that
    is merely consistent. Planned 101, filled 100 is a WORSE short fill --
    you received less for the same shares."""
    assert main._entry_slippage("SELL", 101.0, 100.0) == pytest.approx(-1.0)
    assert main._entry_slippage("BUY", 101.0, 100.0) == pytest.approx(1.0)


def test_r_degrades_safely_where_slippage_does_not():
    """Worth pinning the asymmetry: only one of the two figures announced the
    problem. A winning short scored as a long implies negative risk, and the
    helper declines to return a number -- so R going missing was the visible
    symptom while slippage quietly reported the opposite of the truth."""
    correct = main._score_r("SELL", 100.0, 90.0, 105.0)
    guessed = main._score_r("BUY", 100.0, 90.0, 105.0)

    assert correct == pytest.approx(2.0)
    assert guessed is None


def test_a_short_scored_as_a_long_is_not_merely_the_negative_of_itself():
    """So the guess cannot be corrected after the fact by flipping a sign --
    the direction has to be known when the figure is computed."""
    assert main._score_r("SELL", 100.0, 90.0, 105.0) == pytest.approx(2.0)
    assert main._score_r("BUY", 100.0, 90.0, 105.0) is None
