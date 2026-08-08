"""Pre-trade checklist answers on a plan, and their one-time trip into a
review (migration 033).

Three properties matter, each requiring a real database rather than an
assertion about the migration file:

  * A plan's checklist answers persist and round-trip through create, update
    and list, upserting rather than duplicating on a second save -- the same
    guarantee migration 014 gives position_disciplines, reused here via
    `_upsert_discipline_answers` rather than reimplemented.
  * `list_positions` seeds a pending position's checklist from the plan that
    opened it (positions.open_trade_id -> trades.plan_id -> plan_disciplines)
    for any rule the position has not itself been reviewed against -- and
    ONLY for a rule the review has not already answered, and ONLY while the
    position is still pending. A real answer always wins, and a reviewed
    position never gets a plan default grafted onto it.
  * Both join tables cascade the same way position_disciplines already does:
    deleting the owning row (a plan) or the rule (a discipline) takes the
    answer with it.
"""

import asyncio
import os
import uuid
from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError

os.environ.setdefault("CORS_ALLOW_ORIGINS", "https://trading-journal-test.vercel.app")

import main  # noqa: E402

from conftest import db_session, db_transaction, requires_db  # noqa: E402

run = asyncio.run


# ---------------------------------------------------------------------------
# Schema -- no database needed
# ---------------------------------------------------------------------------


def test_plan_create_accepts_discipline_answers():
    plan = main.PlanCreate(
        ticker="X", direction="BUY", disciplines={uuid.uuid4(): True}
    )
    assert len(plan.disciplines) == 1


def test_plan_update_accepts_discipline_answers():
    changes = main.PlanUpdate(disciplines={uuid.uuid4(): False}).model_dump(
        exclude_unset=True
    )
    assert "disciplines" in changes


def test_a_plan_needs_no_checklist_to_save():
    """Same "optional until you have an opinion" rule as every other field."""
    assert main.PlanCreate(ticker="X", direction="BUY").disciplines is None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


async def _discipline(session, name="Waited for the retest", strategy_id=None):
    out = await main.create_discipline(
        main.DisciplineCreate(name=name, strategy_id=strategy_id), session
    )
    return out.id


async def _pending_position(session, plan_id=None):
    """A closed round trip whose opening fill came from `plan_id` (or from no
    plan at all), still awaiting review."""
    sym = f"ZZ{uuid.uuid4().hex[:6].upper()}"[:10]
    now = datetime.now(main.MARKET_TZ)
    open_id, close_id, pos_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    session.add_all([
        main.Trade(
            id=open_id, ibkr_exec_id=f"IBKR-{uuid.uuid4().hex[:12]}", ticker=sym,
            direction="BUY", style="Unclassified", quantity=main.Decimal("10"),
            actual_entry=main.Decimal("100"), entry_date=now - timedelta(days=2),
            source_tag="IBKR", plan_id=plan_id,
        ),
        main.Trade(
            id=close_id, ibkr_exec_id=f"IBKR-{uuid.uuid4().hex[:12]}", ticker=sym,
            direction="SELL", style="Unclassified", quantity=main.Decimal("10"),
            actual_entry=main.Decimal("110"), entry_date=now - timedelta(days=1),
            source_tag="IBKR",
        ),
    ])
    await session.flush()
    position = main.Position(
        id=pos_id, symbol=sym, direction="LONG", style="Unclassified",
        quantity=main.Decimal("10"), entry_price=main.Decimal("100"),
        exit_price=main.Decimal("110"), entry_time=now - timedelta(days=2),
        exit_time=now - timedelta(days=1), realized_pnl=main.Decimal("100"),
        gross_pnl=main.Decimal("100"), commission=main.Decimal("0"),
        open_trade_id=open_id, close_trade_id=close_id,
        review_status=main.ReviewStatus.pending.value,
    )
    session.add(position)
    await session.flush()
    return position


# ---------------------------------------------------------------------------
# create_plan / update_plan / list_plans
# ---------------------------------------------------------------------------


@requires_db
def test_create_plan_persists_and_returns_disciplines():
    async def scenario():
        async with db_transaction() as conn:
            session = db_session(conn)
            rule_id = await _discipline(session, "Sized correctly")
            out = await main.create_plan(
                main.PlanCreate(
                    ticker="AAPL", direction="BUY", disciplines={rule_id: True}
                ),
                session,
            )
            assert out.disciplines == [
                main.PositionDisciplineOut(
                    discipline_id=rule_id, name="Sized correctly", followed=True
                )
            ]

    run(scenario())


@requires_db
def test_update_plan_upserts_rather_than_duplicates():
    async def scenario():
        async with db_transaction() as conn:
            session = db_session(conn)
            rule_id = await _discipline(session, "Sized correctly")
            plan = await main._new_plan(
                session, main.PlanCreate(ticker="AAPL", direction="BUY")
            )
            await session.commit()

            first = await main.update_plan(
                plan.id, main.PlanUpdate(disciplines={rule_id: True}), session
            )
            assert [d.followed for d in first.disciplines] == [True]

            second = await main.update_plan(
                plan.id, main.PlanUpdate(disciplines={rule_id: False}), session
            )
            # Corrected, not appended -- one row per (plan, rule).
            assert [d.followed for d in second.disciplines] == [False]

    run(scenario())


@requires_db
def test_editing_disciplines_on_an_attached_plan_is_blocked():
    """Frozen like every other field on an ATTACHED plan -- see update_plan's
    own docstring for why: the plan's checklist is what you intended before
    the fill, and a fill already exists."""
    async def scenario():
        async with db_transaction() as conn:
            session = db_session(conn)
            rule_id = await _discipline(session, "Sized correctly")
            plan = await main._new_plan(
                session, main.PlanCreate(ticker="AAPL", direction="BUY")
            )
            plan.status = main.PLAN_ATTACHED
            await session.commit()

            with pytest.raises(main.HTTPException) as caught:
                await main.update_plan(
                    plan.id, main.PlanUpdate(disciplines={rule_id: True}), session
                )
            assert caught.value.status_code == 409

    run(scenario())


@requires_db
def test_unknown_discipline_id_on_a_plan_is_400():
    async def scenario():
        async with db_transaction() as conn:
            session = db_session(conn)
            with pytest.raises(main.HTTPException) as caught:
                await main.create_plan(
                    main.PlanCreate(
                        ticker="AAPL", direction="BUY",
                        disciplines={uuid.uuid4(): True},
                    ),
                    session,
                )
            assert caught.value.status_code == 400

    run(scenario())


@requires_db
def test_list_plans_includes_each_plans_own_disciplines():
    async def scenario():
        async with db_transaction() as conn:
            session = db_session(conn)
            rule_id = await _discipline(session, "Sized correctly")
            await main.create_plan(
                main.PlanCreate(
                    ticker="AAPL", direction="BUY", disciplines={rule_id: True}
                ),
                session,
            )
            await main.create_plan(
                main.PlanCreate(ticker="MSFT", direction="BUY"), session
            )

            out = await main.list_plans(session=session)
            by_ticker = {p.ticker: p for p in out}
            assert [d.discipline_id for d in by_ticker["AAPL"].disciplines] == [rule_id]
            assert by_ticker["MSFT"].disciplines == []

    run(scenario())


# ---------------------------------------------------------------------------
# Cascades
# ---------------------------------------------------------------------------


@requires_db
def test_deleting_the_plan_cascades_its_checklist_answers():
    async def scenario():
        async with db_transaction() as conn:
            session = db_session(conn)
            rule_id = await _discipline(session, "Sized correctly")
            plan = await main._new_plan(
                session, main.PlanCreate(ticker="AAPL", direction="BUY")
            )
            await main._upsert_discipline_answers(
                session, main.PlanDiscipline, "plan_id", plan.id, {rule_id: True}
            )
            await session.commit()

            await session.delete(plan)
            await session.commit()

            remaining = await main._disciplines_by_plan(session, [plan.id])
            assert remaining == {}

    run(scenario())


@requires_db
def test_deleting_the_rule_cascades_its_plan_answers():
    async def scenario():
        async with db_transaction() as conn:
            session = db_session(conn)
            rule_id = await _discipline(session, "Sized correctly")
            plan = await main._new_plan(
                session, main.PlanCreate(ticker="AAPL", direction="BUY")
            )
            await main._upsert_discipline_answers(
                session, main.PlanDiscipline, "plan_id", plan.id, {rule_id: True}
            )
            await session.commit()

            await main.delete_discipline(rule_id, session)

            remaining = await main._disciplines_by_plan(session, [plan.id])
            assert remaining == {}

    run(scenario())


# ---------------------------------------------------------------------------
# list_positions: the plan-default merge
# ---------------------------------------------------------------------------


@requires_db
def test_pending_position_is_seeded_from_its_origin_plan():
    async def scenario():
        async with db_transaction() as conn:
            session = db_session(conn)
            rule_id = await _discipline(session, "Sized correctly")
            plan = await main._new_plan(
                session, main.PlanCreate(ticker="AAPL", direction="BUY")
            )
            await main._upsert_discipline_answers(
                session, main.PlanDiscipline, "plan_id", plan.id, {rule_id: True}
            )
            position = await _pending_position(session, plan_id=plan.id)
            await session.commit()

            out = await main.list_positions("pending", session)
            row = next(p for p in out if p.id == position.id)
            assert row.disciplines == [
                main.PositionDisciplineOut(
                    discipline_id=rule_id, name="Sized correctly", followed=True
                )
            ]

    run(scenario())


@requires_db
def test_a_real_answer_beats_the_plans_default():
    async def scenario():
        async with db_transaction() as conn:
            session = db_session(conn)
            rule_id = await _discipline(session, "Sized correctly")
            plan = await main._new_plan(
                session, main.PlanCreate(ticker="AAPL", direction="BUY")
            )
            await main._upsert_discipline_answers(
                session, main.PlanDiscipline, "plan_id", plan.id, {rule_id: True}
            )
            position = await _pending_position(session, plan_id=plan.id)
            # Reviewed against the same rule already, and disagreed with the
            # plan -- this is the trade, not the intent, and must win.
            await main._upsert_discipline_answers(
                session, main.PositionDiscipline, "position_id", position.id,
                {rule_id: False},
            )
            await session.commit()

            out = await main.list_positions("pending", session)
            row = next(p for p in out if p.id == position.id)
            assert row.disciplines == [
                main.PositionDisciplineOut(
                    discipline_id=rule_id, name="Sized correctly", followed=False
                )
            ]

    run(scenario())


@requires_db
def test_a_reviewed_position_gets_no_plan_default():
    async def scenario():
        async with db_transaction() as conn:
            session = db_session(conn)
            rule_id = await _discipline(session, "Sized correctly")
            plan = await main._new_plan(
                session, main.PlanCreate(ticker="AAPL", direction="BUY")
            )
            await main._upsert_discipline_answers(
                session, main.PlanDiscipline, "plan_id", plan.id, {rule_id: True}
            )
            position = await _pending_position(session, plan_id=plan.id)
            position.review_status = main.ReviewStatus.reviewed.value
            await session.commit()

            out = await main.list_positions(None, session)
            row = next(p for p in out if p.id == position.id)
            assert row.disciplines == []

    run(scenario())


@requires_db
def test_a_position_with_no_origin_plan_gets_no_default():
    async def scenario():
        async with db_transaction() as conn:
            session = db_session(conn)
            await _discipline(session, "Sized correctly")
            position = await _pending_position(session, plan_id=None)
            await session.commit()

            out = await main.list_positions("pending", session)
            row = next(p for p in out if p.id == position.id)
            assert row.disciplines == []

    run(scenario())

