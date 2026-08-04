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


# Copart's real 10-K, as Finnhub returned it, trimmed to the tags that are
# read. Copart is the useful case: it uses the restricted-cash-inclusive cash
# concept, holds its short-term money as held-to-maturity securities, and
# carries no debt at all -- so it exercises the "absent means zero, not
# missing" path that a debt-laden filer would not.
FINNHUB_CPRT = {
    "data": [{
        "year": 2025, "form": "10-K", "endDate": "2025-07-31 00:00:00",
        "report": {
            "cf": [
                {"concept": "us-gaap_NetCashProvidedByUsedInOperatingActivities",
                 "value": 1_799_750_000},
                {"concept": "us-gaap_PaymentsToAcquirePropertyPlantAndEquipment",
                 "value": 568_990_000},
            ],
            "bs": [
                {"concept": "us-gaap_CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
                 "value": 2_780_531_000.0},
                {"concept": "us-gaap_DebtSecuritiesHeldToMaturityAmortizedCostAfterAllowanceForCreditLoss",
                 "value": 2_008_539_000.0},
                {"concept": "cprt_OperatingAndFinanceLeaseLiabilityCurrent",
                 "value": 19_869_000},
                {"concept": "cprt_OperatingAndFinanceLeaseLiabilityNoncurrent",
                 "value": 83_870_000},
            ],
        },
    }]
}

# Thermo Fisher, which reports one combined debt-and-leases line -- the case
# that cannot be split honestly and must be flagged instead.
FINNHUB_TMO = {
    "data": [{
        "year": 2025, "form": "10-K", "endDate": "2025-12-31 00:00:00",
        "report": {
            "cf": [
                {"concept": "us-gaap_NetCashProvidedByUsedInOperatingActivities",
                 "value": 7_818_000_000},
                {"concept": "us-gaap_PaymentsToAcquirePropertyPlantAndEquipment",
                 "value": 1_525_000_000},
            ],
            "bs": [
                {"concept": "us-gaap_CashAndCashEquivalentsAtCarryingValue",
                 "value": 9_852_000_000.0},
                {"concept": "us-gaap_ShortTermInvestments", "value": 253_000_000},
                {"concept": "us-gaap_DebtCurrent", "value": 3_533_000_000.0},
                {"concept": "us-gaap_LongTermDebtAndCapitalLeaseObligations",
                 "value": 35_852_000_000.0},
            ],
        },
    }]
}


def transport(
    status: int = 200,
    bodies: dict | None = None,
    statement_status: int = 200,
    finnhub: dict | None = None,
) -> httpx.MockTransport:
    """FMP by default; `statement_status=402` gates only the statements,
    which is exactly how the free tier behaves -- profile keeps answering."""
    bodies = bodies or {"profile": [PROFILE], "cash-flow-statement": [CASH_FLOW],
                        "balance-sheet-statement": [BALANCE],
                        "quote": [{"symbol": "GOOGL", "price": 373.51}]}
    gated = {"cash-flow-statement", "balance-sheet-statement"}

    def handler(request: httpx.Request) -> httpx.Response:
        if "finnhub" in request.url.host:
            if finnhub is None:
                return httpx.Response(200, json={"data": []})
            return httpx.Response(200, json=finnhub)

        endpoint = request.url.path.rsplit("/", 1)[-1]
        if status != 200:
            return httpx.Response(status, text="refused")
        if endpoint in gated and statement_status != 200:
            return httpx.Response(statement_status, text="Premium Query Parameter")
        return httpx.Response(200, json=bodies.get(endpoint, []))

    return httpx.MockTransport(handler)


def client(**kw) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=transport(**kw))


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("FMP_KEY", "test-key")
    monkeypatch.setenv("FINNHUB_API", "test-token")


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


# ---------------------------------------------------------------------------
# The Finnhub fallback
# ---------------------------------------------------------------------------


def test_fmp_statements_win_when_the_symbol_is_covered():
    """Finnhub is reachable and would answer; the normalised feed is still
    preferred, and the fallback must not fire when nothing is wrong."""
    result = fundamentals(finnhub=FINNHUB_CPRT)
    assert result.statement_source == "fmp"
    assert result.free_cash_flow_m == pytest.approx(73_266.0)


def test_a_gated_symbol_falls_through_to_the_filed_statements():
    """FMP's free tier gates statements BY SYMBOL while `profile` keeps
    answering -- so price and beta survive and only the three statement
    figures come from elsewhere."""
    result = fundamentals(statement_status=402, finnhub=FINNHUB_CPRT)
    assert result.statement_source == "finnhub"
    # Profile still supplied these.
    assert result.price == pytest.approx(373.51)
    assert result.beta == pytest.approx(1.247)
    assert result.shares_outstanding_m is not None


def test_free_cash_flow_is_operating_less_capex_in_millions():
    result = fundamentals(statement_status=402, finnhub=FINNHUB_CPRT)
    assert result.operating_cash_flow_m == pytest.approx(1_799.75)
    assert result.capital_expenditure_m == pytest.approx(568.99)
    assert result.free_cash_flow_m == pytest.approx(1_230.76)


def test_capex_filed_as_a_positive_payment_is_still_subtracted():
    """Filers tag it `PaymentsToAcquire...`, a positive outflow, where FMP
    reports it negative. Adding it would overstate free cash flow by twice
    capex -- here, by more than a billion dollars."""
    result = fundamentals(statement_status=402, finnhub=FINNHUB_CPRT)
    assert result.free_cash_flow_m < result.operating_cash_flow_m


def test_cash_includes_short_term_investments():
    """Copart holds most of its liquidity as held-to-maturity securities.
    Counting only the cash line would understate it by two billion."""
    result = fundamentals(statement_status=402, finnhub=FINNHUB_CPRT)
    assert result.cash_and_st_m == pytest.approx(4_789.07)


def test_lease_liabilities_are_not_counted_as_debt():
    """The workbook's input is labelled "excl. Lease Obligations". Copart's
    only debt-shaped lines ARE leases, so its debt is zero."""
    result = fundamentals(statement_status=402, finnhub=FINNHUB_CPRT)
    assert result.total_debt_ex_leases_m == pytest.approx(0.0)
    assert result.debt_includes_leases is False


def test_current_and_noncurrent_debt_are_added():
    result = fundamentals(statement_status=402, finnhub=FINNHUB_TMO)
    assert result.total_debt_ex_leases_m == pytest.approx(39_385.0)


def test_a_combined_debt_and_lease_line_is_flagged_rather_than_guessed():
    """Thermo Fisher tags one `LongTermDebtAndCapitalLeaseObligations` line.
    There is no honest way to split it, so it is included -- leaving the
    valuation conservative -- and the caller is told."""
    result = fundamentals(statement_status=402, finnhub=FINNHUB_TMO)
    assert result.debt_includes_leases is True


def test_overlapping_debt_tags_are_not_double_counted():
    """A filer reporting both `LongTermDebt` and `LongTermDebtNoncurrent`
    means the same money twice. Summing every debt-shaped tag would inflate
    the figure and understate the value, which is the direction that looks
    prudent and is hardest to notice."""
    doubled = {
        "data": [{
            "year": 2025, "form": "10-K", "endDate": "2025-12-31 00:00:00",
            "report": {
                "cf": [{"concept": "us-gaap_NetCashProvidedByUsedInOperatingActivities",
                        "value": 1_000_000_000},
                       {"concept": "us-gaap_PaymentsToAcquirePropertyPlantAndEquipment",
                        "value": 100_000_000}],
                "bs": [{"concept": "us-gaap_CashAndCashEquivalentsAtCarryingValue",
                        "value": 500_000_000},
                       {"concept": "us-gaap_LongTermDebtNoncurrent", "value": 2_000_000_000},
                       {"concept": "us-gaap_LongTermDebt", "value": 2_000_000_000}],
            },
        }]
    }
    result = fundamentals(statement_status=402, finnhub=doubled)
    assert result.total_debt_ex_leases_m == pytest.approx(2_000.0)


def test_a_filer_with_no_debt_line_reports_zero_not_missing():
    result = fundamentals(statement_status=402, finnhub=FINNHUB_CPRT)
    assert result.total_debt_m == pytest.approx(0.0)


def test_a_foreign_issuer_absent_from_both_still_returns_a_profile():
    """ASML and Novo Nordisk file 20-Fs that this SEC feed does not carry.
    An exception here would cost the other twelve holdings their refresh, so
    the profile is returned and the modal asks for the rest by hand."""
    result = fundamentals(statement_status=402, finnhub={"data": []})
    assert result.statement_source is None
    assert result.free_cash_flow_m is None
    assert result.price == pytest.approx(373.51)
    assert result.beta == pytest.approx(1.247)


def test_only_a_402_falls_back_and_a_rate_limit_still_raises():
    """429 means the allowance is spent, not that this symbol is gated.
    Quietly answering it with another provider's numbers would hide a real
    failure and make the run look complete when it was not."""
    with pytest.raises(md.MarketDataError) as caught:
        fundamentals(statement_status=429, finnhub=FINNHUB_CPRT)
    assert caught.value.status == 429


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
        quote({"profile": [{"symbol": "GOOGL"}]})
    assert caught.value.status == 404


def test_the_daily_price_comes_from_the_endpoint_that_is_not_gated():
    """`quote` is gated by the same narrow symbol list as the statements --
    it refused nine of fourteen holdings, so the daily refresh updated six
    prices and failed the rest. `profile` is not gated, carries the same
    price, and costs the same one call."""
    result = quote({"profile": [{"symbol": "GOOGL", "price": 373.51,
                                 "currency": "USD"}]})
    assert result.price == pytest.approx(373.51)


def test_a_gated_symbol_still_gets_a_price():
    """The regression this replaced: with `quote`, every symbol FMP gates
    returned 402 and kept its stale price with nothing on screen to say so."""
    def handler(request: httpx.Request) -> httpx.Response:
        endpoint = request.url.path.rsplit("/", 1)[-1]
        if endpoint == "quote":
            return httpx.Response(402, text="Premium Query Parameter")
        return httpx.Response(200, json=[{"symbol": "CPRT", "price": 29.28}])

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await md.fetch_quote("CPRT", client=c)

    assert asyncio.run(scenario()).price == pytest.approx(29.28)


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
