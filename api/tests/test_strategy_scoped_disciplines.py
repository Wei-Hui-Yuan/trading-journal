"""Discipline rules that belong to one strategy, or to none (migration 032).

Two properties matter more than the CRUD, and both are database-level
guarantees rather than something the Python handler enforces on its own:

  * The SAME rule name must be usable under two different strategies, and
    REJECTED twice within one -- migration 032's whole point is two partial
    unique indexes rather than the single UNIQUE(name) this replaces, and only
    a real database can prove they behave the way the migration's own comment
    claims.
  * Deleting a strategy must take its own checklist items with it, and no
    others' -- ON DELETE CASCADE, exercised for real rather than asserted from
    the migration file.

`checklist_items` is deliberately excluded from `StrategyUsage.total`: it is
covered here too, because forcing a reassignment target on a never-traded
strategy purely because someone wrote its checklist first would be exactly
the kind of unnecessary friction this app's reassignment flow otherwise goes
out of its way to avoid.
"""

import asyncio
import os
import uuid

import pytest

os.environ.setdefault("CORS_ALLOW_ORIGINS", "https://trading-journal-test.vercel.app")

import main  # noqa: E402

from conftest import db_session, db_transaction, requires_db  # noqa: E402

run = asyncio.run


# ---------------------------------------------------------------------------
# StrategyUsage.total -- no database needed
# ---------------------------------------------------------------------------


def test_checklist_items_do_not_count_toward_total():
    """A strategy with three checklist rules and zero trades must still be
    eligible for the no-reassignment-needed delete path."""
    usage = main.StrategyUsage(trades=0, positions=0, plans=0, checklist_items=3)
    assert usage.total == 0


def test_trades_positions_and_plans_still_do():
    usage = main.StrategyUsage(trades=1, positions=2, plans=3, checklist_items=99)
    assert usage.total == 6


# ---------------------------------------------------------------------------
# create_discipline
# ---------------------------------------------------------------------------


async def _strategy(session, name="Gap and Go"):
    strategy = main.Strategy(id=uuid.uuid4(), name=name)
    session.add(strategy)
    await session.flush()
    return strategy


@requires_db
def test_a_general_rule_has_no_strategy_id():
    # Deliberately not one of migration 013/014's seeded names ("Hard
    # stop-loss set", "Waited for retest", "Followed the plan") -- this
    # database already has those rows, and the test needs a fresh insert to
    # succeed rather than exercise the duplicate-name path.
    async def scenario():
        async with db_transaction() as conn:
            session = db_session(conn)
            out = await main.create_discipline(
                main.DisciplineCreate(name="Sized correctly"), session
            )
            assert out.strategy_id is None

    run(scenario())


@requires_db
def test_a_scoped_rule_carries_its_strategy_id():
    async def scenario():
        async with db_transaction() as conn:
            session = db_session(conn)
            strategy = await _strategy(session)
            out = await main.create_discipline(
                main.DisciplineCreate(
                    name="Waited for the retest", strategy_id=strategy.id
                ),
                session,
            )
            assert out.strategy_id == strategy.id

    run(scenario())


@requires_db
def test_creating_a_rule_under_a_nonexistent_strategy_is_404():
    async def scenario():
        async with db_transaction() as conn:
            session = db_session(conn)
            with pytest.raises(main.HTTPException) as caught:
                await main.create_discipline(
                    main.DisciplineCreate(
                        name="Anything", strategy_id=uuid.uuid4()
                    ),
                    session,
                )
            assert caught.value.status_code == 404

    run(scenario())


@requires_db
def test_the_same_rule_name_is_allowed_under_two_different_strategies():
    """The reason migration 032 exists at all."""
    async def scenario():
        async with db_transaction() as conn:
            session = db_session(conn)
            gap_and_go = await _strategy(session, "Gap and Go")
            breakout = await _strategy(session, "Breakout Retest")

            first = await main.create_discipline(
                main.DisciplineCreate(
                    name="Waited for the retest", strategy_id=gap_and_go.id
                ),
                session,
            )
            second = await main.create_discipline(
                main.DisciplineCreate(
                    name="Waited for the retest", strategy_id=breakout.id
                ),
                session,
            )
            assert first.id != second.id
            assert first.name == second.name

    run(scenario())


@requires_db
def test_a_duplicate_name_within_the_same_strategy_is_409():
    async def scenario():
        async with db_transaction() as conn:
            session = db_session(conn)
            strategy = await _strategy(session)
            await main.create_discipline(
                main.DisciplineCreate(name="Waited for the retest", strategy_id=strategy.id),
                session,
            )
            with pytest.raises(main.HTTPException) as caught:
                await main.create_discipline(
                    main.DisciplineCreate(
                        name="Waited for the retest", strategy_id=strategy.id
                    ),
                    session,
                )
            assert caught.value.status_code == 409
            assert "this strategy" in caught.value.detail

    run(scenario())


@requires_db
def test_a_duplicate_general_name_is_409():
    async def scenario():
        async with db_transaction() as conn:
            session = db_session(conn)
            await main.create_discipline(
                main.DisciplineCreate(name="Sized correctly"), session
            )
            with pytest.raises(main.HTTPException) as caught:
                await main.create_discipline(
                    main.DisciplineCreate(name="Sized correctly"), session
                )
            assert caught.value.status_code == 409
            assert "general rule" in caught.value.detail

    run(scenario())


@requires_db
def test_a_general_name_and_a_scoped_name_can_coexist():
    """The two partial indexes are independent partitions -- the same text
    is not "taken" in one just because it exists in the other."""
    async def scenario():
        async with db_transaction() as conn:
            session = db_session(conn)
            strategy = await _strategy(session)
            general = await main.create_discipline(
                main.DisciplineCreate(name="Waited for the retest"), session
            )
            scoped = await main.create_discipline(
                main.DisciplineCreate(
                    name="Waited for the retest", strategy_id=strategy.id
                ),
                session,
            )
            assert general.id != scoped.id

    run(scenario())


# ---------------------------------------------------------------------------
# _strategy_usage -- checklist_items counted, and cascade delete
# ---------------------------------------------------------------------------


@requires_db
def test_strategy_usage_counts_checklist_items():
    async def scenario():
        async with db_transaction() as conn:
            session = db_session(conn)
            strategy = await _strategy(session)
            await main.create_discipline(
                main.DisciplineCreate(name="Rule one", strategy_id=strategy.id),
                session,
            )
            await main.create_discipline(
                main.DisciplineCreate(name="Rule two", strategy_id=strategy.id),
                session,
            )

            usage = await main._strategy_usage(session)
            assert usage[strategy.id].checklist_items == 2
            assert usage[strategy.id].total == 0

    run(scenario())


@requires_db
def test_deleting_an_unused_strategy_with_only_checklist_items_needs_no_reassignment():
    """The behaviour checklist_items being excluded from `total` exists to
    guarantee: a strategy nobody has traded yet, but whose checklist is
    already written, still deletes outright."""
    async def scenario():
        async with db_transaction() as conn:
            session = db_session(conn)
            strategy = await _strategy(session)
            discipline = await main.create_discipline(
                main.DisciplineCreate(name="Rule one", strategy_id=strategy.id),
                session,
            )

            result = await main.delete_strategy(strategy.id, None, session)
            assert result.deleted_id == strategy.id

            check = db_session(conn)
            assert await check.get(main.Discipline, discipline.id) is None

    run(scenario())


@requires_db
def test_deleting_a_strategy_cascades_only_its_own_checklist_items():
    async def scenario():
        async with db_transaction() as conn:
            session = db_session(conn)
            doomed = await _strategy(session, "Gap and Go")
            survivor = await _strategy(session, "Breakout Retest")

            doomed_rule = await main.create_discipline(
                main.DisciplineCreate(name="Rule one", strategy_id=doomed.id),
                session,
            )
            survivor_rule = await main.create_discipline(
                main.DisciplineCreate(name="Rule one", strategy_id=survivor.id),
                session,
            )
            general_rule = await main.create_discipline(
                main.DisciplineCreate(name="General rule"), session
            )

            await main.delete_strategy(doomed.id, None, session)

            check = db_session(conn)
            assert await check.get(main.Discipline, doomed_rule.id) is None
            assert await check.get(main.Discipline, survivor_rule.id) is not None
            assert await check.get(main.Discipline, general_rule.id) is not None

    run(scenario())
