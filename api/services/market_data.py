"""Market data for the long-term book, from FMP with a Finnhub fallback.

Two cadences, because the two kinds of data go stale at completely different
rates and the free tier is metered:

    prices        daily     1 call per holding
    fundamentals  monthly   3-4 calls per holding

At 14 holdings that is ~14 calls a day and ~50 a month against a 250/day
allowance -- comfortable, but only because nothing here is called during a
page load. Every figure is written to the database by a scheduled refresh and
read from there afterwards. A portfolio page that fetched live would spend
its budget in an afternoon and still render slowly.

FMP's v3 API is retired; `/stable/` is the live one and the only base URL
here. Batch quotes are a paid endpoint, so prices are fetched one symbol at a
time.

WHY THERE ARE TWO PROVIDERS. FMP's free tier gates its STATEMENT endpoints by
symbol, and the gate is narrow: of thirteen holdings in this book only six
resolve (META, GOOGL, NVDA, MSFT, AMZN, UNH), and share class matters --
GOOGL returns data where GOOG answers 402. `profile` is NOT gated and answers
for every symbol, which is what makes the split below work:

    profile      FMP, always      price, beta, shares, name, sector, country
    statements   FMP, if allowed  free cash flow, debt, cash
                 Finnhub, else    the same three, from the filed 10-K

Finnhub's `financials-reported` is SEC XBRL, so it covers domestic filers and
not foreign private issuers -- ASML (20-F, reports in EUR) and Novo Nordisk
(DKK) return nothing from it and stay on manual override. Their per-share
metrics ARE available from Finnhub's `stock/metric`, but that endpoint
reports in the FILING currency while the quote is a USD ADR price, and
deriving a valuation across the two without an explicit rate would silently
produce a wrong number that looks entirely reasonable. Declined on purpose.

SHARES OUTSTANDING IS DERIVED, not fetched. Neither `profile` nor `quote`
returns it, and the dedicated endpoint would cost a call per holding per
month for a number that `marketCap / price` already gives exactly, from a
response being fetched anyway.

This module imports no application code and holds no database session -- it
returns dataclasses and lets the caller decide what to persist. That keeps it
impossible for a market-data failure to reach the trading journal.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://financialmodelingprep.com/stable"

# Generous: FMP is occasionally slow on fundamentals, and a refresh runs in
# the background where waiting costs nothing a user can see.
TIMEOUT = httpx.Timeout(30.0, connect=10.0)


class MarketDataError(RuntimeError):
    """A fetch failed, or the key to make one is missing.

    Carries `status` so a caller can tell "you have not configured this"
    (503) from "the provider refused" (402/429) from "no such symbol" (404),
    rather than collapsing them into one opaque failure.
    """

    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Quote:
    """A price, and what it is denominated in."""

    symbol: str
    price: float
    currency: str = "USD"


@dataclass(frozen=True)
class Fundamentals:
    """Everything the DCF needs except the growth rate.

    Money figures are in MILLIONS, converted here at the boundary. FMP
    reports whole units; the valuation engine and the workbook it mirrors
    both work in millions, and mixing the two silently produces a valuation
    a million times too large.
    """

    symbol: str
    name: Optional[str] = None
    sector: Optional[str] = None
    industry: Optional[str] = None
    country: Optional[str] = None
    currency: str = "USD"

    price: Optional[float] = None
    beta: Optional[float] = None
    shares_outstanding_m: Optional[float] = None

    free_cash_flow_m: Optional[float] = None
    operating_cash_flow_m: Optional[float] = None
    capital_expenditure_m: Optional[float] = None

    # As reported: long-term + short-term + capitalised leases.
    total_debt_m: Optional[float] = None
    # The workbook's figure, and the one the valuation should use. Its input
    # is labelled "Total Debt (excl. Lease Obligations)", and FMP's totalDebt
    # includes them -- 59,291M against 46,547M for Alphabet, a 12,744M
    # difference that comes straight off the intrinsic value. Both are kept
    # so the modal can show which definition produced a number.
    total_debt_ex_leases_m: Optional[float] = None
    capital_lease_obligations_m: Optional[float] = None

    cash_and_st_m: Optional[float] = None

    # Fiscal period the statement figures came from, so a valuation can say
    # which year it valued rather than implying it is current.
    fiscal_date: Optional[str] = None

    # Which provider supplied the statement figures: "fmp", "finnhub", or
    # None when neither would. Recorded because the two are not interchangeable
    # -- FMP gives a normalised feed, Finnhub gives what the filer tagged --
    # and a valuation that disagrees with a screener should be able to say why.
    statement_source: Optional[str] = None

    # True when the debt figure could not be separated from capitalised
    # leases. Some filers tag one combined line
    # (`LongTermDebtAndCapitalLeaseObligations`), and there is no honest way to
    # split it without a second disclosure. Included rather than dropped, which
    # leaves the valuation conservative, and flagged rather than hidden.
    debt_includes_leases: bool = False


MILLION = 1_000_000

FINNHUB_URL = "https://finnhub.io/api/v1/stock/financials-reported"

# XBRL tags, in preference order, written against what these filers actually
# use rather than against the us-gaap dictionary. Verified live: Palo Alto and
# Fortinet tag capex `PaymentsToAcquireProductiveAssets` where Salesforce,
# Thermo Fisher and Copart use `PaymentsToAcquirePropertyPlantAndEquipment`,
# and Copart reports cash under the restricted-cash-inclusive concept.
_OCF_TAGS = (
    "NetCashProvidedByUsedInOperatingActivities",
    "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
)
_CAPEX_TAGS = (
    "PaymentsToAcquirePropertyPlantAndEquipment",
    "PaymentsToAcquireProductiveAssets",
    "PaymentsForCapitalImprovements",
    "PaymentsToAcquireOtherPropertyPlantAndEquipment",
)
_CASH_TAGS = (
    "CashAndCashEquivalentsAtCarryingValue",
    "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
)
_SHORT_TERM_INVESTMENT_TAGS = (
    "ShortTermInvestments",
    "OtherShortTermInvestments",
    "MarketableSecuritiesCurrent",
    "AvailableForSaleSecuritiesDebtSecuritiesCurrent",
    "DebtSecuritiesHeldToMaturityAmortizedCostAfterAllowanceForCreditLoss",
)

# Split into two buckets that cannot overlap, and only the FIRST match in each
# is taken. Summing every debt-shaped tag would double count: a filer
# reporting both `LongTermDebt` and `LongTermDebtNoncurrent` means the same
# money twice, and the resulting valuation would be wrong in the direction
# that looks prudent, which is the hardest kind of error to notice.
_DEBT_CURRENT_TAGS = (
    "DebtCurrent",
    "LongTermDebtCurrent",
    "LongTermDebtAndCapitalLeaseObligationsCurrent",
    "ConvertibleDebtCurrent",
    "ShortTermBorrowings",
    "CommercialPaper",
)
_DEBT_NONCURRENT_TAGS = (
    "LongTermDebtNoncurrent",
    "LongTermDebtAndCapitalLeaseObligations",
    "LongTermDebt",
    "ConvertibleDebtNoncurrent",
)


def _finnhub_token() -> Optional[str]:
    return (os.environ.get("FINNHUB_API") or "").strip() or None


def _concept(name: str) -> str:
    """`us-gaap_Assets` and `cprt_VehiclePoolingCosts` -> the bare tag."""
    return str(name or "").split("_", 1)[-1]


def _pick(section: list, tags: Sequence[str]) -> tuple[Optional[float], Optional[str]]:
    """First tag present in this statement section, and which one it was.

    Preference order matters more than completeness: the tags are ranked so
    the most specific reading wins, and taking only one keeps overlapping
    concepts from being added together.
    """
    by_concept: dict[str, float] = {}
    for item in section or []:
        concept = _concept(item.get("concept"))
        value = item.get("value")
        if concept and value is not None and concept not in by_concept:
            try:
                by_concept[concept] = float(value)
            except (TypeError, ValueError):
                continue

    for tag in tags:
        if tag in by_concept:
            return by_concept[tag], tag
    return None, None


def _api_key() -> str:
    """Read per call, not at import: a process started before the key was set
    must work after a restart that supplies it."""
    key = (os.environ.get("FMP_KEY") or "").strip()
    if not key:
        raise MarketDataError(
            "Market data is not configured: FMP_KEY is not set. Add it to "
            "api/.env (the file is gitignored).",
            status=503,
        )
    return key


async def _get(client: httpx.AsyncClient, path: str, **params: Any) -> Any:
    """One GET against /stable, with the provider's refusals named."""
    try:
        response = await client.get(
            f"{BASE_URL}/{path}", params={**params, "apikey": _api_key()}
        )
    except httpx.HTTPError as exc:
        raise MarketDataError(f"Could not reach Financial Modeling Prep: {exc}") from exc

    if response.status_code == 402:
        raise MarketDataError(
            f"'{path}' is not available on this FMP plan.", status=402
        )
    if response.status_code == 429:
        # The one failure worth retrying later rather than investigating.
        raise MarketDataError(
            "FMP rate limit reached. The daily allowance is spent; the next "
            "scheduled refresh will pick this up.",
            status=429,
        )
    if response.status_code >= 400:
        logger.error("FMP %s failed: %s %s", path, response.status_code,
                     response.text[:300])
        raise MarketDataError(f"FMP refused the request ({response.status_code}).")

    return response.json()


def _first(payload: Any) -> dict:
    """FMP returns a single-element list for most symbol queries."""
    if isinstance(payload, list):
        return payload[0] if payload else {}
    return payload if isinstance(payload, dict) else {}


def _num(row: dict, *names: str) -> Optional[float]:
    for name in names:
        value = row.get(name)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return None


def _millions(row: dict, *names: str) -> Optional[float]:
    raw = _num(row, *names)
    return None if raw is None else raw / MILLION


async def fetch_quote(symbol: str, client: Optional[httpx.AsyncClient] = None) -> Quote:
    """Latest price. One call -- this is the daily path."""
    owned = client is None
    client = client or httpx.AsyncClient(timeout=TIMEOUT)
    try:
        row = _first(await _get(client, "quote", symbol=symbol))
        price = _num(row, "price")
        if price is None:
            raise MarketDataError(f"FMP returned no price for {symbol}.", status=404)
        return Quote(symbol=symbol, price=price)
    finally:
        if owned:
            await client.aclose()


async def _finnhub_statements(
    client: httpx.AsyncClient, symbol: str
) -> Optional[dict]:
    """Free cash flow, debt and cash from the last annual filing.

    Returns None rather than raising when Finnhub has nothing -- a foreign
    private issuer files a 20-F that this SEC-XBRL feed does not carry, and
    that is an expected gap for those names, not a fault to abort a refresh
    over.

    Figures come back in whole units, as filed; the caller converts.
    """
    token = _finnhub_token()
    if not token:
        logger.warning("FINNHUB_API is not set; no statement fallback available.")
        return None

    try:
        response = await client.get(
            FINNHUB_URL, params={"symbol": symbol, "freq": "annual", "token": token}
        )
    except httpx.HTTPError as exc:
        logger.warning("Finnhub unreachable for %s: %s", symbol, exc)
        return None

    if response.status_code >= 400:
        logger.warning("Finnhub refused %s: %s", symbol, response.status_code)
        return None

    filings = (response.json() or {}).get("data") or []
    if not filings:
        return None

    latest = filings[0]
    report = latest.get("report") or {}
    cash_flow = report.get("cf") or []
    balance = report.get("bs") or []

    operating, _ = _pick(cash_flow, _OCF_TAGS)
    capex, _ = _pick(cash_flow, _CAPEX_TAGS)
    cash, _ = _pick(balance, _CASH_TAGS)
    investments, _ = _pick(balance, _SHORT_TERM_INVESTMENT_TAGS)

    current_debt, current_tag = _pick(balance, _DEBT_CURRENT_TAGS)
    noncurrent_debt, noncurrent_tag = _pick(balance, _DEBT_NONCURRENT_TAGS)

    # A filer that reports no debt line has no debt. Distinct from a filer
    # this feed simply does not cover, which returned None above.
    total_debt = (current_debt or 0.0) + (noncurrent_debt or 0.0)
    includes_leases = any(
        tag and "CapitalLeaseObligations" in tag
        for tag in (current_tag, noncurrent_tag)
    )

    # Capex is filed as a positive "payment". Made a magnitude either way so
    # the subtraction below cannot accidentally add it back.
    free_cash_flow = (
        operating - abs(capex) if operating is not None and capex is not None
        else None
    )

    return {
        "operating_cash_flow": operating,
        "capital_expenditure": abs(capex) if capex is not None else None,
        "free_cash_flow": free_cash_flow,
        "total_debt": total_debt,
        "debt_includes_leases": includes_leases,
        "cash_and_st": (cash or 0.0) + (investments or 0.0) if cash is not None else None,
        "fiscal_date": str(latest.get("endDate") or "")[:10] or None,
    }


async def fetch_fundamentals(
    symbol: str, client: Optional[httpx.AsyncClient] = None
) -> Fundamentals:
    """Everything the DCF needs bar growth. The monthly path.

    Three calls when FMP serves the statements, four when it does not and
    Finnhub is asked instead. `profile` is fetched first and unconditionally,
    because it is the one endpoint FMP does not gate by symbol and it carries
    price, beta and market cap for every holding in the book.

    Shares are derived from market cap and price rather than fetched: the
    dedicated endpoint costs another call per holding for a number this
    response already determines exactly.
    """
    owned = client is None
    client = client or httpx.AsyncClient(timeout=TIMEOUT)
    try:
        profile = _first(await _get(client, "profile", symbol=symbol))

        price = _num(profile, "price")
        market_cap = _num(profile, "marketCap")
        shares_m = None
        if price and market_cap and price > 0:
            shares_m = market_cap / price / MILLION

        statements: Optional[str] = None
        cash_flow: dict = {}
        balance: dict = {}
        try:
            cash_flow = _first(await _get(client, "cash-flow-statement",
                                          symbol=symbol, limit=1))
            balance = _first(await _get(client, "balance-sheet-statement",
                                        symbol=symbol, limit=1))
            statements = "fmp" if (cash_flow or balance) else None
        except MarketDataError as exc:
            # 402 is the free tier declining this SYMBOL, not this endpoint,
            # and no amount of retrying changes it. Anything else (429, a
            # network fault) is a real failure and must not be papered over
            # with a second provider's numbers.
            if exc.status != 402:
                raise
            logger.info("FMP has no statements for %s; trying Finnhub.", symbol)

        if statements == "fmp":
            total_debt_m = _millions(balance, "totalDebt")
            leases_m = _millions(balance, "capitalLeaseObligations")
            return Fundamentals(
                symbol=symbol,
                name=profile.get("companyName"),
                sector=profile.get("sector"),
                industry=profile.get("industry"),
                country=profile.get("country"),
                currency=profile.get("currency") or "USD",
                price=price,
                beta=_num(profile, "beta"),
                shares_outstanding_m=shares_m,
                free_cash_flow_m=_millions(cash_flow, "freeCashFlow"),
                operating_cash_flow_m=_millions(cash_flow, "operatingCashFlow"),
                # FMP reports capex negative (a cash outflow). Stored as a
                # magnitude so callers subtract it rather than having to know.
                capital_expenditure_m=(
                    abs(_millions(cash_flow, "capitalExpenditure") or 0.0)
                    if cash_flow.get("capitalExpenditure") is not None else None
                ),
                total_debt_m=total_debt_m,
                capital_lease_obligations_m=leases_m,
                # Falls back to the reported total when the lease line is
                # absent rather than guessing at zero: an unknown lease
                # balance should leave the valuation conservative, not
                # flatter it.
                total_debt_ex_leases_m=(
                    None if total_debt_m is None
                    else total_debt_m - (leases_m or 0.0)
                ),
                cash_and_st_m=_millions(
                    balance, "cashAndShortTermInvestments", "cashAndCashEquivalents"
                ),
                fiscal_date=cash_flow.get("date") or balance.get("date"),
                statement_source="fmp",
            )

        filed = await _finnhub_statements(client, symbol)
        if filed is None:
            # Everything the profile knew, and nothing the statements would
            # have added. The caller stores what it can and the modal asks for
            # the rest by hand -- better than an exception that would cost the
            # other twelve holdings their refresh.
            return Fundamentals(
                symbol=symbol,
                name=profile.get("companyName"),
                sector=profile.get("sector"),
                industry=profile.get("industry"),
                country=profile.get("country"),
                currency=profile.get("currency") or "USD",
                price=price,
                beta=_num(profile, "beta"),
                shares_outstanding_m=shares_m,
                statement_source=None,
            )

        def to_m(value: Optional[float]) -> Optional[float]:
            return None if value is None else value / MILLION

        return Fundamentals(
            symbol=symbol,
            name=profile.get("companyName"),
            sector=profile.get("sector"),
            industry=profile.get("industry"),
            country=profile.get("country"),
            currency=profile.get("currency") or "USD",
            price=price,
            beta=_num(profile, "beta"),
            shares_outstanding_m=shares_m,
            free_cash_flow_m=to_m(filed["free_cash_flow"]),
            operating_cash_flow_m=to_m(filed["operating_cash_flow"]),
            capital_expenditure_m=to_m(filed["capital_expenditure"]),
            total_debt_m=to_m(filed["total_debt"]),
            # Lease-only tags are never summed into the debt figure, so what
            # comes back is already the workbook's definition -- unless the
            # filer reported one combined line, which the flag records.
            total_debt_ex_leases_m=to_m(filed["total_debt"]),
            debt_includes_leases=filed["debt_includes_leases"],
            cash_and_st_m=to_m(filed["cash_and_st"]),
            fiscal_date=filed["fiscal_date"],
            statement_source="finnhub",
        )
    finally:
        if owned:
            await client.aclose()


def region_for(country: Optional[str], currency: Optional[str] = None) -> str:
    """Which risk table a holding is priced against.

    Hong Kong and mainland China carry a market risk premium of 7.4% against
    the US table's 3.0% -- more than double, so this is not cosmetic. Anything
    else falls back to the US table, which is what the workbook does for the
    European and ADR names in this portfolio.
    """
    code = (country or "").strip().upper()
    ccy = (currency or "").strip().upper()
    if code in {"HK", "CN", "CHN", "HKG"} or ccy in {"HKD", "CNY", "CNH"}:
        return "HK"
    return "US"
