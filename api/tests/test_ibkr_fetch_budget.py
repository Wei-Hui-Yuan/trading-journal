"""The Flex handshake has to answer inside the browser's patience.

One query's worst case is already ~266s -- 3 SendRequest attempts (2 x 5s
backoff, 30s HTTP timeout each) plus 5 GetStatement polls (4 x 4s backoff, same
timeout). Every second of that is legitimate; IBKR compiles the report on
demand. But IBKR_QUERY_ID takes a LIST and two queries is the documented
configuration, so the real worst case was ~535s against a 300s client timeout.

What that cost was not the wait but the reporting. The browser gave up on a run
that had not failed -- IBKR had already been asked to compile, fills may have
been staged -- and told the user "no response from the server" about an outcome
they now had no way to learn.

So the server bounds itself, and an over-run becomes an ordinary partial
result: named in `queries_failed`, rendered by the sync toast, and picked up by
the next run because ingestion is idempotent.
"""

import asyncio
import os
import xml.etree.ElementTree as ET

import pytest

os.environ.setdefault("CORS_ALLOW_ORIGINS", "https://trading-journal-test.vercel.app")

from services import ibkr_client  # noqa: E402


@pytest.fixture(autouse=True)
def no_inter_query_pause(monkeypatch):
    """The 3s spacing between queries is real but not what these tests measure."""
    monkeypatch.setattr(ibkr_client, "BETWEEN_QUERIES_SECONDS", 0.0)


def statement(query_id: str) -> ET.Element:
    return ET.fromstring(f'<FlexQueryResponse queryName="{query_id}" />')


def run(coro):
    return asyncio.run(coro)


def test_the_budget_is_below_the_client_timeout():
    """The two numbers are one contract. src/lib/api.ts allows 300s; this has
    to leave room inside it for the staging, promotion and FIFO work that runs
    after the fetch."""
    assert ibkr_client.TOTAL_BUDGET_SECONDS < 300, "must answer before the browser gives up"
    assert ibkr_client.TOTAL_BUDGET_SECONDS >= 120, "must not undercut a legitimate compile"


class TestCredentialResolution:
    """token and query_ids resolve from the environment INDEPENDENTLY.

    The regression: a caller supplying its own query_ids while leaving token
    unset -- the investment sync, reading a different account's query with
    the one shared Flex token -- used to have query_ids silently discarded.
    `if token is None or query_ids is None: both from get_credentials()` ran
    on ANY partial call, which resolved BOTH from IBKR_QUERY_ID and quietly
    substituted the trading account's queries for the one actually requested.
    Caught live: a probe asking for query 1594048 tried to fetch 1578306
    instead, with no exception and no hint anything had gone wrong -- the
    swap was silent by construction, exactly the failure mode a parallel
    pipeline exists to prevent.
    """

    def test_explicit_query_ids_survive_an_unset_token(self, monkeypatch):
        monkeypatch.setenv("IBKR_TOKEN", "env-token")
        monkeypatch.setenv("IBKR_QUERY_ID", "1111,2222")

        captured: list[str] = []

        async def spy(token, query_id):
            captured.append(query_id)
            return statement(query_id)

        monkeypatch.setattr(ibkr_client, "fetch_statement", spy)

        run(ibkr_client.fetch_statements(query_ids=["9999"]))

        assert captured == ["9999"], (
            "an explicitly supplied query_ids must be used as-is, not "
            "replaced by IBKR_QUERY_ID just because token was omitted"
        )

    def test_the_token_still_resolves_from_the_environment(self, monkeypatch):
        """The other half of the same call: an unset token must still reach
        the env var, not be left None and fail the request."""
        monkeypatch.setenv("IBKR_TOKEN", "env-token")
        monkeypatch.setenv("IBKR_QUERY_ID", "1111")

        captured: list[str] = []

        async def spy(token, query_id):
            captured.append(token)
            return statement(query_id)

        monkeypatch.setattr(ibkr_client, "fetch_statement", spy)

        run(ibkr_client.fetch_statements(query_ids=["9999"]))

        assert captured == ["env-token"]

    def test_both_omitted_still_resolves_both_from_the_environment(self, monkeypatch):
        """The trading ingest's actual call shape --
        `ibkr_client.fetch_statements()`, no arguments -- must be unchanged."""
        monkeypatch.setenv("IBKR_TOKEN", "env-token")
        monkeypatch.setenv("IBKR_QUERY_ID", "1111,2222")

        captured: list[tuple[str, str]] = []

        async def spy(token, query_id):
            captured.append((token, query_id))
            return statement(query_id)

        monkeypatch.setattr(ibkr_client, "fetch_statement", spy)

        run(ibkr_client.fetch_statements())

        assert captured == [("env-token", "1111"), ("env-token", "2222")]

    def test_an_unset_token_with_no_env_fallback_names_the_variable(self, monkeypatch):
        monkeypatch.delenv("IBKR_TOKEN", raising=False)
        monkeypatch.delenv("IBKR_FLEX_TOKEN", raising=False)

        with pytest.raises(ibkr_client.IBKRError) as caught:
            run(ibkr_client.fetch_statements(query_ids=["9999"]))
        assert "IBKR_FLEX_TOKEN" in str(caught.value) or "IBKR_TOKEN" in str(caught.value)

    def test_an_unset_query_ids_with_no_env_fallback_names_the_variable(self, monkeypatch):
        monkeypatch.setenv("IBKR_TOKEN", "env-token")
        monkeypatch.delenv("IBKR_QUERY_ID", raising=False)

        with pytest.raises(ibkr_client.IBKRError) as caught:
            run(ibkr_client.fetch_statements(token="explicit-token"))
        assert "IBKR_QUERY_ID" in str(caught.value)


def test_a_query_that_never_returns_is_reported_not_waited_on(monkeypatch):
    """The regression. A hung query used to run to its own ~266s ceiling with
    nothing bounding the total; now it is cut off and named."""
    monkeypatch.setattr(ibkr_client, "TOTAL_BUDGET_SECONDS", 0.2)

    async def never_returns(token, query_id):
        await asyncio.sleep(60)

    monkeypatch.setattr(ibkr_client, "fetch_statement", never_returns)

    with pytest.raises(ibkr_client.IBKRError) as caught:
        run(ibkr_client.fetch_statements("tok", ["slow"]))

    # Nothing came back at all, so this is a failed sync rather than a partial
    # one -- and retryable, because a still-compiling report clears on its own.
    assert caught.value.retryable
    assert "slow" in str(caught.value)


def test_a_slow_query_does_not_cost_the_one_behind_it(monkeypatch):
    """Partial results are the whole point. The Activity query carries history
    and the TCF query carries today; losing both because one hung would drop a
    date range the user cannot see is missing."""
    monkeypatch.setattr(ibkr_client, "TOTAL_BUDGET_SECONDS", 0.4)

    async def one_hangs(token, query_id):
        if query_id == "slow":
            await asyncio.sleep(60)
        return statement(query_id)

    monkeypatch.setattr(ibkr_client, "fetch_statement", one_hangs)

    statements, failures = run(ibkr_client.fetch_statements("tok", ["slow", "quick"]))

    assert [qid for qid, _ in statements] == ["quick"], "the healthy query still lands"
    assert len(failures) == 1
    assert "slow" in failures[0]


def test_the_share_is_split_by_what_is_left_to_run(monkeypatch):
    """A fair share, not a fixed slice. The common case is a query returning in
    seconds, and its unused budget has to reach the next one -- otherwise two
    queries would each be capped at half even when the first cost nothing."""
    monkeypatch.setattr(ibkr_client, "TOTAL_BUDGET_SECONDS", 1.0)
    seen: list[str] = []

    async def instant_then_slow(token, query_id):
        seen.append(query_id)
        if query_id == "second":
            # Longer than an even split of the budget would allow, shorter than
            # the whole of it -- so this only succeeds if the first query's
            # unused share was handed on.
            await asyncio.sleep(0.7)
        return statement(query_id)

    monkeypatch.setattr(ibkr_client, "fetch_statement", instant_then_slow)

    statements, failures = run(
        ibkr_client.fetch_statements("tok", ["first", "second"])
    )

    assert seen == ["first", "second"]
    assert failures == [], "the second query inherited the first's unused time"
    assert len(statements) == 2


def test_an_ordinary_run_is_untouched(monkeypatch):
    """The budget is a ceiling, not a schedule. Nothing about a healthy sync
    changes."""
    async def prompt(token, query_id):
        return statement(query_id)

    monkeypatch.setattr(ibkr_client, "fetch_statement", prompt)

    statements, failures = run(ibkr_client.fetch_statements("tok", ["a", "b"]))

    assert [qid for qid, _ in statements] == ["a", "b"]
    assert failures == []


def test_a_refusal_is_still_reported_as_a_refusal(monkeypatch):
    """A bad token and an exhausted budget must not read the same. One clears
    on its own; the other never will, and retrying it only burns requests."""
    async def rejected(token, query_id):
        raise ibkr_client.IBKRError(
            "IBKR rejected the statement request (code 1015): unknown query",
            retryable=False,
        )

    monkeypatch.setattr(ibkr_client, "fetch_statement", rejected)

    with pytest.raises(ibkr_client.IBKRError) as caught:
        run(ibkr_client.fetch_statements("tok", ["bad"]))

    assert "unknown query" in str(caught.value)
    assert not ibkr_client.is_transient_failure(str(caught.value)), "not a throttle"
