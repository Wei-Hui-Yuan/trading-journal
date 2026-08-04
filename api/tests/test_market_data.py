"""Fetching the DCF's inputs, and the unit traps between provider and model.

Every test here runs against a mocked transport. The suite must not depend on
a network, an API key, or a metered allowance -- the free tier is 250 calls a
day and a test run that spent them would break the thing it is testing.

Three conversions sit between what FMP returns and what the valuation engine
expects, and each one is silent when wrong:

  * FMP reports whole units; the engine works in MILLIONS, like the workbook.
    Getting this wrong values a company a million times too high and the
    output still looks like a number.
  * FMP reports capital expenditure NEGATIVE, as a cash outflow. Subtracting
    it again adds it back.
  * FMP's `totalDebt` INCLUDES capitalised leases; the workbook's input is
    labelled "excl. Lease Obligations". For Alphabet that is 59,291M against
    46,547M, and the difference comes straight off the intrinsic value.
"""

import asyncio
import os

import httpx
import pytest

os.environ.setdefault("CORS_ALLOW_ORIGINS", "https://trading-journal-test.vercel.app")

from services import market_data as md  # noqa: E402

# Alphabet's real figures, as FMP returned them, trimmed to what is read.
PROFILE = {
    "symbol": "GOOGL", "price": 373.51, "marketCap": 4_520_259_586_434,
    "beta": 1.247, "companyName": "Alphabet Inc.", "currency": "USD",
    "sector": "Communication Services",
    "industry": "Internet Content & Information", "country": "US",
}
CASH_FLOW = {
    "date": "2025-12-31", "operatingCashFlow": 164_713_000_000,
    "capitalExpenditure": -91_447_000_000, "freeCashFlow": 73_266_000_000,
}
BALANCE = {
    "date": "2025-12-31", "totalDebt": 59_291_000_000,
    "capitalLeaseObligations": 12_744_000_000, "longTermDebt": 46_547_000_000,
    "cashAndShortTermInvestments": 126_843_000_000,
    "cashAndCashEquivalents": 30_708_000_000,
}


def transport(status: int = 200, bodies: dict | None = None) -> httpx.MockTransport:
    bodies = bodies or {"profile": [PROFILE], "cash-flow-statement": [CASH_FLOW],
                        "balance-sheet-statement": [BALANCE],
                        "quote": [{"symbol": "GOOGL", "price": 373.51}]}

    def handler(request: httpx.Request) -> httpx.Response:
        endpoint = request.url.path.rsplit("/", 1)[-1]
        if status != 200:
            return httpx.Response(status, text="refused")
        return httpx.Response(200, json=bodies.get(endpoint, []))

    return httpx.MockTransport(handler)


def client(status: int = 200, bodies: dict | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=transport(status, bodies))


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("FMP_KEY", "test-key")


# The suite has no pytest-asyncio and deliberately stays dependency-free (see
# test_strategy_deletion.py), so each async call is driven through
# asyncio.run rather than a marker.
def fundamentals(**kw):
    async def scenario():
        async with client(**kw) as c:
            return await md.fetch_fundamentals("GOOGL", client=c)

    return asyncio.run(scenario())


def quote(bodies):
    async def scenario():
        async with client(bodies=bodies) as c:
            return await md.fetch_quote("GOOGL", client=c)

    return asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------


def test_money_arrives_in_millions():
    """The engine and the workbook both work in millions. A raw pass-through
    would value Alphabet at roughly $73 trillion per share."""
    f = fundamentals()
    assert f.free_cash_flow_m == pytest.approx(73_266.0)
    assert f.operating_cash_flow_m == pytest.approx(164_713.0)
    assert f.cash_and_st_m == pytest.approx(126_843.0)


def test_capital_expenditure_is_a_magnitude_not_a_negative():
    """FMP reports it as an outflow. Callers subtract it, so handing over the
    negative would add it back and inflate free cash flow by twice capex."""
    f = fundamentals()
    assert f.capital_expenditure_m == pytest.approx(91_447.0)
    assert f.capital_expenditure_m > 0


# ---------------------------------------------------------------------------
# Debt, and the lease definition
# ---------------------------------------------------------------------------


def test_debt_excluding_leases_matches_the_workbook_definition():
    f = fundamentals()
    assert f.total_debt_m == pytest.approx(59_291.0)
    assert f.capital_lease_obligations_m == pytest.approx(12_744.0)
    assert f.total_debt_ex_leases_m == pytest.approx(46_547.0)


def test_both_debt_figures_are_kept():
    """So the modal can say which definition produced a valuation, rather
    than leaving a reader to wonder why it disagrees with a screener."""
    f = fundamentals()
    assert f.total_debt_m != f.total_debt_ex_leases_m


def test_a_missing_lease_line_leaves_debt_untouched():
    """Absent, not zero. A company that reports no lease line should stay at
    its full reported debt -- assuming zero would flatter the valuation on
    exactly the businesses where leases matter most."""
    balance = {k: v for k, v in BALANCE.items() if k != "capitalLeaseObligations"}
    f = fundamentals(bodies={
        "profile": [PROFILE], "cash-flow-statement": [CASH_FLOW],
        "balance-sheet-statement": [balance],
    })
    assert f.capital_lease_obligations_m is None
    assert f.total_debt_ex_leases_m == pytest.approx(59_291.0)


# ---------------------------------------------------------------------------
# Shares
# ---------------------------------------------------------------------------


def test_shares_are_derived_from_market_cap_and_price():
    """Neither profile nor quote returns share count, and the dedicated
    endpoint would cost a call per holding per month for a figure these two
    already determine exactly."""
    f = fundamentals()
    assert f.shares_outstanding_m == pytest.approx(4_520_259_586_434 / 373.51 / 1e6)
    assert f.shares_outstanding_m == pytest.approx(12_102.0, rel=1e-3)


def test_shares_are_none_rather_than_infinite_at_zero_price():
    profile = {**PROFILE, "price": 0}
    f = fundamentals(bodies={
        "profile": [profile], "cash-flow-statement": [CASH_FLOW],
        "balance-sheet-statement": [BALANCE],
    })
    assert f.shares_outstanding_m is None


# ---------------------------------------------------------------------------
# Region routing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("country,currency,expected", [
    ("US", "USD", "US"), ("HK", "HKD", "HK"), ("CN", "CNY", "HK"),
    (None, "HKD", "HK"), ("NL", "EUR", "US"), (None, None, "US"),
])
def test_region_routes_to_the_right_risk_table(country, currency, expected):
    """Hong Kong's market risk premium is 7.4% against the US table's 3.0% --
    more than double, so this is not cosmetic. European and ADR names fall
    back to the US table, which is what the workbook does."""
    assert md.region_for(country, currency) == expected


# ---------------------------------------------------------------------------
# Failures the caller has to tell apart
# ---------------------------------------------------------------------------


def test_a_missing_key_is_a_503_naming_the_variable(monkeypatch):
    monkeypatch.delenv("FMP_KEY", raising=False)
    with pytest.raises(md.MarketDataError) as caught:
        fundamentals()
    assert caught.value.status == 503
    assert "FMP_KEY" in str(caught.value)


def test_a_paywalled_endpoint_says_so():
    """Batch quotes answer 402 on the free tier. Worth distinguishing from a
    transient fault, because retrying will never fix it."""
    with pytest.raises(md.MarketDataError) as caught:
        fundamentals(status=402)
    assert caught.value.status == 402


def test_rate_limiting_is_distinguishable_from_a_real_failure():
    """The one failure worth waiting out rather than investigating."""
    with pytest.raises(md.MarketDataError) as caught:
        fundamentals(status=429)
    assert caught.value.status == 429
    assert "rate limit" in str(caught.value).lower()


def test_a_quote_without_a_price_is_a_404_not_a_zero():
    """A zero price would render as a real quote and produce a portfolio
    worth nothing, silently."""
    with pytest.raises(md.MarketDataError) as caught:
        quote({"quote": [{"symbol": "GOOGL"}]})
    assert caught.value.status == 404


# ---------------------------------------------------------------------------
# The guardrail
# ---------------------------------------------------------------------------


def test_the_module_imports_no_application_code():
    """A market-data failure must not be able to reach the trading journal.
    This module returns dataclasses and lets the caller persist them; it holds
    no session and imports nothing from the app."""
    import inspect

    source = inspect.getsource(md)
    for forbidden in ("import main", "from main", "matching_engine",
                      "AsyncSession", "sqlalchemy"):
        assert forbidden not in source, f"market_data.py must not reference {forbidden}"


def test_only_the_stable_api_is_used():
    """FMP's v3 endpoints are retired and answer 403 for this key."""
    assert md.BASE_URL.endswith("/stable")
    assert "/api/v3" not in md.BASE_URL
