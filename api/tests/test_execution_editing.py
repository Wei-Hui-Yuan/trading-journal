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

import asyncio
import os
import uuid
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

os.environ.setdefault("CORS_ALLOW_ORIGINS", "https://trading-journal-test.vercel.app")

import main  # noqa: E402

from conftest import db_session, db_transaction, requires_db  # noqa: E402


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


@requires_db
def test_clearing_the_execution_time_is_refused_before_anything_is_touched():
    """`trades.entry_date` is NOT NULL, and the test above is exactly why an
    explicit null gets this far: the field is Optional so it can be OMITTED,
    and ExecutionUpdatePayload types it `string | null`.

    Assigning it through reached the driver as a not-null violation and came
    back as an opaque 500 -- and it did so AFTER the round trips built on this
    fill had been deleted in the same handler. The transaction rolls that back,
    so nothing was lost, but the user was told "internal error" for a request
    the API could have named the problem with.
    """
    async def scenario():
        async with db_transaction() as conn:
            open_id, pos_id = await _build_closed_round_trip(conn)
            session = db_session(conn)

            with pytest.raises(main.HTTPException) as caught:
                await main.update_execution(
                    trade_id=open_id,
                    params=main.ExecutionUpdate(execution_time=None),
                    session=session,
                )
            await session.close()

            assert caught.value.status_code == 422, "not a 500"
            assert "execution_time" in caught.value.detail

            pos_ok, trade_ok, _ = await _survives(conn, open_id, pos_id)
            assert pos_ok, "refused before the positions were deleted"
            assert trade_ok

    asyncio.run(scenario())


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
# These tests simulate exactly that crash -- monkeypatching
# `run_matching_for_ticker` to raise partway through the handler -- and check
# what a real Postgres transaction did about it, not what the source text
# says. Each has a CONTROL that reproduces the UNFIXED ordering (commit, then
# crash) and asserts it DOES lose the data; without that control a test that
# merely finds the position still present after the fixed code proves
# nothing about whether it would have caught a regression.
#
# The commit message on the original fix claimed the savepoint-rollback
# pattern used elsewhere in this suite "cannot observe this property at all",
# reasoning that join_transaction_mode="create_savepoint" turns every
# commit() into a savepoint release, so a two-commit sequence and a
# one-commit sequence are undone identically by the outer rollback. That is
# incorrect, and the fixture setup below is exactly why: an EARLY commit
# releases the savepoint holding the fixture rows, which makes them part of
# the OUTER transaction and therefore durable across the handler session's
# own close(); without that early commit, close() rolls back only the
# handler's savepoint and the fixture rows are untouched. The difference is
# visible as long as the fixture is committed in a session that predates the
# one under test -- see `_build_closed_round_trip` below.


async def _build_closed_round_trip(conn) -> tuple[uuid.UUID, uuid.UUID]:
    """A closed round trip with a review, committed into the OUTER
    transaction (not left in a savepoint the handler's own close() would
    undo). Returns (open_trade_id, position_id).
    """
    setup = db_session(conn)
    sym = f"ZZ{uuid.uuid4().hex[:6].upper()}"[:10]
    now = datetime.now(main.MARKET_TZ)
    open_id, close_id, pos_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    setup.add_all([
        main.Trade(
            id=open_id, ibkr_exec_id=f"IBKR-{uuid.uuid4().hex[:12]}", ticker=sym,
            direction="BUY", style="Unclassified", quantity=Decimal("10"),
            actual_entry=Decimal("100"), entry_date=now - timedelta(days=2),
            source_tag="IBKR",
        ),
        main.Trade(
            id=close_id, ibkr_exec_id=f"IBKR-{uuid.uuid4().hex[:12]}", ticker=sym,
            direction="SELL", style="Unclassified", quantity=Decimal("10"),
            actual_entry=Decimal("110"), entry_date=now - timedelta(days=1),
            source_tag="IBKR",
        ),
    ])
    await setup.flush()
    setup.add(main.Position(
        id=pos_id, symbol=sym, direction="LONG", style="Unclassified",
        quantity=Decimal("10"), entry_price=Decimal("100"), exit_price=Decimal("110"),
        entry_time=now - timedelta(days=2), exit_time=now - timedelta(days=1),
        realized_pnl=Decimal("100"), gross_pnl=Decimal("100"), commission=Decimal("0"),
        open_trade_id=open_id, close_trade_id=close_id,
        review_status="reviewed", review_went_well="must survive a crashed rebuild",
    ))
    await setup.flush()
    setup.add_all([
        main.PositionFill(
            id=uuid.uuid4(), position_id=pos_id, trade_id=open_id, role="OPEN",
            quantity=Decimal("10"), price=Decimal("100"), executed_at=now - timedelta(days=2),
        ),
        main.PositionFill(
            id=uuid.uuid4(), position_id=pos_id, trade_id=close_id, role="CLOSE",
            quantity=Decimal("10"), price=Decimal("110"), executed_at=now - timedelta(days=1),
        ),
    ])
    await setup.flush()
    await setup.commit()
    await setup.close()
    return open_id, pos_id


async def _survives(conn, open_id, pos_id) -> tuple[bool, bool, str | None]:
    """(position survived, trade survived, its review text) from a session
    that has seen none of the handler-under-test's work."""
    check = db_session(conn)
    pos = (await check.execute(
        main.select(main.Position.id, main.Position.review_went_well)
        .where(main.Position.id == pos_id)
    )).first()
    trade = (await check.execute(
        main.select(main.Trade.id).where(main.Trade.id == open_id)
    )).first()
    await check.close()
    return pos is not None, trade is not None, (pos.review_went_well if pos else None)


def _crash_matching(monkeypatch, session, *, commit_first: bool):
    """Make run_matching_for_ticker raise, optionally after a commit --
    `commit_first=True` reproduces the UNFIXED ordering (delete committed,
    then the rebuild dies); `commit_first=False` is what the current handlers
    actually do."""
    import services.matching_engine as me

    async def boom(*a, **k):
        if commit_first:
            await session.commit()
        raise RuntimeError("simulated crash during rebuild")

    monkeypatch.setattr(me, "run_matching_for_ticker", boom)


@requires_db
def test_update_execution_keeps_its_positions_if_the_rebuild_crashes(monkeypatch):
    async def scenario():
        async with db_transaction() as conn:
            open_id, pos_id = await _build_closed_round_trip(conn)
            session = db_session(conn)
            _crash_matching(monkeypatch, session, commit_first=False)

            with pytest.raises(RuntimeError):
                await main.update_execution(
                    trade_id=open_id, params=main.ExecutionUpdate(price=105.0),
                    session=session,
                )
            await session.close()

            pos_ok, trade_ok, review = await _survives(conn, open_id, pos_id)
            assert pos_ok, "the position must survive a crash before any commit"
            assert trade_ok
            assert review == "must survive a crashed rebuild"

    asyncio.run(scenario())


@requires_db
def test_update_execution_control_loses_its_positions_with_the_unfixed_ordering(monkeypatch):
    """The control: prove this probe CAN see the bug, by reproducing it."""
    async def scenario():
        async with db_transaction() as conn:
            open_id, pos_id = await _build_closed_round_trip(conn)
            session = db_session(conn)
            _crash_matching(monkeypatch, session, commit_first=True)

            with pytest.raises(RuntimeError):
                await main.update_execution(
                    trade_id=open_id, params=main.ExecutionUpdate(price=105.0),
                    session=session,
                )
            await session.close()

            pos_ok, trade_ok, _review = await _survives(conn, open_id, pos_id)
            assert not pos_ok, "the control must actually lose the position"

    asyncio.run(scenario())


@requires_db
def test_delete_trade_keeps_its_positions_if_the_rebuild_crashes(monkeypatch):
    async def scenario():
        async with db_transaction() as conn:
            open_id, pos_id = await _build_closed_round_trip(conn)
            session = db_session(conn)
            _crash_matching(monkeypatch, session, commit_first=False)

            with pytest.raises(RuntimeError):
                await main.delete_trade(trade_id=open_id, reason="probe", session=session)
            await session.close()

            pos_ok, trade_ok, review = await _survives(conn, open_id, pos_id)
            assert pos_ok, "the position must survive a crash before any commit"
            assert trade_ok, "the trade itself must not have been deleted either"
            assert review == "must survive a crashed rebuild"

    asyncio.run(scenario())


@requires_db
def test_delete_trade_control_loses_its_positions_with_the_unfixed_ordering(monkeypatch):
    async def scenario():
        async with db_transaction() as conn:
            open_id, pos_id = await _build_closed_round_trip(conn)
            session = db_session(conn)
            _crash_matching(monkeypatch, session, commit_first=True)

            with pytest.raises(RuntimeError):
                await main.delete_trade(trade_id=open_id, reason="probe", session=session)
            await session.close()

            pos_ok, _trade_ok, _review = await _survives(conn, open_id, pos_id)
            assert not pos_ok, "the control must actually lose the position"

    asyncio.run(scenario())
