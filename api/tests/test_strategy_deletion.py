"""Deleting a playbook entry without dissolving the history attached to it.

The three foreign keys pointing at `strategies` are all ON DELETE SET NULL, so
the database would accept a bare delete and lose no trade. What it would lose
is the *attribution*: every trade tagged with that strategy falls into the
"Unassigned" bucket in the analytics breakdown, with nothing left to say which
setup it belonged to. On this account one entry carries 76 of 123 closed
trades, so a single unguarded click destroys the track record of the most-used
setup in the playbook.

So reassignment is mandatory while anything still references the strategy, and
these tests pin the four refusals that enforce it. The success path is covered
too, because the one thing worse than refusing a valid delete is accepting one
and moving only part of the history.
"""

import asyncio
import os
import uuid
from dataclasses import dataclass, field

import pytest
from fastapi import HTTPException

os.environ.setdefault("CORS_ALLOW_ORIGINS", "https://trading-journal-test.vercel.app")

import main  # noqa: E402

# The handlers are async; the suite has no pytest-asyncio. Driving each call
# through asyncio.run keeps these tests dependency-free, and each one awaits
# exactly one thing so there is no event loop to share.
run = asyncio.run


@dataclass
class _Strategy:
    """Stands in for the ORM row; only `name` is read by the handler."""

    id: uuid.UUID
    name: str


@dataclass
class _StubResult:
    rowcount: int


@dataclass
class _StubSession:
    """Records what the handler asked for, without a database.

    `rows_by_model` is how many rows each UPDATE claims to have moved, which is
    what the handler reports back and therefore what the UI shows.
    """

    strategies: dict[uuid.UUID, _Strategy] = field(default_factory=dict)
    rows_by_model: dict[str, int] = field(default_factory=dict)
    updated: list[str] = field(default_factory=list)
    deleted: list[uuid.UUID] = field(default_factory=list)
    commits: int = 0

    async def get(self, _model, key):
        return self.strategies.get(key)

    async def execute(self, stmt):
        table = stmt.table.name
        self.updated.append(table)
        return _StubResult(rowcount=self.rows_by_model.get(table, 0))

    async def delete(self, obj):
        self.deleted.append(obj.id)

    async def commit(self):
        self.commits += 1


def _usage(trades=0, positions=0, plans=0):
    return main.StrategyUsage(trades=trades, positions=positions, plans=plans)


@pytest.fixture
def doomed():
    return _Strategy(id=uuid.uuid4(), name="Breakout form base/within base")


@pytest.fixture
def target():
    return _Strategy(id=uuid.uuid4(), name="DR1")


def _session(*strategies, rows=None):
    return _StubSession(
        strategies={s.id: s for s in strategies},
        rows_by_model=rows or {},
    )


def _patch_usage(monkeypatch, usage):
    async def fake(_session):
        return {} if usage is None else usage

    monkeypatch.setattr(main, "_strategy_usage", fake)


# ---------------------------------------------------------------------------
# How much is at stake
# ---------------------------------------------------------------------------


def test_usage_counts_each_grain_separately():
    """Trades and round trips are different grains of the same history.

    A round trip is built FROM trades, so adding the two would report one
    trade twice and overstate what a delete would touch.
    """
    u = _usage(trades=76, positions=1, plans=1)
    assert (u.trades, u.positions, u.plans) == (76, 1, 1)
    assert u.total == 78


def test_an_untouched_strategy_totals_zero():
    assert _usage().total == 0


def test_usage_is_absent_rather_than_zero_on_a_fresh_strategy():
    """`None` on create/update means "not counted", not "counted, found none".

    A zero there would read as a promise the response never made.
    """
    assert "usage" in main.StrategyOut.model_fields
    assert main.StrategyOut.model_fields["usage"].default is None


# ---------------------------------------------------------------------------
# The refusals
# ---------------------------------------------------------------------------


def test_a_used_strategy_cannot_be_deleted_without_a_target(monkeypatch, doomed):
    """The whole feature. Without this the 76 trades go Unassigned in silence."""
    _patch_usage(monkeypatch, {doomed.id: _usage(trades=76, positions=1, plans=1)})
    session = _session(doomed)

    with pytest.raises(HTTPException) as excinfo:
        run(main.delete_strategy(doomed.id, reassign_to=None, session=session))

    assert excinfo.value.status_code == 409
    # The counts belong in the message: "this is in use" does not tell you
    # whether you are about to move one trade or most of your history.
    assert "76 trade(s)" in excinfo.value.detail
    assert session.deleted == []
    assert session.commits == 0


def test_a_strategy_cannot_be_reassigned_to_itself(monkeypatch, doomed):
    """Otherwise the UPDATE is a no-op and the delete strands every row."""
    _patch_usage(monkeypatch, {doomed.id: _usage(trades=76)})
    session = _session(doomed)

    with pytest.raises(HTTPException) as excinfo:
        run(main.delete_strategy(doomed.id, reassign_to=doomed.id, session=session))

    assert excinfo.value.status_code == 422
    assert session.deleted == []


def test_a_missing_reassign_target_is_refused(monkeypatch, doomed):
    """A bad target id would otherwise write a dangling FK, or fail mid-move."""
    _patch_usage(monkeypatch, {doomed.id: _usage(trades=76)})
    session = _session(doomed)

    with pytest.raises(HTTPException) as excinfo:
        run(main.delete_strategy(doomed.id, reassign_to=uuid.uuid4(), session=session))

    assert excinfo.value.status_code == 404
    assert session.deleted == []
    assert session.commits == 0


def test_a_missing_strategy_is_a_404(monkeypatch):
    _patch_usage(monkeypatch, {})
    session = _session()

    with pytest.raises(HTTPException) as excinfo:
        run(main.delete_strategy(uuid.uuid4(), reassign_to=None, session=session))

    assert excinfo.value.status_code == 404


# ---------------------------------------------------------------------------
# The paths that go through
# ---------------------------------------------------------------------------


def test_an_unused_strategy_deletes_without_a_target(monkeypatch, doomed):
    """Nothing references it, so there is nothing to preserve.

    Demanding a reassignment target here would make tidying the playbook
    needlessly awkward.
    """
    _patch_usage(monkeypatch, {})
    session = _session(doomed)

    result = run(main.delete_strategy(doomed.id, reassign_to=None, session=session))

    assert result.deleted_name == doomed.name
    assert result.reassigned_to_id is None
    assert result.trades_reassigned == 0
    assert session.deleted == [doomed.id]
    assert session.commits == 1


def test_every_referencing_table_is_moved(monkeypatch, doomed, target):
    """All three, not just `trades`.

    `positions` and `planned_trades` carry strategy_id too. Missing either
    would leave rows pointing at a deleted strategy -- which the FK then
    quietly NULLs, reintroducing the exact bug this endpoint prevents.
    """
    _patch_usage(monkeypatch, {doomed.id: _usage(trades=76, positions=1, plans=1)})
    session = _session(
        doomed, target, rows={"trades": 76, "positions": 1, "planned_trades": 1}
    )

    result = run(
        main.delete_strategy(doomed.id, reassign_to=target.id, session=session)
    )

    assert set(session.updated) == {"trades", "positions", "planned_trades"}
    assert result.trades_reassigned == 76
    assert result.positions_reassigned == 1
    assert result.plans_reassigned == 1


def test_the_move_happens_before_the_delete(monkeypatch, doomed, target):
    """Order is load-bearing.

    Deleting first would let the FK's ON DELETE SET NULL fire and blank every
    strategy_id, leaving the UPDATE nothing to find.
    """
    _patch_usage(monkeypatch, {doomed.id: _usage(trades=76)})
    session = _session(doomed, target, rows={"trades": 76})

    run(main.delete_strategy(doomed.id, reassign_to=target.id, session=session))

    assert session.updated, "no UPDATE was issued"
    assert session.deleted == [doomed.id]
    # One commit: the moves and the delete land together or not at all.
    assert session.commits == 1


def test_the_result_names_both_ends_of_the_move(monkeypatch, doomed, target):
    """So the confirmation can say where the history went, not just that it went."""
    _patch_usage(monkeypatch, {doomed.id: _usage(trades=76)})
    session = _session(doomed, target, rows={"trades": 76})

    result = run(
        main.delete_strategy(doomed.id, reassign_to=target.id, session=session)
    )

    assert result.deleted_name == "Breakout form base/within base"
    assert result.reassigned_to_name == "DR1"
    assert result.reassigned_to_id == target.id
