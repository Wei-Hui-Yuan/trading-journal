"""Reviewing and lifting tombstones, and telling the truth about a sync.

Two properties carry most of the weight here:

  * Lifting a tombstone restores NOTHING by itself. The fill was deleted; only
    the broker still has it, and it returns only when a sync next covers its
    date. `restored_immediately` is False on purpose so the UI cannot promise
    otherwise -- a "Restore" button that silently does nothing visible is worse
    than no button.
  * A throttled query and a rejected one look identical in a failure list but
    mean opposite things. Only the first is worth retrying, and
    `is_transient_failure` is what lets the toast say which happened.
"""

import os
from datetime import datetime

import pytest
from pydantic import ValidationError

os.environ.setdefault("CORS_ALLOW_ORIGINS", "https://trading-journal-test.vercel.app")

import main  # noqa: E402
from services import ibkr_client  # noqa: E402


# ---------------------------------------------------------------------------
# Classifying Flex failures
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", ["1001", "1018", "1019"])
def test_throttling_codes_are_transient(code):
    """All three clear on their own; retrying later is the right advice."""
    assert ibkr_client.is_transient_failure(
        f"query 12345: IBKR rejected the statement request (code {code}): busy"
    )


def test_hard_rejections_are_not_transient():
    """A bad token fails identically forever. Retrying only burns the budget."""
    assert not ibkr_client.is_transient_failure(
        "query 12345: IBKR rejected the statement request (code 1015): invalid token"
    )
    assert not ibkr_client.is_transient_failure("query 12345: connection reset")


def test_a_bare_number_is_not_a_code():
    """Matches on the `(code NNNN)` fragment, not a loose digit run.

    A price or quantity that happens to read 1001 must not be mistaken for a
    rate limit and turned into "try again in a few minutes".
    """
    assert not ibkr_client.is_transient_failure("query 7: filled 1001 shares")


# ---------------------------------------------------------------------------
# The sync summary
# ---------------------------------------------------------------------------


def _result(**overrides):
    base = dict(
        executions_parsed=0, staged_new=0, staged_duplicates=0,
        trades_created=0, trades_duplicates=0, positions_matched=0,
        symbols_touched=[], skipped_non_tradeable=0,
    )
    base.update(overrides)
    return main.IngestResult(**base)


def test_defaults_claim_nothing():
    """An older client, or a path that never set them, must not imply trouble."""
    result = _result()
    assert result.rate_limited is False
    assert result.suppressed_skipped == 0
    assert result.queries_failed == []


def test_carries_every_figure_the_toast_reports():
    result = _result(
        executions_parsed=45, staged_new=45, staged_duplicates=0,
        trades_created=3, trades_duplicates=42, positions_matched=2,
        symbols_touched=["CAT", "UNH"], suppressed_skipped=2,
        queries_failed=["query 9: (code 1001): busy"], rate_limited=True,
    )
    assert result.trades_created == 3
    assert result.trades_duplicates == 42
    assert result.suppressed_skipped == 2
    assert result.rate_limited is True
    assert len(result.queries_failed) == 1


def test_rate_limited_is_independent_of_failure_count():
    """A query can fail without being throttled; the flag must not be inferred."""
    result = _result(queries_failed=["query 9: connection reset"], rate_limited=False)
    assert result.queries_failed and result.rate_limited is False


# ---------------------------------------------------------------------------
# Tombstone rows
# ---------------------------------------------------------------------------


def test_pre_017_tombstones_survive_validation():
    """Rows written before the detail columns existed have nothing to backfill.

    Nulls must serialise rather than raise, or the whole management list 500s
    because of one old row.
    """
    out = main.SuppressedExecutionOut(
        ibkr_exec_id="IBKR-111634108", ticker="CAT", reason="duplicate",
        direction=None, quantity=None, price=None,
        executed_at=None, created_at=datetime.now(main.MARKET_TZ),
    )
    assert out.quantity is None
    assert out.executed_at is None


def test_full_tombstone_round_trips():
    out = main.SuppressedExecutionOut(
        ibkr_exec_id="IBKR-111634108", ticker="CAT", reason="phantom from a bad sync",
        direction="BUY", quantity=3.0, price=890.0,
        executed_at=datetime.now(main.MARKET_TZ),
        created_at=datetime.now(main.MARKET_TZ),
    )
    assert out.direction == "BUY"
    assert out.quantity == 3.0


def test_exec_id_is_required():
    """It is the primary key and the only thing ingest matches on."""
    with pytest.raises(ValidationError):
        main.SuppressedExecutionOut(
            ticker="CAT", reason=None, direction=None, quantity=None,
            price=None, executed_at=None, created_at=None,
        )


def test_unsuppress_does_not_claim_to_restore():
    """The fill is gone; only a later sync can bring it back."""
    assert main.UnsuppressResult(
        ibkr_exec_id="IBKR-1", ticker="CAT"
    ).restored_immediately is False


def test_suppression_detail_columns_exist_and_are_nullable():
    """Guards migration 017 against a model/schema drift."""
    cols = main.SuppressedExecution.__table__.c
    for name in ("direction", "quantity", "price", "executed_at"):
        assert cols[name].nullable is True, name
    assert cols["ibkr_exec_id"].primary_key is True
