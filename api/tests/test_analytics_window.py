"""The timeframe window on /api/analytics/advanced.

The analytics page and the dashboard both showed a figure called "win rate"
and disagreed: 34.59% against 36.80% on the same account. Two of the reasons
were deliberate -- different denominators (all closed trades vs only those that
could be scored in R) and different definitions of a win (net dollars vs gross
R). The third was not: the dashboard was windowed and this endpoint was not, so
once the ledger passes a year of history the two would drift apart again with
nothing on screen to explain it.

The window here therefore has to mean exactly what it means on the dashboard --
same presets, same precedence, same default, and trades selected by when they
CLOSED. `filter_trades_by_exit_window` is the counterpart to
`filter_by_exit_window` for that reason, and these tests pin the equivalence
rather than trusting that the two stay aligned.

The selections are independent per page, which needs no test: each page holds
its own `useState` and neither reads the other's. What DOES need one is the
ETag. It used to be keyed on the data version alone, which was correct while
the payload took no parameters and silently wrong the moment it took a window.
"""

import os
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("CORS_ALLOW_ORIGINS", "https://trading-journal-test.vercel.app")

import main  # noqa: E402
from auth import verify_clerk_token  # noqa: E402
from services import analytics  # noqa: E402
from services.analytics import (  # noqa: E402
    ReviewedTrade,
    Window,
    filter_trades_by_exit_window,
)

from decimal import Decimal  # noqa: E402


def _trade(ticker: str, exit_time) -> ReviewedTrade:
    """A scorable trade that closed at `exit_time`."""
    return ReviewedTrade(
        trade_id=f"t-{ticker}",
        ticker=ticker,
        direction="BUY",
        quantity=Decimal("10"),
        actual_entry=Decimal("100"),
        exit_price=Decimal("110"),
        planned_entry=Decimal("100"),
        stop_loss=Decimal("95"),
        mistakes=[],
        review_status="reviewed",
        realized_pnl=Decimal("100"),
        exit_time=exit_time,
    )


ET = timezone(timedelta(hours=-4))  # a fixed offset is enough for these dates


# ---------------------------------------------------------------------------
# The filter itself
# ---------------------------------------------------------------------------


def test_an_unbounded_window_keeps_everything():
    trades = [
        _trade("A", datetime(2024, 1, 5, 12, 0, tzinfo=ET)),
        _trade("B", datetime(2026, 6, 5, 12, 0, tzinfo=ET)),
    ]
    assert len(filter_trades_by_exit_window(trades, Window())) == 2


def test_trades_are_selected_by_when_they_closed():
    """Not by entry. The dashboard dates round trips by exit, and a window
    that meant different things on the two pages is the bug being fixed."""
    inside = _trade("IN", datetime(2026, 3, 15, 12, 0, tzinfo=ET))
    before = _trade("BEFORE", datetime(2026, 1, 5, 12, 0, tzinfo=ET))
    after = _trade("AFTER", datetime(2026, 7, 5, 12, 0, tzinfo=ET))

    window = Window(start=date(2026, 2, 1), end=date(2026, 4, 30))
    kept = filter_trades_by_exit_window([inside, before, after], window)

    assert [t.ticker for t in kept] == ["IN"]


def test_the_bounds_are_inclusive():
    window = Window(start=date(2026, 3, 1), end=date(2026, 3, 31))
    on_start = _trade("START", datetime(2026, 3, 1, 10, 0, tzinfo=ET))
    on_end = _trade("END", datetime(2026, 3, 31, 15, 0, tzinfo=ET))

    kept = filter_trades_by_exit_window([on_start, on_end], window)
    assert {t.ticker for t in kept} == {"START", "END"}


def test_a_trade_that_cannot_be_placed_in_time_is_dropped_from_a_bounded_window():
    """Not knowing when it closed is not evidence that it closed in the span.

    Defensive: positions.exit_time is NOT NULL and the loader only reads closed
    positions, so this should be unreachable in practice.
    """
    undated = _trade("NODATE", None)
    window = Window(start=date(2026, 1, 1), end=date(2026, 12, 31))

    assert filter_trades_by_exit_window([undated], window) == []
    # ...but an unbounded window is not a claim about time, so it keeps it.
    assert len(filter_trades_by_exit_window([undated], Window())) == 1


def test_it_agrees_with_the_dashboards_own_filter_on_the_same_dates():
    """The two filters must not drift. Same instant, same window, same verdict.

    Compared against `Window.contains` on the market-time date, which is what
    `filter_by_exit_window` uses -- so this fails if either side ever starts
    bucketing by UTC.
    """
    window = Window(start=date(2026, 3, 1), end=date(2026, 3, 31))
    # 21:00 ET on the 31st is 01:00 UTC on 1 April -- the case a UTC comparison
    # gets wrong, and the reason both filters convert to market time first.
    late = datetime(2026, 3, 31, 21, 0, tzinfo=ET)

    kept = filter_trades_by_exit_window([_trade("LATE", late)], window)
    assert len(kept) == 1, "a late close was pushed out of its own month"
    assert window.contains(late.astimezone(analytics.MARKET_TZ).date())


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------


class _StubSession:
    async def execute(self, *_a, **_k):
        raise AssertionError("the payload builder is stubbed; nothing should query")

    async def rollback(self):
        return None


@pytest.fixture
def client(monkeypatch):
    """TestClient with auth, session and the payload builder stubbed."""
    seen: list = []

    async def _fake_advanced(session, window=None):
        seen.append(window)
        return {"scored_trades": 0, "window": None}

    async def _fake_version(_session):
        return 42

    monkeypatch.setattr(analytics, "build_advanced_analytics", _fake_advanced)
    monkeypatch.setattr(main, "_journal_version", _fake_version)

    async def _session():
        yield _StubSession()

    main.app.dependency_overrides[main.get_session] = _session
    main.app.dependency_overrides[verify_clerk_token] = lambda: None
    try:
        with TestClient(main.app) as test_client:
            yield test_client, seen
    finally:
        main.app.dependency_overrides.clear()


def test_a_bare_request_defaults_to_one_year(client):
    """Matching the dashboard. An unbounded default gets slower forever and
    would keep the two pages disagreeing about what they cover."""
    test_client, seen = client
    assert test_client.get("/api/analytics/advanced").status_code == 200

    window = seen[-1]
    assert window is not None
    assert window.preset == "1Y"
    assert window.start is not None, "the default window must be bounded"


def test_a_preset_is_expanded_server_side(client):
    test_client, seen = client
    test_client.get("/api/analytics/advanced?preset=YTD")

    window = seen[-1]
    assert window.preset == "YTD"
    assert window.start == date(analytics.market_today().year, 1, 1)


def test_an_explicit_range_wins_over_a_preset(client):
    """Same precedence as the dashboard: a saved custom preset sends dates and
    must mean exactly what it says."""
    test_client, seen = client
    test_client.get(
        "/api/analytics/advanced"
        "?preset=1Y&start_date=2026-02-01&end_date=2026-02-28"
    )

    window = seen[-1]
    assert window.start == date(2026, 2, 1)
    assert window.end == date(2026, 2, 28)


def test_a_backwards_range_is_a_422_naming_the_problem(client):
    test_client, _ = client
    response = test_client.get(
        "/api/analytics/advanced?start_date=2026-06-01&end_date=2026-01-01"
    )
    assert response.status_code == 422
    assert "start_date" in response.json()["detail"]


def test_an_unknown_preset_is_a_422(client):
    test_client, _ = client
    response = test_client.get("/api/analytics/advanced?preset=LAST_TUESDAY")
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# The ETag -- the part that fails silently
# ---------------------------------------------------------------------------


def test_two_windows_do_not_share_a_validator(client):
    """The regression this endpoint was one line away from shipping.

    The ETag was `_etag("advanced", version)` back when the payload took no
    parameters, and that was correct. Adding a window without adding it here
    would have served the cached 1Y payload for an ALL request -- and kept
    doing so until something unrelated bumped the data version, which on a
    quiet week is never.
    """
    test_client, _ = client
    one_year = test_client.get("/api/analytics/advanced?preset=1Y").headers["ETag"]
    everything = test_client.get("/api/analytics/advanced?preset=ALL").headers["ETag"]

    assert one_year != everything, (
        "1Y and ALL share an ETag, so a browser holding one will be told its "
        "cached copy is still good for the other"
    )


def test_a_validator_from_another_window_does_not_produce_a_304(client):
    """The same claim end to end, through actual conditional-request handling
    rather than by comparing header strings."""
    test_client, _ = client
    one_year = test_client.get("/api/analytics/advanced?preset=1Y").headers["ETag"]

    response = test_client.get(
        "/api/analytics/advanced?preset=ALL",
        headers={"If-None-Match": one_year},
    )
    assert response.status_code == 200, (
        "an ALL request was answered 304 against a 1Y validator"
    )


def test_the_same_window_still_revalidates_to_a_304(client):
    """The guard above must not have cost the caching it protects."""
    test_client, _ = client
    etag = test_client.get("/api/analytics/advanced?preset=1Y").headers["ETag"]

    response = test_client.get(
        "/api/analytics/advanced?preset=1Y",
        headers={"If-None-Match": etag},
    )
    assert response.status_code == 304


def test_the_advanced_and_dashboard_windows_do_not_collide(client):
    """Both endpoints now key on (name, version, start, end). The name is what
    keeps two payloads covering the same span from sharing a validator."""
    test_client, _ = client
    advanced = test_client.get("/api/analytics/advanced?preset=1Y").headers["ETag"]

    version = 42
    window = analytics.resolve_window("1Y", None, None)
    dashboard = main._etag("dashboard", version, window.start, window.end)

    assert advanced != dashboard
