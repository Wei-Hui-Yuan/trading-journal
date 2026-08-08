"""Saved custom windows: validation, CRUD behaviour, and how they fail.

A preset is small, but it is the thing standing between the trader and every
figure on the dashboard, so the ways it can be wrong all produce a dashboard
that is silently describing the wrong span. Each of those is refused with a
message naming the field rather than allowed through to become an empty chart.
"""

import asyncio
import os
import uuid
from dataclasses import dataclass, field
from datetime import date

import pytest
from pydantic import ValidationError

os.environ.setdefault("CORS_ALLOW_ORIGINS", "https://trading-journal-test.vercel.app")

import main  # noqa: E402

run = asyncio.run


@dataclass
class _Session:
    """Enough session to drive the handlers that only get/add/commit."""

    stored: dict = field(default_factory=dict)
    committed: bool = False
    deleted: list = field(default_factory=list)
    added: list = field(default_factory=list)
    raise_on_commit: Exception | None = None

    async def get(self, _model, key):
        return self.stored.get(key)

    def add(self, row):
        self.added.append(row)

    async def commit(self):
        if self.raise_on_commit is not None:
            raise self.raise_on_commit
        self.committed = True

    async def rollback(self):
        self.committed = False

    async def refresh(self, _row):
        return None

    async def delete(self, row):
        self.deleted.append(row)


def preset_row(name="2025 Full Year", start=date(2025, 1, 1), end=date(2025, 12, 31)):
    return main.TimeframePreset(
        id=uuid.uuid4(), name=name, start_date=start, end_date=end
    )


# ---------------------------------------------------------------------------
# What a preset is allowed to be
# ---------------------------------------------------------------------------


def test_a_valid_preset_round_trips():
    body = main.TimeframePresetIn(
        name="2025 Full Year", start_date=date(2025, 1, 1), end_date=date(2025, 12, 31)
    )
    assert body.name == "2025 Full Year"


def test_a_backwards_range_is_refused_by_the_model():
    """Caught in Pydantic so the 422 names the field. Reaching the database
    instead surfaces `timeframe_presets_range_check` in a 500 the client cannot
    do anything with."""
    with pytest.raises(ValidationError, match="on or before"):
        main.TimeframePresetIn(
            name="Backwards", start_date=date(2025, 12, 31), end_date=date(2025, 1, 1)
        )


def test_a_single_day_window_is_allowed():
    """start == end is a legitimate question: "what did I do that day"."""
    body = main.TimeframePresetIn(
        name="Fed day", start_date=date(2026, 3, 18), end_date=date(2026, 3, 18)
    )
    assert body.start_date == body.end_date


def test_a_blank_name_is_refused():
    with pytest.raises(ValidationError):
        main.TimeframePresetIn(
            name="   ", start_date=date(2025, 1, 1), end_date=date(2025, 12, 31)
        )


def test_a_name_is_trimmed():
    """So '2025' and '2025 ' cannot become two pills that render identically.
    The unique index compares lower(btrim(name)) for the same reason."""
    body = main.TimeframePresetIn(
        name="  Q1  ", start_date=date(2025, 1, 1), end_date=date(2025, 3, 31)
    )
    assert body.name == "Q1"


def test_both_dates_are_required():
    """Deliberately stricter than the ad-hoc query parameters, which do accept
    an open end. A SAVED window with no end would mean something different
    every time it was opened."""
    with pytest.raises(ValidationError):
        main.TimeframePresetIn(name="Since March", start_date=date(2026, 3, 1))


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def test_creating_a_preset_stores_the_trimmed_name():
    session = _Session()
    body = main.TimeframePresetIn(
        name="  2025 Full Year  ", start_date=date(2025, 1, 1), end_date=date(2025, 12, 31)
    )
    out = run(main.create_timeframe(body, session))
    assert out.name == "2025 Full Year"
    assert session.committed and len(session.added) == 1


def test_a_duplicate_name_is_a_409_not_a_500():
    from sqlalchemy.exc import IntegrityError

    session = _Session(raise_on_commit=IntegrityError("stmt", {}, Exception("dup")))
    body = main.TimeframePresetIn(
        name="2025", start_date=date(2025, 1, 1), end_date=date(2025, 12, 31)
    )
    with pytest.raises(main.HTTPException) as caught:
        run(main.create_timeframe(body, session))

    assert caught.value.status_code == 409
    assert "2025" in caught.value.detail


def test_editing_a_missing_preset_is_a_404():
    body = main.TimeframePresetIn(
        name="Gone", start_date=date(2025, 1, 1), end_date=date(2025, 12, 31)
    )
    with pytest.raises(main.HTTPException) as caught:
        run(main.update_timeframe(uuid.uuid4(), body, _Session()))
    assert caught.value.status_code == 404


def test_editing_replaces_every_field():
    """A full replacement, not a patch: the three fields are one statement
    about a window, and moving an end date without its start in view is how a
    range ends up backwards."""
    row = preset_row()
    session = _Session(stored={row.id: row})
    body = main.TimeframePresetIn(
        name="2025 H1", start_date=date(2025, 1, 1), end_date=date(2025, 6, 30)
    )
    out = run(main.update_timeframe(row.id, body, session))
    assert (out.name, out.start_date, out.end_date) == (
        "2025 H1", date(2025, 1, 1), date(2025, 6, 30)
    )
    assert session.committed


def test_deleting_a_missing_preset_is_a_404():
    with pytest.raises(main.HTTPException) as caught:
        run(main.delete_timeframe(uuid.uuid4(), _Session()))
    assert caught.value.status_code == 404


def test_deleting_returns_the_row_it_removed():
    """So the confirmation can name what went without the client having had to
    hold onto it."""
    row = preset_row(name="Doomed")
    session = _Session(stored={row.id: row})
    out = run(main.delete_timeframe(row.id, session))
    assert out.name == "Doomed"
    assert session.deleted == [row] and session.committed


# ---------------------------------------------------------------------------
# Degrading when the migration has not been applied
# ---------------------------------------------------------------------------


def test_listing_degrades_to_empty_when_the_table_is_missing():
    """An unapplied migration 021 must cost the toolbar its custom pills, not
    the whole dashboard."""
    from asyncpg.exceptions import UndefinedTableError
    from sqlalchemy.exc import ProgrammingError

    @dataclass
    class _MissingTable:
        async def execute(self, _stmt):
            raise ProgrammingError("stmt", {}, UndefinedTableError("no table"))

        async def rollback(self):
            return None

    assert run(main.list_timeframes(_MissingTable())) == []


def test_listing_does_not_swallow_other_database_errors():
    """A dropped connection must not read as "you have no saved timeframes".
    Same reasoning as list_disciplines: an empty list is indistinguishable from
    the truth, which is how a dead dashboard once read as a flat 0% win rate."""
    from sqlalchemy.exc import ProgrammingError

    @dataclass
    class _OtherFault:
        async def execute(self, _stmt):
            raise ProgrammingError("stmt", {}, Exception("connection reset"))

        async def rollback(self):
            return None

    with pytest.raises(ProgrammingError):
        run(main.list_timeframes(_OtherFault()))


# ---------------------------------------------------------------------------
# The dashboard endpoint's own validation
# ---------------------------------------------------------------------------


# The endpoint takes `request` and `response` so it can answer a conditional
# request with a 304 and tag the 200 it does send. Neither is reached on the
# paths below -- the window is resolved first, and an invalid one raises before
# anything is read or written -- but the signature still has to be satisfied.
@dataclass
class _NoConditionalHeaders:
    """A request offering no If-None-Match, so nothing can match it."""

    headers: dict = field(default_factory=dict)


@dataclass
class _CollectsHeaders:
    """Stands in for the Response FastAPI injects, whose headers the handler
    sets the ETag on."""

    headers: dict = field(default_factory=dict)


def test_the_dashboard_refuses_an_unknown_preset_with_a_422():
    with pytest.raises(main.HTTPException) as caught:
        run(main.analytics_dashboard(
            _NoConditionalHeaders(), _CollectsHeaders(),
            preset="3Y", session=_Session(),
        ))
    assert caught.value.status_code == 422
    assert "YTD" in caught.value.detail


def test_the_dashboard_refuses_a_backwards_range_with_a_422():
    """Rather than answering with an empty payload the user has to diagnose."""
    with pytest.raises(main.HTTPException) as caught:
        run(main.analytics_dashboard(
            _NoConditionalHeaders(), _CollectsHeaders(),
            start_date=date(2026, 5, 1), end_date=date(2026, 1, 1),
            session=_Session(),
        ))
    assert caught.value.status_code == 422
    assert "after end_date" in caught.value.detail


def test_validation_happens_before_the_cache_is_consulted():
    """A 422 must not depend on the database being reachable. `_Session` here
    raises on any query, so if the version lookup moved above resolve_window
    this would surface as that error instead of the 422 the caller needs."""
    with pytest.raises(main.HTTPException) as caught:
        run(main.analytics_dashboard(
            _NoConditionalHeaders(), _CollectsHeaders(),
            preset="NOPE", session=_Session(),
        ))
    assert caught.value.status_code == 422
