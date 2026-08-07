"""The conditional-request flow through the real HTTP stack.

test_conditional_requests.py covers the pure helpers. This covers the thing
they exist for, and the only claim that actually matters: that a client holding
a current copy gets a 304 WITHOUT the payload being rebuilt.

A 304 that still ran the query would save bandwidth and nothing else. The
dashboard's cost is not its size, it is the full scan of positions and realised
legs behind it, so the assertion worth making is that the builder was never
called -- not merely that the body came back empty.

No database: the session is stubbed to answer the one version lookup, and the
analytics builders are monkeypatched so a call to them is observable.
"""

import pytest
from fastapi.testclient import TestClient

import main
from auth import verify_clerk_token


VERSION = 42


class _StubSession:
    """Answers the version lookup and nothing else.

    Any other query is a bug in the endpoint's ordering -- the version read is
    supposed to be the only database access before the 304 decision.
    """

    async def scalar(self, _stmt):
        return VERSION

    async def execute(self, _stmt):  # pragma: no cover - reached only on regression
        raise AssertionError(
            "the endpoint queried the database before deciding on a 304"
        )

    async def rollback(self):
        return None


@pytest.fixture
def client(monkeypatch):
    """A TestClient with auth and the session dependency stubbed out."""
    calls = {"dashboard": 0, "advanced": 0}

    async def _fake_dashboard(session, window):
        calls["dashboard"] += 1
        return {"summary": {"net_pnl": 1.0}, "points": []}

    async def _fake_advanced(session):
        calls["advanced"] += 1
        return {"expectancy": 0.5}

    import services.analytics as analytics

    monkeypatch.setattr(analytics, "build_dashboard", _fake_dashboard)
    monkeypatch.setattr(analytics, "build_advanced_analytics", _fake_advanced)

    async def _session():
        yield _StubSession()

    main.app.dependency_overrides[main.get_session] = _session
    main.app.dependency_overrides[verify_clerk_token] = lambda: None
    try:
        with TestClient(main.app) as test_client:
            yield test_client, calls
    finally:
        main.app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# The dashboard
# ---------------------------------------------------------------------------


def test_first_request_is_a_200_carrying_a_validator(client):
    test_client, calls = client
    response = test_client.get("/api/analytics/dashboard?preset=1Y")

    assert response.status_code == 200
    assert response.headers["ETag"]
    assert response.headers["Cache-Control"] == main.CONDITIONAL_CACHE_CONTROL
    assert calls["dashboard"] == 1


def test_a_matching_validator_returns_304_without_rebuilding(client):
    """The point of the whole exercise. Not just an empty body -- an untouched
    database and an unexecuted builder."""
    test_client, calls = client
    first = test_client.get("/api/analytics/dashboard?preset=1Y")
    etag = first.headers["ETag"]
    assert calls["dashboard"] == 1

    second = test_client.get(
        "/api/analytics/dashboard?preset=1Y", headers={"If-None-Match": etag}
    )

    assert second.status_code == 304
    assert second.content == b""
    # Unchanged from the first request: the payload was never rebuilt.
    assert calls["dashboard"] == 1


def test_the_304_repeats_the_validator(client):
    """A 304 replaces the stored headers on the cached entry. Dropping the
    ETag would leave the browser's copy with no validator, so the next request
    could not be conditional and the saving would happen exactly once."""
    test_client, _ = client
    etag = test_client.get("/api/analytics/dashboard?preset=1Y").headers["ETag"]

    second = test_client.get(
        "/api/analytics/dashboard?preset=1Y", headers={"If-None-Match": etag}
    )

    assert second.headers["ETag"] == etag
    assert second.headers["Cache-Control"] == main.CONDITIONAL_CACHE_CONTROL


def test_a_stale_validator_rebuilds(client):
    test_client, calls = client
    test_client.get("/api/analytics/dashboard?preset=1Y")

    response = test_client.get(
        "/api/analytics/dashboard?preset=1Y",
        headers={"If-None-Match": '"a-tag-from-before-the-last-write"'},
    )

    assert response.status_code == 200
    assert calls["dashboard"] == 2


def test_a_validator_from_a_different_window_does_not_match(client):
    """The tag for a 1Y dashboard must not satisfy a request for ALL, or one
    window's numbers get served under the other's heading."""
    test_client, calls = client
    one_year = test_client.get("/api/analytics/dashboard?preset=1Y").headers["ETag"]

    response = test_client.get(
        "/api/analytics/dashboard?preset=ALL", headers={"If-None-Match": one_year}
    )

    assert response.status_code == 200
    assert calls["dashboard"] == 2


# ---------------------------------------------------------------------------
# Advanced analytics
# ---------------------------------------------------------------------------


def test_advanced_analytics_is_conditional_too(client):
    test_client, calls = client
    first = test_client.get("/api/analytics/advanced")
    assert first.status_code == 200
    assert calls["advanced"] == 1

    second = test_client.get(
        "/api/analytics/advanced", headers={"If-None-Match": first.headers["ETag"]}
    )

    assert second.status_code == 304
    assert calls["advanced"] == 1


def test_the_two_analytics_endpoints_do_not_share_a_validator(client):
    """Same version, different payloads. A shared tag would let one answer a
    request for the other."""
    test_client, _ = client
    dashboard = test_client.get("/api/analytics/dashboard?preset=1Y").headers["ETag"]
    advanced = test_client.get("/api/analytics/advanced").headers["ETag"]
    assert dashboard != advanced


# ---------------------------------------------------------------------------
# Degrading when migration 030 has not been applied
# ---------------------------------------------------------------------------


def test_no_version_means_a_plain_200_with_no_validator(monkeypatch):
    """Between deploying this code and applying migration 030 the counter does
    not exist. The endpoint must answer normally and simply not advertise a
    cache -- an untagged 200, never a 500."""

    async def _fake_dashboard(session, window):
        return {"summary": {}, "points": []}

    import services.analytics as analytics

    monkeypatch.setattr(analytics, "build_dashboard", _fake_dashboard)

    async def _no_version(_session):
        return None

    monkeypatch.setattr(main, "_journal_version", _no_version)

    class _Session:
        async def rollback(self):
            return None

    async def _session():
        yield _Session()

    main.app.dependency_overrides[main.get_session] = _session
    main.app.dependency_overrides[verify_clerk_token] = lambda: None
    try:
        with TestClient(main.app) as test_client:
            response = test_client.get("/api/analytics/dashboard?preset=1Y")
    finally:
        main.app.dependency_overrides.clear()

    assert response.status_code == 200
    assert "ETag" not in response.headers
    assert "Cache-Control" not in response.headers


def test_an_untagged_response_ignores_a_stale_validator(monkeypatch):
    """A client that kept a tag from before the counter went missing must get
    a full payload, not a 304 matched against nothing."""

    async def _fake_dashboard(session, window):
        return {"summary": {}, "points": []}

    import services.analytics as analytics

    monkeypatch.setattr(analytics, "build_dashboard", _fake_dashboard)

    async def _no_version(_session):
        return None

    monkeypatch.setattr(main, "_journal_version", _no_version)

    class _Session:
        async def rollback(self):
            return None

    async def _session():
        yield _Session()

    main.app.dependency_overrides[main.get_session] = _session
    main.app.dependency_overrides[verify_clerk_token] = lambda: None
    try:
        with TestClient(main.app) as test_client:
            response = test_client.get(
                "/api/analytics/dashboard?preset=1Y",
                headers={"If-None-Match": '"anything"'},
            )
    finally:
        main.app.dependency_overrides.clear()

    assert response.status_code == 200
