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

import inspect
import os

os.environ.setdefault("CORS_ALLOW_ORIGINS", "https://trading-journal-test.vercel.app")

import main  # noqa: E402

SOURCE = inspect.getsource(main.update_strategy)


def test_the_conflict_message_no_longer_reads_params_name():
    """params.name is None on a request that never touched the name field;
    the message must not be able to say so."""
    assert "{params.name}" not in SOURCE


def test_the_conflict_message_reads_a_name_captured_before_the_commit():
    """Must be assigned before `try: await session.commit()`, not inside the
    `except` block -- a failed commit expires the ORM instance immediately,
    before rollback() is even called, so any later read of an ORM attribute
    needs a reload the async session cannot do implicitly."""
    capture_idx = SOURCE.index("attempted_name = strategy.name")
    try_idx = SOURCE.index("try:")
    commit_idx = SOURCE.index("await session.commit()")
    assert capture_idx < try_idx < commit_idx


def test_the_conflict_message_uses_the_captured_name():
    assert "{attempted_name}" in SOURCE
