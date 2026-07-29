"""Dashboard timeframe windows, and the ceiling on the equity curve's span.

Two separate things are pinned here.

WINDOWS. The dashboard filters every figure it reports to one span, selected by
when a round trip CLOSED. The tests below fix what each built-in preset means,
that explicit dates beat a preset, and that a backwards range is refused rather
than silently returning nothing.

THE CLAMP. `build_equity_curve` emits one point per calendar day between the
first and last close. That is deliberate -- the gaps carry information -- but
the loop is driven by whatever dates are in the data, so a single corrupt
timestamp stretches it across decades. One 1970 row beside ordinary 2026 data
produced 20,637 points in a single response, which is megabytes over the wire
and enough series to lock the tab. MAX_CURVE_DAYS is the backstop, and
`truncated` is how the payload admits it fired.
"""

import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

os.environ.setdefault("CORS_ALLOW_ORIGINS", "https://trading-journal-test.vercel.app")

from services.analytics import (  # noqa: E402
    DEFAULT_PRESET,
    MAX_CURVE_DAYS,
    ONE_YEAR_DAYS,
    PRESET_1Y,
    PRESET_ALL,
    PRESET_YTD,
    ClosedPosition,
    Window,
    build_equity_curve,
    clamp_to_max_span,
    filter_by_exit_window,
    resolve_window,
)

TODAY = date(2026, 7, 30)


def closed(exit_day: date, pnl="10", entry_day: date | None = None) -> ClosedPosition:
    """A closed round trip that exited on `exit_day`, at 15:00 market time."""
    entry = entry_day or exit_day

    def at(day: date) -> datetime:
        # 19:00 UTC is 15:00 ET in summer and 14:00 ET in winter -- either way
        # comfortably inside the same market calendar day, so these fixtures
        # never sit on a boundary the conversion could move them across.
        return datetime(day.year, day.month, day.day, 19, 0, tzinfo=timezone.utc)

    return ClosedPosition(
        realized_pnl=Decimal(pnl),
        gross_pnl=Decimal(pnl),
        commission=Decimal("0"),
        entry_price=Decimal("100"),
        quantity=Decimal("1"),
        entry_time=at(entry),
        exit_time=at(exit_day),
    )


# ---------------------------------------------------------------------------
# What each preset means
# ---------------------------------------------------------------------------


def test_no_parameters_defaults_to_one_year():
    """A bare dashboard request is bounded. Plotting the whole ledger by
    default is what makes the first paint grow without limit."""
    window = resolve_window(today=TODAY)
    assert window.preset == DEFAULT_PRESET == PRESET_1Y
    assert window.start == TODAY - timedelta(days=ONE_YEAR_DAYS)
    assert window.end == TODAY


def test_ytd_starts_on_january_first_of_the_current_year():
    window = resolve_window(PRESET_YTD, today=date(2026, 3, 15))
    assert (window.start, window.end) == (date(2026, 1, 1), date(2026, 3, 15))


def test_all_is_genuinely_unbounded_not_a_sentinel_date():
    """ALL carries no dates at all.

    Expressing it as "start at 1900" would work until the clamp fired, at
    which point an ordinary all-time view would be indistinguishable from one
    containing a corrupt timestamp.
    """
    window = resolve_window(PRESET_ALL, today=TODAY)
    assert window.is_unbounded
    assert (window.start, window.end) == (None, None)


def test_preset_matching_ignores_case():
    assert resolve_window("ytd", today=TODAY).preset == PRESET_YTD


def test_three_year_is_not_a_preset():
    """Pinned because it is a plausible thing to add and the toolbar is
    specified as exactly YTD / 1Y / ALL."""
    with pytest.raises(ValueError, match="preset must be one of"):
        resolve_window("3Y", today=TODAY)


def test_explicit_dates_beat_a_preset():
    """A saved custom window sends dates, and must mean exactly what it says
    even if a stale preset name rides along with it."""
    window = resolve_window(PRESET_YTD, date(2025, 1, 1), date(2025, 12, 31), today=TODAY)
    assert (window.start, window.end) == (date(2025, 1, 1), date(2025, 12, 31))
    # No preset: the UI uses this to tell a saved pill from a built-in one.
    assert window.preset is None


def test_a_half_open_explicit_range_is_allowed():
    window = resolve_window(None, date(2026, 1, 1), None, today=TODAY)
    assert (window.start, window.end) == (date(2026, 1, 1), None)


def test_a_backwards_range_is_refused():
    """Refused rather than returned empty. A dashboard showing nothing is a
    symptom with many causes; naming this one costs a line."""
    with pytest.raises(ValueError, match="after end_date"):
        resolve_window(None, date(2026, 5, 1), date(2026, 1, 1), today=TODAY)


# ---------------------------------------------------------------------------
# Filtering by the window
# ---------------------------------------------------------------------------


def test_positions_are_selected_by_exit_not_entry():
    """A trade opened in December and closed in January moved the account in
    January, so a January window contains it."""
    straddler = closed(date(2026, 1, 5), entry_day=date(2025, 12, 28))
    kept, _ = filter_by_exit_window([straddler], Window(date(2026, 1, 1), date(2026, 1, 31)))
    assert kept == [straddler]


def test_both_window_bounds_are_inclusive():
    window = Window(date(2026, 1, 10), date(2026, 1, 20))
    days = [date(2026, 1, 9), date(2026, 1, 10), date(2026, 1, 20), date(2026, 1, 21)]
    kept, _ = filter_by_exit_window([closed(d) for d in days], window)
    assert [p.exit_time.date() for p in kept] == [date(2026, 1, 10), date(2026, 1, 20)]


def test_an_unbounded_window_keeps_everything_untouched():
    positions = [closed(date(1999, 1, 1)), closed(date(2026, 1, 1))]
    kept, undated = filter_by_exit_window(positions, Window())
    assert kept == positions and undated == 0


def test_a_position_with_no_exit_time_is_counted_not_swallowed():
    """positions.exit_time is NOT NULL, so this should never happen -- which is
    exactly why it is reported rather than assumed. A schema change that made
    it possible would otherwise quietly shrink every windowed figure."""
    orphan = ClosedPosition(
        realized_pnl=Decimal("10"), gross_pnl=Decimal("10"), commission=Decimal("0"),
        entry_price=Decimal("100"), quantity=Decimal("1"),
        entry_time=datetime(2026, 1, 5, 19, 0, tzinfo=timezone.utc), exit_time=None,
    )
    kept, undated = filter_by_exit_window([orphan], Window(date(2026, 1, 1), date(2026, 1, 31)))
    assert kept == [] and undated == 1


# ---------------------------------------------------------------------------
# The clamp
# ---------------------------------------------------------------------------


def test_an_ordinary_span_is_not_clamped():
    positions = [closed(date(2025, 7, 30)), closed(date(2026, 7, 30))]
    kept, truncated = clamp_to_max_span(positions)
    assert kept == positions and truncated is False


def test_one_corrupt_date_is_dropped_rather_than_stretching_the_curve():
    """The measured case: a 1970 row beside real 2026 data."""
    good, bad = closed(date(2026, 7, 1)), closed(date(1970, 1, 1))
    kept, truncated = clamp_to_max_span([bad, good])
    assert kept == [good] and truncated is True


def test_the_clamp_is_measured_from_the_last_close_not_from_today():
    """A ledger idle for years keeps its full recent history instead of the
    window walking off the end of it."""
    days = [date(2010, 1, 1), date(2010, 6, 1)]
    kept, truncated = clamp_to_max_span([closed(d) for d in days])
    assert len(kept) == 2 and truncated is False


def test_exactly_max_curve_days_apart_is_kept():
    last = date(2026, 7, 30)
    kept, truncated = clamp_to_max_span(
        [closed(last - timedelta(days=MAX_CURVE_DAYS)), closed(last)]
    )
    assert len(kept) == 2 and truncated is False


def test_clamping_is_idempotent():
    """build_dashboard clamps, then build_equity_curve clamps again. The second
    pass must not report a truncation that already happened, or the flag would
    be set on every payload."""
    once, first = clamp_to_max_span([closed(date(1970, 1, 1)), closed(date(2026, 7, 1))])
    twice, second = clamp_to_max_span(once)
    assert first is True and second is False and twice == once


# ---------------------------------------------------------------------------
# What the curve reports
# ---------------------------------------------------------------------------


def test_the_curve_stays_bounded_and_says_it_was_cut():
    curve = build_equity_curve([closed(date(1970, 1, 1)), closed(date(2026, 7, 1))])
    assert curve["summary"]["truncated"] is True
    assert curve["summary"]["max_days"] == MAX_CURVE_DAYS
    # Without the clamp this was 20,637 points.
    assert len(curve["points"]) <= MAX_CURVE_DAYS + 2


def test_an_ordinary_curve_reports_truncated_false():
    curve = build_equity_curve([closed(date(2026, 7, 1)), closed(date(2026, 7, 10))])
    assert curve["summary"]["truncated"] is False
    # 10 days inclusive, plus the zero anchor the day before the first close.
    assert len(curve["points"]) == 11


def test_a_caller_that_already_clamped_is_believed():
    """build_dashboard filters and clamps before the curve sees the data, so
    the flag has to be able to travel with it -- otherwise a truncation would
    be reported by the window block and denied by the summary beside it."""
    curve = build_equity_curve([closed(date(2026, 7, 1))], truncated=True)
    assert curve["summary"]["truncated"] is True


def test_an_empty_window_still_carries_the_flag():
    """A window with no trades in it must not lose the reason it is empty."""
    curve = build_equity_curve([], truncated=True)
    assert curve["points"] == []
    assert curve["summary"]["truncated"] is True
    assert curve["summary"]["closed_trades"] == 0


def test_a_one_year_window_is_about_365_points():
    """The headline claim of the default view: a bounded first paint."""
    last = date(2026, 7, 30)
    positions = [closed(last - timedelta(days=n * 30)) for n in range(13)]
    window = resolve_window(PRESET_1Y, today=last)
    kept, _ = filter_by_exit_window(positions, window)
    curve = build_equity_curve(kept)
    assert len(curve["points"]) <= ONE_YEAR_DAYS + 2
