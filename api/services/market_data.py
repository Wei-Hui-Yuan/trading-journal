"""Market data for the long-term book, from Financial Modeling Prep.

Two cadences, because the two kinds of data go stale at completely different
rates and the free tier is metered:

    prices        daily     1 call per holding
    fundamentals  monthly   3 calls per holding

At 14 holdings that is ~14 calls a day and ~42 a month against a 250/day
allowance -- comfortable, but only because nothing here is called during a
page load. Every figure is written to the database by a scheduled refresh and
read from there afterwards. A portfolio page that fetched live would spend
its budget in an afternoon and still render slowly.

FMP's v3 API is retired; `/stable/` is the live one and the only base URL
here. Batch quotes are a paid endpoint, so prices are fetched one symbol at a
time.

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


MILLION = 1_000_000


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


async def fetch_fundamentals(
    symbol: str, client: Optional[httpx.AsyncClient] = None
) -> Fundamentals:
    """Everything the DCF needs bar growth. Three calls -- the monthly path.

    Shares are derived from market cap and price rather than fetched: the
    dedicated endpoint costs another call per holding for a number this
    response already determines exactly.
    """
    owned = client is None
    client = client or httpx.AsyncClient(timeout=TIMEOUT)
    try:
        profile = _first(await _get(client, "profile", symbol=symbol))
        cash_flow = _first(await _get(client, "cash-flow-statement",
                                      symbol=symbol, limit=1))
        balance = _first(await _get(client, "balance-sheet-statement",
                                    symbol=symbol, limit=1))

        price = _num(profile, "price")
        market_cap = _num(profile, "marketCap")
        shares_m = None
        if price and market_cap and price > 0:
            shares_m = market_cap / price / MILLION

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
            # Falls back to the reported total when the lease line is absent
            # rather than guessing at zero: an unknown lease balance should
            # leave the valuation conservative, not flatter it.
            total_debt_ex_leases_m=(
                None if total_debt_m is None
                else total_debt_m - (leases_m or 0.0)
            ),
            cash_and_st_m=_millions(
                balance, "cashAndShortTermInvestments", "cashAndCashEquivalents"
            ),
            fiscal_date=cash_flow.get("date") or balance.get("date"),
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
