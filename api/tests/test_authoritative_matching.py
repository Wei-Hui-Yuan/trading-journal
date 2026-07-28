"""A backdated fill must not leave a round trip behind that FIFO abandoned.

`insert_positions` only ever appends, relying on ON CONFLICT against
uq_positions_open_close to make a re-run a no-op. That guard holds only while
re-running produces the same (open_trade_id, close_trade_id) pairs -- and a
fill arriving with an EARLIER timestamp than fills already stored
re-partitions the whole FIFO queue, so it does not.

Two live paths reach this. Repairing a fill IBKR dropped is backdated by
definition, and the two Flex queries disagree on purpose: a TCF query reports
today's fills while an Activity query lags a day or so, so an older fill
routinely lands after a newer one has already matched.

The tests below pin the premise -- that backdating really does change the
pairs -- because everything else follows from it. If `match_executions` were
ever changed to emit stable pairs regardless of arrival order, the deletion
pass would become dead weight and these would be the tests that said so.
"""

import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

os.environ.setdefault("CORS_ALLOW_ORIGINS", "https://trading-journal-test.vercel.app")

import main  # noqa: E402
from services.matching_engine import (  # noqa: E402
    Execution,
    MatchingResult,
    match_executions,
    run_matching_for_ticker,
)

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


def pairs(result):
    return {(p.open_trade_id, p.close_trade_id) for p in result.positions}


# ---------------------------------------------------------------------------
# The premise: arrival order changes which pairs FIFO produces
# ---------------------------------------------------------------------------


def test_a_backdated_buy_dissolves_an_existing_round_trip():
    """The plain case, and the reason ON CONFLICT is not enough.

    BUY 10@100 / SELL 10@110 closes flat and stores one round trip. A BUY
    10@90 dated earlier consumes the sell instead, leaving 10 shares open --
    so nothing closes, the fresh result is empty, and the stored row conflicts
    with nothing. Appending alone would keep it forever.
    """
    b1, s1, b0 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    before = match_executions([ex(b1, "BUY", 10, 100, 0), ex(s1, "SELL", 10, 110, 60)])
    assert pairs(before) == {(b1, s1)}
    assert before.total_realized_pnl == Decimal("100")

    after = match_executions([
        ex(b0, "BUY", 10, 90, -30),
        ex(b1, "BUY", 10, 100, 0),
        ex(s1, "SELL", 10, 110, 60),
    ])
    assert after.positions == [], "FIFO no longer closes anything"
    # The stored pair is not reproduced, so nothing would conflict with it.
    assert pairs(before) - pairs(after) == {(b1, s1)}


def test_the_dissolved_pnl_is_deferred_not_destroyed():
    """The +100 does not vanish; it becomes P&L on a position that is still
    open, which is a different thing from a closed round trip and is why the
    row must not stay in `positions`."""
    b1, s1, b0 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    after = match_executions([
        ex(b0, "BUY", 10, 90, -30),
        ex(b1, "BUY", 10, 100, 0),
        ex(s1, "SELL", 10, 110, 60),
    ])
    assert after.open_round_trip_realized_pnl == Decimal("200")
    assert after.open_quantity == Decimal("10")


def test_a_backdated_fill_can_re_pair_into_a_different_round_trip():
    """The worse variant: the fresh pair is NEW, so it inserts alongside the
    stale one and the P&L is double-counted rather than merely stranded."""
    b1, s1, b0, s2 = (uuid.uuid4() for _ in range(4))

    before = match_executions([ex(b1, "BUY", 10, 100, 0), ex(s1, "SELL", 10, 110, 60)])
    after = match_executions([
        ex(b0, "BUY", 10, 90, -30),
        ex(b1, "BUY", 10, 100, 0),
        ex(s1, "SELL", 10, 110, 60),
        ex(s2, "SELL", 10, 120, 90),
    ])

    assert pairs(before).isdisjoint(pairs(after)), "no overlap, so nothing conflicts"
    assert after.total_realized_pnl == Decimal("400")
    # Appending would leave +100 (stale) + +400 (fresh) = +500 against a truth
    # of +400, and two round trips where there is one.
    assert before.total_realized_pnl + after.total_realized_pnl == Decimal("500")


def test_fills_arriving_in_order_keep_their_pairs():
    """The common case must stay a no-op, or every sync would churn reviews.

    A later fill appended after existing ones does not disturb what came
    before, so the stored pair is reproduced and the row is left alone.
    """
    b1, s1, b2 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    before = match_executions([ex(b1, "BUY", 10, 100, 0), ex(s1, "SELL", 10, 110, 60)])
    after = match_executions([
        ex(b1, "BUY", 10, 100, 0),
        ex(s1, "SELL", 10, 110, 60),
        ex(b2, "BUY", 5, 120, 120),  # a new, later position opening
    ])
    assert pairs(before) <= pairs(after), "the earlier round trip survives untouched"


# ---------------------------------------------------------------------------
# What the engine reports back
# ---------------------------------------------------------------------------


def test_a_result_reports_nothing_removed_by_default():
    """So a caller that never triggers a re-partition reads a clean zero
    rather than None."""
    result = MatchingResult(ticker="ACME")
    assert result.positions_removed == 0
    assert result.reviews_discarded == 0


def test_reviews_discarded_is_tracked_separately_from_positions_removed():
    """They are different losses. A dissolved round trip that was never
    reviewed costs only a derived row, which re-matching rebuilds; one that
    was reviewed takes a grade, notes and discipline answers with it, and
    nothing can reconstruct those."""
    fields = MatchingResult.__dataclass_fields__
    assert "positions_removed" in fields
    assert "reviews_discarded" in fields


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------


class _ExplodingSession:
    """Any database access here is a bug: the guard must fire first."""

    async def execute(self, *_args, **_kwargs):
        raise AssertionError("the guard must reject before touching the database")


def test_persisting_a_subset_match_is_refused():
    """Matching only unclassified fills cannot decide which of a ticker's
    positions are stale -- every position built from a classified fill would
    look stale and be deleted. No caller does this today; the guard is what
    stops one being added by accident.
    """
    import asyncio

    with pytest.raises(ValueError, match="cannot be combined"):
        asyncio.run(
            run_matching_for_ticker(
                _ExplodingSession(), "ACME", persist=True, only_unclassified=True
            )
        )


# ---------------------------------------------------------------------------
# The endpoints have somewhere to report it
# ---------------------------------------------------------------------------


def test_ingest_reports_removed_round_trips():
    """A sync that silently changed net P&L and trade count would be
    indistinguishable from a bug."""
    result = main.IngestResult(
        executions_parsed=1, staged_new=1, staged_duplicates=0,
        trades_created=1, trades_duplicates=0, positions_matched=0,
        symbols_touched=["ACME"], skipped_non_tradeable=0,
        positions_removed=2, reviews_discarded=1,
    )
    assert result.positions_removed == 2
    assert result.reviews_discarded == 1


def test_ingest_defaults_the_new_counters_to_zero():
    """Older callers constructing a result without them must not break."""
    result = main.IngestResult(
        executions_parsed=0, staged_new=0, staged_duplicates=0,
        trades_created=0, trades_duplicates=0, positions_matched=0,
        symbols_touched=[], skipped_non_tradeable=0,
    )
    assert result.positions_removed == 0
    assert result.reviews_discarded == 0


def test_repair_fill_reports_removed_round_trips():
    """The path most likely to trigger this, since a repair fill is backdated
    by definition."""
    assert "positions_removed" in main.ManualTradeResult.model_fields
    assert "reviews_discarded" in main.ManualTradeResult.model_fields
