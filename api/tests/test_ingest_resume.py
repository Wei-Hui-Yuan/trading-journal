"""A sync that dies mid-run must not strand its fills forever.

The pipeline promotes staged executions into `trades`, commits, and only then
rebuilds round trips. Anything that kills the process in between -- a container
restart, a redeploy, an OOM, a dropped Supabase connection -- leaves the fills
in the ledger with no positions built from them.

There was no way back. `staged_ids` holds only rows that were NEW to staging,
so re-fetching the same executions returns nothing, `new_executions` is empty,
the symbol list is empty, and those tickers are never matched again. The fills
sit there as phantom open exposure that no statistic can see.

`processed` was added for precisely this -- migration 005 documents it as
existing so "a partial failure mid-ingest can be resumed", and indexes it. It
was being set in the same transaction as the promotion, before the matching it
was meant to guard, so it prevented the resume instead of enabling it.
"""

import asyncio
import os
from dataclasses import dataclass, field

import pytest

os.environ.setdefault("CORS_ALLOW_ORIGINS", "https://trading-journal-test.vercel.app")

import main  # noqa: E402

run = asyncio.run


@dataclass
class _Scalars:
    rows: list

    def all(self):
        return self.rows


@dataclass
class _Result:
    rows: list

    def scalars(self):
        return _Scalars(self.rows)


@dataclass
class _Session:
    """Returns the symbols of staging rows the query would match."""

    unprocessed: list = field(default_factory=list)
    statements: list = field(default_factory=list)

    async def execute(self, stmt):
        self.statements.append(str(stmt))
        return _Result(self.unprocessed)


# ---------------------------------------------------------------------------
# The resume set
# ---------------------------------------------------------------------------


def test_an_interrupted_run_leaves_its_symbols_to_be_picked_up():
    session = _Session(unprocessed=["NVDA", "ACME"])
    assert run(main._symbols_awaiting_match(session)) == ["ACME", "NVDA"]


def test_a_clean_ledger_has_nothing_awaiting():
    """The normal case, and it has to stay cheap: one indexed query returning
    nothing, not a rebuild of anything."""
    session = _Session(unprocessed=[])
    assert run(main._symbols_awaiting_match(session)) == []


def test_the_same_symbol_twice_is_matched_once():
    """Several fills of one ticker strand together; re-running FIFO for it once
    rebuilds all of them."""
    session = _Session(unprocessed=["ACME", "ACME", "ACME"])
    assert run(main._symbols_awaiting_match(session)) == ["ACME"]


def test_it_asks_about_the_flag_and_not_about_position_fills():
    """Pins the choice of question, because the wrong one looks identical from
    the outside. An OPEN position's fills have no position_fills row, so a
    query built on that table reports every running trade as stranded -- on
    every sync, forever -- and rebuilds each of them for nothing.
    """
    session = _Session(unprocessed=[])
    run(main._symbols_awaiting_match(session))

    sql = " ".join(session.statements).lower()
    assert "ibkr_executions" in sql
    assert "processed" in sql
    assert "position_fills" not in sql


def test_a_null_flag_counts_as_unprocessed():
    """The column is nullable. A NULL means never marked, which is exactly the
    state being looked for, so the test cannot be `= false`."""
    session = _Session(unprocessed=[])
    run(main._symbols_awaiting_match(session))

    sql = " ".join(session.statements).lower()
    assert "is not true" in sql, sql


# ---------------------------------------------------------------------------
# What the sync reports
# ---------------------------------------------------------------------------


def test_a_recovery_is_reported_rather_than_silent():
    """A sync that quietly repaired the ledger would move net P&L, trade count
    and the equity curve with nothing on screen to say why."""
    result = main.IngestResult(
        executions_parsed=0, staged_new=0, staged_duplicates=12,
        trades_created=0, trades_duplicates=0, positions_matched=3,
        symbols_touched=["ACME"], skipped_non_tradeable=0,
        symbols_recovered=["ACME"],
    )
    assert result.symbols_recovered == ["ACME"]


def test_an_ordinary_sync_reports_nothing_recovered():
    result = main.IngestResult(
        executions_parsed=0, staged_new=0, staged_duplicates=0,
        trades_created=0, trades_duplicates=0, positions_matched=0,
        symbols_touched=[], skipped_non_tradeable=0,
    )
    assert result.symbols_recovered == []


# ---------------------------------------------------------------------------
# The manual repair path
# ---------------------------------------------------------------------------


@dataclass
class _FirstScalars:
    value: object

    def first(self):
        return self.value

    def all(self):
        return [] if self.value is None else [self.value]


@dataclass
class _FirstResult:
    value: object

    def scalars(self):
        return _FirstScalars(self.value)


@dataclass
class _LookupSession:
    """Answers "does this ticker have any executions?" and nothing else."""

    known: object = None

    async def execute(self, _stmt):
        return _FirstResult(self.known)


def test_rematching_a_ticker_with_no_executions_is_a_404():
    """Better than silently succeeding over an empty set, which would report
    "repaired" for a typo."""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as caught:
        run(main.rematch(ticker="NOSUCH", session=_LookupSession(known=None)))

    assert caught.value.status_code == 404
    assert "NOSUCH" in caught.value.detail


def test_the_rematch_result_reports_what_it_changed():
    """Removed round trips are the part worth reading: re-matching is
    authoritative, so a repair can also delete a round trip FIFO no longer
    produces, and take its review with it."""
    result = main.RematchResult(
        tickers=["ACME", "NVDA"], positions_matched=7,
        positions_removed=1, reviews_discarded=1,
    )
    assert result.tickers == ["ACME", "NVDA"]
    assert result.positions_removed == 1
    assert result.reviews_discarded == 1
