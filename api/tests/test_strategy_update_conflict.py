"""The name in a strategy-name conflict message must be the one that conflicts.

`update_strategy` is a partial update: `PATCH /api/strategies/{id}` with only
`{"description": "..."}` leaves `params.name` at its Pydantic default of
`None`, because the field was never in the request. The handler's
IntegrityError branch used to build its message from `params.name` anyway, so
that request -- one that never mentioned a name at all -- would have reported
"A strategy named 'None' already exists".

Verified live against Supabase that this exact branch is not reachable today:
`strategies` has exactly one constraint (`name`, NOT NULL UNIQUE), and neither
Postgres nor SQLAlchemy re-validates a column an UPDATE never sets, so a
request that omits `name` cannot itself produce a name conflict. The fix is
correct and worth having regardless -- it stops attributing a hypothetical
future conflict, or a differently-shaped one, to the wrong value -- and
`strategy.name` is simply the value the constraint is about either way,
whether or not `name` was in this particular request.

Fixing this uncovered a sharper bug in the first attempt at fixing it:
`strategy.name`, read from the `except` block after `session.rollback()`,
raises `MissingGreenlet` -- rollback() expires every instance in the session,
and an async session cannot do the implicit reload that reading an expired
attribute would need. Reading it from the except block BEFORE rollback() is
no better: a failed flush expires the instance immediately, so that ordering
raises `PendingRollbackError` instead. Both were confirmed live. The only
correct point to read it is before the commit is attempted at all, while the
object still holds its ordinary in-memory value -- which is also exactly the
value the failing commit was trying to write.
"""

import asyncio
import inspect
import os
import uuid

import pytest
from fastapi import HTTPException

os.environ.setdefault("CORS_ALLOW_ORIGINS", "https://trading-journal-test.vercel.app")

import main  # noqa: E402

from conftest import db_session, db_transaction, requires_db  # noqa: E402

SOURCE = inspect.getsource(main.update_strategy)


def test_the_conflict_message_reads_a_name_captured_before_the_commit():
    """Must be assigned before `try: await session.commit()`, not inside the
    `except` block -- a failed commit expires the ORM instance immediately,
    before rollback() is even called, so any later read of an ORM attribute
    needs a reload the async session cannot do implicitly. Kept as a
    structural pin (rather than replaced outright by the behavioural test
    below) because it guards a real trap that is easy to reintroduce by
    moving one line: reading `strategy.name` back inside the `except` block
    looks equally reasonable and raises PendingRollbackError or
    MissingGreenlet instead, both confirmed live while fixing this."""
    capture_idx = SOURCE.index("attempted_name = strategy.name")
    try_idx = SOURCE.index("try:")
    commit_idx = SOURCE.index("await session.commit()")
    assert capture_idx < try_idx < commit_idx


# ---------------------------------------------------------------------------
# The reachable path, against real Postgres
# ---------------------------------------------------------------------------
#
# Fault-injection during verification found that this test CANNOT distinguish
# `params.name` from `attempted_name` -- reverting the fix and rerunning still
# passed. That is not a weak test; it is a fact about the code proven here
# structurally: `strategies` carries exactly one NOT NULL/UNIQUE constraint,
# `name` itself (see the model), so the only request that can ever land in
# this except block is one that SETS `name` to the colliding value -- which
# means params.name and attempted_name are always equal on every reachable
# path. This test still earns its place: it drives the real path end to end
# and pins that the 409 body is sane and the handler does not crash (the
# MissingGreenlet / PendingRollbackError risk the structural test above also
# guards). It just is not, and cannot be, the test that would catch someone
# swapping `attempted_name` back to `params.name` -- only the structural
# ordering test above does that, which is why it was kept rather than
# replaced.


@requires_db
def test_renaming_onto_a_taken_name_reports_that_name_not_none():
    async def scenario():
        async with db_transaction() as conn:
            taken = f"ZZ-TAKEN-{uuid.uuid4().hex[:6]}"
            victim = f"ZZ-VICTIM-{uuid.uuid4().hex[:6]}"
            setup = db_session(conn)
            a = main.Strategy(id=uuid.uuid4(), name=taken, method="m",
                               entry_criteria="", exit_criteria="")
            b = main.Strategy(id=uuid.uuid4(), name=victim, method="m",
                               entry_criteria="", exit_criteria="")
            setup.add_all([a, b])
            await setup.commit()
            bid = b.id
            await setup.close()

            session = db_session(conn)
            with pytest.raises(HTTPException) as excinfo:
                await main.update_strategy(
                    strategy_id=bid, params=main.StrategyUpdate(name=taken),
                    session=session,
                )
            await session.close()

            assert excinfo.value.status_code == 409
            assert taken in excinfo.value.detail
            assert "None" not in excinfo.value.detail

    asyncio.run(scenario())


@requires_db
def test_a_partial_update_omitting_name_still_succeeds():
    """The claim the fix's docstring makes: today, omitting `name` cannot
    itself trip the conflict branch. If a future constraint changes that,
    this starts failing and says so, rather than the assumption going stale
    silently."""
    async def scenario():
        async with db_transaction() as conn:
            setup = db_session(conn)
            victim = f"ZZ-VICTIM-{uuid.uuid4().hex[:6]}"
            s = main.Strategy(id=uuid.uuid4(), name=victim, method="m",
                               entry_criteria="", exit_criteria="")
            setup.add(s)
            await setup.commit()
            sid = s.id
            await setup.close()

            session = db_session(conn)
            out = await main.update_strategy(
                strategy_id=sid,
                params=main.StrategyUpdate(description="touches nothing but this"),
                session=session,
            )
            await session.close()

            assert out.name == victim

    asyncio.run(scenario())
