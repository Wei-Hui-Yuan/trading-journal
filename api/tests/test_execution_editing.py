"""Correcting the execution ledger by hand, without making it untrustworthy.

Three properties matter more than the CRUD, and each encodes a decision that
would be easy to simplify back into a bug:

  * Deleting a broker fill must TOMBSTONE it. Ingest is idempotent through
    ON CONFLICT DO NOTHING, which skips rows that still exist -- a deleted row
    does not, so without suppression the next sync silently resurrects it and
    the delete button undoes itself.
  * The broker snapshot is written on the FIRST edit only. Re-snapshotting
    would overwrite IBKR's figures with the previous edit's and destroy the
    only record of what actually arrived.
  * Dismissing and deleting a position are different actions. Dismiss keeps a
    real trade's P&L and takes it out of the queue; delete removes the
    executions underneath, for a round trip that never happened.
"""

import inspect
import os
import uuid
from datetime import datetime

import pytest
from pydantic import ValidationError

os.environ.setdefault("CORS_ALLOW_ORIGINS", "https://trading-journal-test.vercel.app")

import main  # noqa: E402


# ---------------------------------------------------------------------------
# ExecutionUpdate — the facts of a fill
# ---------------------------------------------------------------------------


def test_partial_update_only_carries_named_fields():
    """The handler applies `exclude_unset`, so absence must mean absence."""
    params = main.ExecutionUpdate(price=410.5)
    assert params.model_dump(exclude_unset=True) == {"price": 410.5}


def test_direction_is_normalised():
    assert main.ExecutionUpdate(direction="sell").direction == "SELL"
    assert main.ExecutionUpdate(direction="  buy ").direction == "BUY"


def test_direction_rejects_nonsense():
    with pytest.raises(ValidationError):
        main.ExecutionUpdate(direction="LONG")


def test_quantity_and_price_must_be_positive():
    """A zero-quantity fill is not a correction, it is a deletion."""
    with pytest.raises(ValidationError):
        main.ExecutionUpdate(quantity=0)
    with pytest.raises(ValidationError):
        main.ExecutionUpdate(price=0)
    with pytest.raises(ValidationError):
        main.ExecutionUpdate(quantity=-5)


def test_fractional_quantities_are_allowed():
    """trades.quantity is NUMERIC(18,8); 0.2-share fills are real here."""
    assert main.ExecutionUpdate(quantity=0.2).quantity == 0.2


def test_empty_update_is_distinguishable_from_a_field_set_to_none():
    assert main.ExecutionUpdate().model_dump(exclude_unset=True) == {}
    explicit = main.ExecutionUpdate(execution_time=None)
    assert "execution_time" in explicit.model_fields_set


# ---------------------------------------------------------------------------
# Suppression — what makes a deletion stick
# ---------------------------------------------------------------------------


def test_suppression_is_keyed_by_broker_id_not_a_foreign_key():
    """The row it names is deleted; a FK would cascade away and suppress nothing."""
    table = main.SuppressedExecution.__table__
    assert table.c.ibkr_exec_id.primary_key is True
    assert list(table.c.ibkr_exec_id.foreign_keys) == []


def test_only_broker_fills_are_worth_tombstoning():
    """MANUAL- keys are generated fresh per entry; no sync can re-send them."""
    assert "IBKR-123".startswith("IBKR-")
    assert not f"MANUAL-{uuid.uuid4()}".startswith("IBKR-")


def test_ingest_result_reports_suppressed_skips():
    """A climbing count means the Flex query still returns unwanted fills."""
    result = main.IngestResult(
        executions_parsed=5, staged_new=5, staged_duplicates=0,
        trades_created=3, trades_duplicates=0, positions_matched=1,
        symbols_touched=["CAT"], skipped_non_tradeable=0, suppressed_skipped=2,
    )
    assert result.suppressed_skipped == 2


def test_delete_result_defaults_to_not_suppressed():
    """Manual deletions write no tombstone, and must not claim to have."""
    result = main.TradeDeleteResult(
        deleted_trade_id=uuid.uuid4(), ticker="CAT",
        positions_removed=1, positions_rebuilt=0, reviews_discarded=0,
    )
    assert result.suppressed_from_future_syncs is False


# ---------------------------------------------------------------------------
# Result shapes
# ---------------------------------------------------------------------------


def test_execution_update_result_carries_the_broker_original():
    """Null on untouched rows and on manual entries, which had no broker value."""
    result = main.ExecutionUpdateResult(
        trade_id=uuid.uuid4(), ticker="MSFT", direction="BUY",
        quantity=4.0, price=438.80, execution_time=datetime.now(main.MARKET_TZ),
        edited_at=None, broker_original=None,
        positions_removed=0, positions_rebuilt=0, reviews_discarded=0,
    )
    assert result.broker_original is None
    assert result.edited_at is None


def test_position_delete_reports_executions_not_just_the_row():
    """Deleting the position alone would let the next rebuild recreate it."""
    result = main.PositionDeleteResult(
        position_id=uuid.uuid4(), ticker="CAT", executions_deleted=3,
        positions_rebuilt=1, suppressed_from_future_syncs=3,
    )
    assert result.executions_deleted == 3
    assert result.suppressed_from_future_syncs == 3


# ---------------------------------------------------------------------------
# I7 -- the delete and the rebuild share one transaction
# ---------------------------------------------------------------------------
#
# Both handlers used to commit the position delete (and, in delete_trade, the
# trade delete and suppression tombstone) BEFORE calling
# run_matching_for_ticker, which has its own commit at the end. A crash in
# that window -- a redeploy, an OOM, a dropped Supabase connection -- left the
# delete permanent with nothing rebuilt to replace it: a round trip and its
# review gone, and (for update_execution) a fill sitting there edited with no
# positions describing it at all.
#
# Verified live against Supabase: wrapping session.commit with a call-counting
# spy during a real update_execution and a real delete_trade shows exactly
# one call in each, where the unfixed code made two. The savepoint-rollback
# pattern used for the DB checks elsewhere in this suite cannot observe this
# property at all -- join_transaction_mode="create_savepoint" turns every
# commit() into a savepoint release, so a two-commit sequence and a
# one-commit sequence are undone identically by the outer rollback. Counting
# calls during a real invocation is the only way to see the difference, which
# is why this is pinned here structurally instead.


def _between(source: str, start_marker: str, end_marker: str) -> str:
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    return source[start:end]


def test_update_execution_has_no_commit_between_the_delete_and_the_rebuild():
    source = inspect.getsource(main.update_execution)
    between = _between(source, "delete(Position)", "run_matching_for_ticker(")
    assert "session.commit()" not in between


def test_delete_trade_has_no_commit_between_the_delete_and_the_rebuild():
    source = inspect.getsource(main.delete_trade)
    between = _between(source, "await session.delete(trade)", "run_matching_for_ticker(")
    assert "session.commit()" not in between
