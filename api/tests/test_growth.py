"""The stage-1 growth rate, and the difference between the two sources.

Nothing here touches the network. The Finviz markup below is cut verbatim
from a live quote page rather than written by hand -- a fabricated fixture
would let the parser pass these tests and still return None against the real
site, which is exactly the failure that broke the `finvizfinance` package.

The distinction these tests exist to protect: Finviz's `EPS next 5Y` is a
FORWARD analyst estimate and Finnhub's `epsGrowth5Y` is a TRAILING historical
CAGR. They are not interchangeable, the model is far more sensitive to this
input than to any other, and a silent swap between them would move an
intrinsic value by tens of percent with nothing on screen to say why.
"""

import asyncio
import os

import httpx
import pytest

os.environ.setdefault("CORS_ALLOW_ORIGINS", "https://trading-journal-test.vercel.app")

from services import growth as g  # noqa: E402

# Verbatim from finviz.com/quote.ashx?t=AAPL. The neighbouring rows are kept
# so the bounded-gap pattern is exercised against real adjacency, including
# the `<small>`-wrapped row that follows.
FINVIZ_HTML = (
    'napshot-td2 max-xl:w-[8%] xl:w-full xl:max-w-0" align="left" style="">'
    '<div class="snapshot-td-content"><b>8.74%</b></div></td>\n</tr>\n'
    '<tr class="table-dark-row">\n'
    '<td class="snapshot-td2 cursor-pointer max-xl:w-[4%] xl:w-px" align="left" '
    'data-boxover-html="Long term annual growth estimate (5 years)">'
    '<div class="snapshot-td-label">EPS next 5Y</div></td>'
    '<td class="snapshot-td2 max-xl:w-[8%] xl:w-full xl:max-w-0" align="left" '
    'style=""><div class="snapshot-td-content"><b>12.69%</b></div></td>\n</tr>\n'
    '<tr class="table-dark-row">\n'
    '<td class="snapshot-td2 cursor-pointer max-xl:w-[4%] xl:w-px" align="left" '
    'data-boxover-html="Annual EPS growth past 3 and 5 years">'
    '<div class="snapshot-td-label">EPS past 3/5Y</div></td>'
    '<td class="snapshot-td2 max-xl:w-[8%] xl:w-full xl:max-w-0" align="left" '
    'style=""><div class="snapshot-td-content"><b>'
    '<small class="text-2xs">6.89% 17.91%</small></b></div></td>'
)

# Verbatim from finviz.com/quote.ashx?t=NVDA. Finviz colour-codes some
# figures, which nests the value one tag deeper. A parser written against the
# Alphabet page alone reads that page perfectly and silently returns nothing
# here -- which is how Nvidia, ASML and Novo Nordisk all fell through to the
# trailing fallback on the first live run.
FINVIZ_HTML_COLOURED = (
    '<td class="snapshot-td2 cursor-pointer max-xl:w-[4%] xl:w-px" align="left" '
    'data-boxover-html="Long term annual growth estimate (5 years)">'
    '<div class="snapshot-td-label">EPS next 5Y</div></td>'
    '<td class="snapshot-td2 max-xl:w-[8%] xl:w-full xl:max-w-0" align="left" '
    'style=""><div class="snapshot-td-content"><b>'
    '<span class="color-text is-positive">47.55%</span></b></div></td>\n</tr>'
)

# Novo Nordisk, same page structure, negative and colour-coded. Its forward
# consensus is roughly flat while its trailing five-year CAGR is +20.66% --
# the single clearest demonstration in this portfolio that the two numbers
# are not substitutes for one another.
FINVIZ_HTML_NEGATIVE = FINVIZ_HTML_COLOURED.replace(
    'is-positive">47.55%', 'is-negative">-0.86%'
)

FINNHUB_JSON = {
    "symbol": "AAPL",
    "metric": {"epsGrowth5Y": 17.91, "epsGrowth3Y": 6.89, "beta": 1.0963676},
}


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("FINNHUB_API", "test-token")


def transport(
    finviz_html: str | None = FINVIZ_HTML,
    finviz_status: int = 200,
    finnhub: dict | None = None,
    finnhub_status: int = 200,
    counts: dict | None = None,
) -> httpx.MockTransport:
    """Routes by host, and optionally records how often each was asked."""
    finnhub = FINNHUB_JSON if finnhub is None else finnhub

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if counts is not None:
            counts[host] = counts.get(host, 0) + 1
        if "finviz" in host:
            if finviz_status != 200:
                return httpx.Response(finviz_status, text="rate limited")
            return httpx.Response(200, text=finviz_html or "")
        if finnhub_status != 200:
            return httpx.Response(finnhub_status, text="refused")
        return httpx.Response(200, json=finnhub)

    return httpx.MockTransport(handler)


# The suite has no pytest-asyncio and stays dependency-free by choice (see
# test_strategy_deletion.py), so coroutines are driven through asyncio.run.
def one(symbol: str = "AAPL", **kw):
    async def scenario():
        async with httpx.AsyncClient(transport=transport(**kw)) as client:
            return await g.fetch_growth(symbol, client=client)

    return asyncio.run(scenario())


def many(symbols, **kw):
    async def scenario():
        async with httpx.AsyncClient(transport=transport(**kw)) as client:
            return await g.fetch_growth_many(symbols, client=client, throttle=0)

    return asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Reading the page
# ---------------------------------------------------------------------------


def test_the_forward_estimate_is_found_in_real_markup():
    assert g._snapshot_field(FINVIZ_HTML, "EPS next 5Y") == "12.69%"


def test_a_colour_coded_value_is_read_through_its_wrapper():
    """Finviz nests colour-coded figures one tag deeper. Reading the cell's
    text rather than assuming a fixed tag shape is what makes this work for
    both layouts."""
    assert g._snapshot_field(FINVIZ_HTML_COLOURED, "EPS next 5Y") == "47.55%"


def test_a_negative_colour_coded_value_keeps_its_sign():
    """Dropping the minus would turn a shrinking business into a growing one."""
    assert g._snapshot_field(FINVIZ_HTML_NEGATIVE, "EPS next 5Y") == "-0.86%"
    assert g._percent("-0.86%") == pytest.approx(-0.0086)


def test_novo_nordisk_reads_forward_not_trailing():
    """The regression this parser bug caused, end to end: a live consensus of
    roughly flat, against a trailing +20.66% that Finnhub would have supplied
    had the page gone unread."""
    result = one(finviz_html=FINVIZ_HTML_NEGATIVE,
                 finnhub={"metric": {"epsGrowth5Y": 20.66}})
    assert result.source == "finviz"
    assert result.growth_1_5 == pytest.approx(-0.0086)


def test_a_percentage_becomes_a_decimal_fraction():
    """The engine takes 0.1269, not 12.69. A hundredfold error here values a
    company in the quadrillions and still renders as a number."""
    assert g._percent("12.69%") == pytest.approx(0.1269)
    assert g._percent("-3.40%") == pytest.approx(-0.034)


def test_finviz_writes_a_dash_for_absent_which_is_not_zero():
    """Zero growth is a claim about the business. A dash is the absence of
    one, and must fall through to the next source."""
    assert g._percent("-") is None
    assert g._percent("") is None
    assert g._percent(None) is None


def test_an_unknown_label_yields_nothing_rather_than_the_next_row():
    """The bounded gap keeps a missing label from matching whatever content
    happens to follow it."""
    assert g._snapshot_field(FINVIZ_HTML, "Forward P/E") is None


# ---------------------------------------------------------------------------
# Which source answered
# ---------------------------------------------------------------------------


def test_the_forward_estimate_wins_when_both_sources_have_one():
    """Finnhub is reachable and reports 17.91% trailing; the 12.69% forward
    figure is the one that should survive."""
    result = one()
    assert result.source == "finviz"
    assert result.is_forward is True
    assert result.growth_1_5 == pytest.approx(0.1269)


def test_a_symbol_finviz_does_not_cover_falls_back_to_trailing():
    """ETFs and most funds have no analyst estimate -- expected, not a fault."""
    result = one(finviz_html="<html>no snapshot table</html>")
    assert result.source == "finnhub"
    assert result.is_forward is False
    assert result.growth_1_5 == pytest.approx(0.1791)


def test_the_five_year_trailing_figure_is_preferred_to_the_three():
    """Five years matches the stage-1 window the engine projects."""
    result = one(finviz_html="", finnhub={"metric": {"epsGrowth3Y": 6.89,
                                                     "epsGrowth5Y": 17.91}})
    assert result.growth_1_5 == pytest.approx(0.1791)


def test_the_three_year_is_used_only_when_the_five_is_missing():
    result = one(finviz_html="", finnhub={"metric": {"epsGrowth3Y": 6.89}})
    assert result.growth_1_5 == pytest.approx(0.0689)


def test_no_coverage_anywhere_returns_none_rather_than_a_guess():
    """None routes the modal to ask for a manual override. A default here
    would value a company on a number nobody chose."""
    assert one(finviz_html="", finnhub={"metric": {}}) is None


def test_a_missing_finnhub_token_degrades_instead_of_raising(monkeypatch):
    monkeypatch.delenv("FINNHUB_API", raising=False)
    assert one(finviz_html="", finnhub={"metric": {}}) is None


def test_a_provider_error_is_soft(monkeypatch):
    """A refusal from either source must not raise into a refresh loop --
    the run continues and the symbol is left for an override."""
    assert one(finviz_html="", finnhub_status=500) is None
    assert one(finviz_status=403, finnhub={"metric": {}}) is None


# ---------------------------------------------------------------------------
# Being blocked
# ---------------------------------------------------------------------------


def test_a_rate_limit_is_raised_as_its_own_kind_of_failure():
    async def scenario():
        async with httpx.AsyncClient(
            transport=transport(finviz_status=429)
        ) as client:
            await g._finviz_growth("AAPL", client)

    with pytest.raises(g.FinvizBlocked) as caught:
        asyncio.run(scenario())
    assert caught.value.status == 429


def test_one_block_stops_the_batch_asking_finviz_again():
    """Blocked is about the IP, not the symbol. Retrying twelve more times
    spends twelve throttled requests to learn the same thing."""
    counts: dict = {}
    results = many(["AAPL", "META", "GOOG", "NVDA"],
                   finviz_status=429, counts=counts)

    finviz_calls = sum(n for host, n in counts.items() if "finviz" in host)
    assert finviz_calls == 1, "Finviz should be asked once, then abandoned"
    assert len(results) == 4
    assert all(r.source == "finnhub" for r in results.values())


def test_a_blocked_batch_still_returns_every_symbol():
    """Degraded, not partial. A refresh that silently dropped holdings would
    leave them valued on last month's inputs with nothing to say so."""
    results = many(["AAPL", "META"], finviz_status=429)
    assert set(results) == {"AAPL", "META"}
    assert all(r is not None for r in results.values())


# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------


def test_a_real_forward_consensus_survives_the_clamp():
    """Nvidia's actual 47.55%. The ceiling exists to catch bad data, not to
    overrule the analysts -- clamping this would substitute the model's
    opinion for the estimate it was asked to use."""
    result = one(finviz_html=FINVIZ_HTML_COLOURED)
    assert result.growth_1_5 == pytest.approx(0.4755)
    assert result.clamped is False


def test_an_absurd_forward_estimate_is_still_bounded_and_says_so():
    result = one(finviz_html=FINVIZ_HTML.replace("12.69%", "180.00%"))
    assert result.growth_1_5 == pytest.approx(g.FORWARD_CEILING)
    assert result.reported == pytest.approx(1.80)
    assert result.clamped is True


def test_a_trailing_boom_is_clamped_harder_than_a_forecast():
    """Nvidia's trailing five-year CAGR is 95.3% -- a boom that already
    happened. Projecting it forward another five years counts it twice, so
    the fallback path is bounded more tightly than the forward one."""
    result = one(finviz_html="", finnhub={"metric": {"epsGrowth5Y": 95.3}})
    assert result.source == "finnhub"
    assert result.growth_1_5 == pytest.approx(g.TRAILING_CEILING)
    assert result.reported == pytest.approx(0.953)
    assert result.clamped is True


def test_the_two_ceilings_are_genuinely_different():
    """The same figure is admissible as a forecast and not as history."""
    assert g.TRAILING_CEILING < g.FORWARD_CEILING
    forward = one(finviz_html=FINVIZ_HTML.replace("12.69%", "40.00%"))
    trailing = one(finviz_html="", finnhub={"metric": {"epsGrowth5Y": 40.0}})
    assert forward.clamped is False
    assert trailing.clamped is True


def test_a_collapsing_estimate_is_floored_and_says_so():
    result = one(finviz_html=FINVIZ_HTML.replace("12.69%", "-40.00%"))
    assert result.growth_1_5 == pytest.approx(g.GROWTH_FLOOR)
    assert result.reported == pytest.approx(-0.40)
    assert result.clamped is True


def test_an_ordinary_estimate_passes_through_untouched():
    result = one()
    assert result.clamped is False
    assert result.growth_1_5 == result.reported


# ---------------------------------------------------------------------------
# The guardrail
# ---------------------------------------------------------------------------


def test_the_module_imports_no_application_code():
    """A scrape of a third-party site must not be able to reach the trading
    journal. This module returns dataclasses and holds no session."""
    import inspect

    source = inspect.getsource(g)
    for forbidden in ("import main", "from main", "matching_engine",
                      "AsyncSession", "sqlalchemy"):
        assert forbidden not in source, f"growth.py must not reference {forbidden}"


def test_the_scraper_adds_no_deployment_dependency():
    """Parsed by pattern on purpose: the API deploys from requirements.txt,
    and one scraper is not worth a parser dependency in production."""
    import inspect

    source = inspect.getsource(g)
    for forbidden in ("bs4", "BeautifulSoup", "lxml", "finvizfinance"):
        assert f"import {forbidden}" not in source
