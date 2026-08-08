"""The sizing scratchpad: a fast note, deliberately separate from a real plan.

The property that matters is the one-way bridge between the two. A note in
`sizing_scratchpad` is read by nothing outside this file's endpoints -- not
the matching engine, not analytics -- until `promote_sizing_entry` turns it
into a real `planned_trades` row and deletes the note in the SAME commit.
That atomicity, and the three-day self-clearing on the read path, are the two
things worth more than one test each.
"""

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

os.environ.setdefault("CORS_ALLOW_ORIGINS", "https://trading-journal-test.vercel.app")

import main  # noqa: E402

from conftest import db_session, db_transaction, requires_db  # noqa: E402


# ---------------------------------------------------------------------------
# Validators — no database needed
# ---------------------------------------------------------------------------


def test_ticker_is_upper_cased_and_trimmed():
    entry = main.SizingEntryCreate(ticker=" aapl ", direction="BUY")
    assert entry.ticker == "AAPL"


def test_direction_is_upper_cased():
    entry = main.SizingEntryCreate(ticker="AAPL", direction="buy")
    assert entry.direction == "BUY"


def test_an_invalid_direction_is_rejected():
    with pytest.raises(ValidationError, match="BUY.*SELL"):
        main.SizingEntryCreate(ticker="AAPL", direction="LONG")


def test_ticker_and_direction_are_the_only_required_fields():
    """Matching PlanCreate's own philosophy: a note is worth keeping the
    moment it has a ticker, not only once every price is filled in."""
    entry = main.SizingEntryCreate(ticker="AAPL", direction="BUY")
    assert entry.entry is None
    assert entry.stop_loss is None
    assert entry.take_profit is None
    assert entry.quantity is None


def test_a_negative_price_is_rejected():
    with pytest.raises(ValidationError):
        main.SizingEntryCreate(ticker="AAPL", direction="BUY", entry=-5)


def test_update_leaves_unset_fields_absent_not_none():
    """The handler applies exclude_unset, so absence must mean absence -- an
    explicit null and an omitted field are different requests."""
    update = main.SizingEntryUpdate(entry=150.0)
    dumped = update.model_dump(exclude_unset=True)
    assert dumped == {"entry": 150.0}


def test_update_direction_validator_passes_none_through():
    """None must survive un-rejected: it is what an omitted field looks like
    once Pydantic has parsed the request, not an invalid direction."""
    update = main.SizingEntryUpdate()
    assert update.direction is None


def test_update_direction_validator_still_rejects_garbage():
    with pytest.raises(ValidationError):
        main.SizingEntryUpdate(direction="SIDEWAYS")


# ---------------------------------------------------------------------------
# _sizing_entry_out — Decimal at the boundary
# ---------------------------------------------------------------------------


def test_output_converts_decimal_to_float():
    row = main.SizingScratchpadEntry(
        id=uuid.uuid4(), ticker="AAPL", direction="BUY",
        entry=Decimal("150.25"), stop_loss=Decimal("147.00"),
        take_profit=None, quantity=Decimal("10"),
        created_at=datetime.now(timezone.utc),
    )
    out = main._sizing_entry_out(row)
    assert out.entry == 150.25
    assert out.take_profit is None
    assert isinstance(out.quantity, float)


# ---------------------------------------------------------------------------
# CRUD, against a real database (rolled back after every test)
# ---------------------------------------------------------------------------


def _create(ticker="AAPL", direction="BUY", **kw):
    return main.SizingEntryCreate(ticker=ticker, direction=direction, **kw)


@requires_db
def test_create_then_list_round_trips_the_values():
    async def scenario():
        async with db_transaction() as conn:
            session = db_session(conn)
            created = await main.create_sizing_entry(
                _create(entry=150.0, stop_loss=147.0, take_profit=160.0, quantity=10),
                session,
            )
            assert created.ticker == "AAPL"
            assert created.entry == 150.0

            listed = await main.list_sizing_entries(session)
            assert any(e.id == created.id for e in listed)

    asyncio.run(scenario())


@requires_db
def test_list_orders_newest_first():
    """created_at set explicitly rather than left to the server default: two
    creates inside the SAME test transaction would otherwise share one
    Postgres now() (transaction-scoped, not wall-clock), tying their
    timestamps and making the real thing under test -- which one sorts
    first -- undecided by the fixture rather than by the endpoint."""
    async def scenario():
        async with db_transaction() as conn:
            session = db_session(conn)
            now = datetime.now(timezone.utc)
            older = main.SizingScratchpadEntry(
                id=uuid.uuid4(), ticker="AAA", direction="BUY",
                created_at=now - timedelta(minutes=5),
            )
            newer = main.SizingScratchpadEntry(
                id=uuid.uuid4(), ticker="BBB", direction="BUY", created_at=now,
            )
            session.add_all([older, newer])
            await session.commit()

            listed = await main.list_sizing_entries(session)
            ids = [e.id for e in listed]
            assert ids.index(newer.id) < ids.index(older.id)

    asyncio.run(scenario())


@requires_db
def test_list_is_deterministic_when_two_notes_share_a_timestamp():
    """The case the id tiebreak exists for: same created_at, no defined
    "true" order between them -- but calling list twice must return the two
    in the same relative order both times, or pagination built on this
    endpoint later would see one row twice and miss the other."""
    async def scenario():
        async with db_transaction() as conn:
            session = db_session(conn)
            tied_at = datetime.now(timezone.utc)
            one = main.SizingScratchpadEntry(
                id=uuid.uuid4(), ticker="ONE", direction="BUY", created_at=tied_at,
            )
            two = main.SizingScratchpadEntry(
                id=uuid.uuid4(), ticker="TWO", direction="BUY", created_at=tied_at,
            )
            session.add_all([one, two])
            await session.commit()

            first_call = [e.id for e in await main.list_sizing_entries(session)]
            second_call = [e.id for e in await main.list_sizing_entries(session)]
            assert first_call == second_call

    asyncio.run(scenario())


@requires_db
def test_update_changes_only_the_named_field():
    async def scenario():
        async with db_transaction() as conn:
            session = db_session(conn)
            created = await main.create_sizing_entry(
                _create(entry=150.0, stop_loss=147.0), session
            )

            updated = await main.update_sizing_entry(
                created.id, main.SizingEntryUpdate(entry=155.0), session
            )
            assert updated.entry == 155.0
            assert updated.stop_loss == 147.0, "untouched field must survive"

    asyncio.run(scenario())


@requires_db
def test_update_does_not_move_created_at():
    """A note is exactly as old as when it was first written, however many
    times its numbers change before the three-day clock runs out."""
    async def scenario():
        async with db_transaction() as conn:
            session = db_session(conn)
            created = await main.create_sizing_entry(_create(), session)
            original_created_at = created.created_at

            updated = await main.update_sizing_entry(
                created.id, main.SizingEntryUpdate(entry=200.0), session
            )
            assert updated.created_at == original_created_at

    asyncio.run(scenario())


@requires_db
def test_update_of_a_missing_entry_is_404():
    async def scenario():
        async with db_transaction() as conn:
            session = db_session(conn)
            with pytest.raises(main.HTTPException) as caught:
                await main.update_sizing_entry(
                    uuid.uuid4(), main.SizingEntryUpdate(entry=1.0), session
                )
            assert caught.value.status_code == 404

    asyncio.run(scenario())


@requires_db
def test_delete_removes_the_row():
    async def scenario():
        async with db_transaction() as conn:
            session = db_session(conn)
            created = await main.create_sizing_entry(_create(), session)

            await main.delete_sizing_entry(created.id, session)

            check = db_session(conn)
            listed = await main.list_sizing_entries(check)
            assert all(e.id != created.id for e in listed)

    asyncio.run(scenario())


@requires_db
def test_delete_of_a_missing_entry_is_404():
    async def scenario():
        async with db_transaction() as conn:
            session = db_session(conn)
            with pytest.raises(main.HTTPException) as caught:
                await main.delete_sizing_entry(uuid.uuid4(), session)
            assert caught.value.status_code == 404

    asyncio.run(scenario())


@requires_db
def test_the_direction_check_constraint_is_enforced_in_the_database():
    """Belt and braces: the Pydantic validator is the normal path, but the
    migration's own CHECK constraint is what protects a row written any other
    way -- a script, a future endpoint that forgets the validator."""
    from sqlalchemy.exc import IntegrityError

    async def scenario():
        async with db_transaction() as conn:
            session = db_session(conn)
            # Short enough to fit direction VARCHAR(5) -- long enough and this
            # raises a string-length error before the CHECK is ever reached,
            # which proves the wrong thing.
            bad = main.SizingScratchpadEntry(
                id=uuid.uuid4(), ticker="AAPL", direction="FLAT",
            )
            session.add(bad)
            with pytest.raises(IntegrityError):
                await session.flush()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# The three-day self-clearing
# ---------------------------------------------------------------------------


@requires_db
def test_a_note_older_than_three_days_is_excluded():
    async def scenario():
        async with db_transaction() as conn:
            session = db_session(conn)
            stale = main.SizingScratchpadEntry(
                id=uuid.uuid4(), ticker="OLD", direction="BUY",
                created_at=datetime.now(timezone.utc) - timedelta(days=4),
            )
            session.add(stale)
            await session.commit()

            listed = await main.list_sizing_entries(session)
            assert all(e.id != stale.id for e in listed)

    asyncio.run(scenario())


@requires_db
def test_a_stale_note_is_actually_deleted_not_merely_hidden():
    """The point of doing this on the read path rather than filtering: the
    row must not still be sitting in the table underneath the response."""
    async def scenario():
        async with db_transaction() as conn:
            session = db_session(conn)
            stale = main.SizingScratchpadEntry(
                id=uuid.uuid4(), ticker="OLD", direction="BUY",
                created_at=datetime.now(timezone.utc) - timedelta(days=4),
            )
            session.add(stale)
            await session.commit()

            await main.list_sizing_entries(session)

            check = db_session(conn)
            still_there = await check.get(main.SizingScratchpadEntry, stale.id)
            assert still_there is None

    asyncio.run(scenario())


@requires_db
def test_a_note_just_under_three_days_old_survives():
    """The boundary the deletion must not eat into -- two days eleven hours
    is a note from earlier today's session as far as the user is concerned."""
    async def scenario():
        async with db_transaction() as conn:
            session = db_session(conn)
            fresh = main.SizingScratchpadEntry(
                id=uuid.uuid4(), ticker="FRESH", direction="BUY",
                created_at=datetime.now(timezone.utc) - timedelta(days=2, hours=23),
            )
            session.add(fresh)
            await session.commit()

            listed = await main.list_sizing_entries(session)
            assert any(e.id == fresh.id for e in listed)

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Promotion — the one-way bridge to a real plan
# ---------------------------------------------------------------------------


@requires_db
def test_promoting_creates_a_plan_with_matching_values():
    async def scenario():
        async with db_transaction() as conn:
            session = db_session(conn)
            note = await main.create_sizing_entry(
                _create(
                    ticker="NVDA", direction="BUY",
                    entry=120.0, stop_loss=115.0, take_profit=140.0, quantity=8,
                ),
                session,
            )

            plan = await main.promote_sizing_entry(note.id, session)

            assert plan.ticker == "NVDA"
            assert plan.direction == "BUY"
            assert float(plan.planned_entry) == 120.0
            assert float(plan.stop_loss) == 115.0
            assert float(plan.take_profit) == 140.0
            assert float(plan.quantity) == 8.0
            assert plan.status == main.PLAN_OPEN

    asyncio.run(scenario())


@requires_db
def test_promoting_derives_risk_the_same_way_a_normal_plan_does():
    """Confirms promotion goes through _new_plan rather than hand-copying
    fields -- risk_amount must be entry-minus-stop times quantity, not
    something the scratchpad never had an opinion on."""
    async def scenario():
        async with db_transaction() as conn:
            session = db_session(conn)
            note = await main.create_sizing_entry(
                _create(entry=100.0, stop_loss=95.0, quantity=10), session
            )

            plan = await main.promote_sizing_entry(note.id, session)

            assert float(plan.risk_amount) == pytest.approx(50.0)

    asyncio.run(scenario())


@requires_db
def test_promoting_deletes_the_note():
    async def scenario():
        async with db_transaction() as conn:
            session = db_session(conn)
            note = await main.create_sizing_entry(_create(), session)

            await main.promote_sizing_entry(note.id, session)

            check = db_session(conn)
            still_there = await check.get(main.SizingScratchpadEntry, note.id)
            assert still_there is None

    asyncio.run(scenario())


@requires_db
def test_promoting_a_ticker_only_note_still_works():
    """The scratchpad's own philosophy -- worth keeping with just a ticker --
    has to survive all the way through to a real plan. PlanCreate already
    allows every price field to be absent; promotion must not add a stricter
    requirement of its own."""
    async def scenario():
        async with db_transaction() as conn:
            session = db_session(conn)
            note = await main.create_sizing_entry(_create(ticker="TSLA"), session)

            plan = await main.promote_sizing_entry(note.id, session)

            assert plan.ticker == "TSLA"
            assert plan.planned_entry is None
            assert plan.risk_amount is None

    asyncio.run(scenario())


@requires_db
def test_promoting_a_missing_note_is_404():
    async def scenario():
        async with db_transaction() as conn:
            session = db_session(conn)
            with pytest.raises(main.HTTPException) as caught:
                await main.promote_sizing_entry(uuid.uuid4(), session)
            assert caught.value.status_code == 404

    asyncio.run(scenario())


@requires_db
def test_promotion_does_not_carry_a_strategy_or_thesis():
    """The scratchpad never captures them (see the section header in
    main.py) -- a promoted plan should start bare, same as any plan entered
    without that detail, not silently invent one."""
    async def scenario():
        async with db_transaction() as conn:
            session = db_session(conn)
            note = await main.create_sizing_entry(_create(), session)

            plan = await main.promote_sizing_entry(note.id, session)

            assert plan.strategy_id is None
            assert plan.thesis is None

    asyncio.run(scenario())
