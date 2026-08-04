"""The one DCF input that cannot be computed: how fast the flow grows.

Years 6-10 and 11-20 are rules applied to this number inside
services/valuation.py -- capped at 15%, then a regional terminal constant.
Only years 1-5 come from data, which makes this the single most consequential
figure in the model and the one most worth being explicit about.

TWO SOURCES, AND THEY ARE NOT THE SAME KIND OF NUMBER.

    finviz    `EPS next 5Y`, a FORWARD analyst consensus. The same kind of
              figure the reference tool this engine was calibrated against
              appears to use, and the reason its numbers reconciled when a
              trailing-CAGR attempt sat roughly 49% low. Preferred.

    finnhub   `epsGrowth5Y`, a TRAILING five-year historical CAGR. A fallback,
              not an equivalent -- it projects the last five years forward for
              twenty, so a company mid-slump is valued as though the slump is
              permanent. AAPL's own trailing 3Y and 5Y differ by more than
              2.5x, which is the method being unstable rather than noise.

`is_forward` rides on every result so the modal can say which kind it got
instead of presenting the two as interchangeable.

WHY FINVIZ IS SCRAPED AND THROTTLED. There is no API and no key, so there is
no quota to reason about -- the only signal is a 429, and measurement puts
that at roughly three requests in quick succession from one IP. Hence
THROTTLE_SECONDS between symbols, and hence the batch giving up on Finviz for
the remainder of a run once it is blocked rather than spending twelve more
requests learning the same thing. At a monthly cadence over ~13 valuable
holdings that is about a minute of wall clock in a background job.

Being a scrape, this is the most fragile thing in the valuation path: Finviz
owes no compatibility and has already broken the `finvizfinance` package's
parser. Every failure here is soft -- a symbol resolves to Finnhub, then to
None, and a None means the modal asks for a manual override. Nothing raises
into the caller's refresh loop.

This module imports no application code and holds no database session.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from typing import Iterable, Optional

import httpx

from services.market_data import MarketDataError

logger = logging.getLogger(__name__)

FINVIZ_URL = "https://finviz.com/quote.ashx"
FINNHUB_URL = "https://finnhub.io/api/v1/stock/metric"

# Finviz serves a different page to an obvious bot. This is not evasion -- the
# request rate is deliberately below what a person browsing would generate.
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

# Measured, not guessed: three rapid requests were enough to earn a 429.
THROTTLE_SECONDS = 5.0

TIMEOUT = httpx.Timeout(20.0, connect=10.0)

# Bounds on the stage-1 rate, applied after fetching and RECORDED when they
# bite. A clamp that fired invisibly would be worse than no clamp, so
# `clamped` and the original `reported` figure are both carried out.
#
# The two sources get different ceilings because they fail differently. A
# forward consensus is a considered estimate, so it is bounded only against
# absurdity -- Nvidia's real 47.55% must survive, or the clamp is overruling
# the analysts rather than catching bad data. A trailing CAGR has no such
# standing: above roughly 30% it is nearly always a boom that already
# happened, and projecting it forward five years double-counts it. Nvidia is
# the textbook case, trailing at 95.3% against that 47.55% consensus.
FORWARD_CEILING = 0.50
TRAILING_CEILING = 0.30
GROWTH_FLOOR = -0.10


@dataclass(frozen=True)
class GrowthEstimate:
    """A stage-1 growth rate, and enough provenance to judge it.

    `growth_1_5` is a decimal fraction (0.1269, not 12.69) because that is
    what the valuation engine takes. Both providers report percentages and
    are converted here, at the boundary.
    """

    symbol: str
    growth_1_5: float
    source: str          # "finviz" | "finnhub"
    is_forward: bool
    reported: float      # what the provider said, before any clamp
    clamped: bool = False


class FinvizBlocked(MarketDataError):
    """Finviz answered 429. Distinct because it is not about this symbol --
    every remaining symbol in the batch will get the same answer, so the
    caller should stop asking rather than retry twelve more times."""

    def __init__(self) -> None:
        super().__init__(
            "Finviz rate limit reached; falling back to Finnhub for the "
            "remainder of this refresh.",
            status=429,
        )


def _percent(raw: Optional[str]) -> Optional[float]:
    """'12.69%' -> 0.1269. Finviz writes '-' for absent, which is not zero."""
    if raw is None:
        return None
    text = raw.strip().replace("%", "").replace(",", "")
    if not text or text == "-":
        return None
    try:
        return float(text) / 100.0
    except ValueError:
        return None


def _snapshot_field(html: str, label: str) -> Optional[str]:
    """Pull one labelled value out of Finviz's snapshot table.

    Parsed by pattern rather than with BeautifulSoup so that the deployed API
    gains no dependency for one scraper. The table is a flat run of
    label/value pairs; the bounded gap keeps a missing label from matching
    the next row's content.

    The cell's whole inner HTML is captured and its tags stripped, rather
    than the text being matched directly. Finviz nests the value differently
    depending on the row -- a bare `<b>`, a `<b><span class="color-text
    is-positive">` when it colour-codes a figure, a `<b><small>` on the
    combined rows -- and a pattern that assumed any one of those silently
    returned nothing for the others. That is not hypothetical: it read
    Alphabet correctly while dropping Nvidia, ASML and Novo Nordisk onto the
    trailing fallback, which for Novo Nordisk meant +20.66% in place of an
    actual consensus of -0.86%.
    """
    pattern = (
        r'snapshot-td-label"[^>]*>\s*' + re.escape(label) + r"\s*</div>"
        r'.{0,400}?snapshot-td-content"[^>]*>(.*?)</div>'
    )
    found = re.search(pattern, html, re.DOTALL)
    if not found:
        return None
    return re.sub(r"<[^>]+>", "", found.group(1)).strip() or None


async def _finviz_growth(symbol: str, client: httpx.AsyncClient) -> Optional[float]:
    """Forward five-year consensus, or None if this symbol has no estimate.

    ETFs and most funds have none -- there are no analysts forecasting an
    index's earnings -- which is expected rather than a failure.
    """
    try:
        response = await client.get(
            FINVIZ_URL,
            params={"t": symbol},
            headers={"User-Agent": USER_AGENT},
        )
    except httpx.HTTPError as exc:
        logger.warning("Finviz unreachable for %s: %s", symbol, exc)
        return None

    if response.status_code == 429:
        raise FinvizBlocked()
    if response.status_code >= 400:
        logger.warning("Finviz refused %s: %s", symbol, response.status_code)
        return None

    return _percent(_snapshot_field(response.text, "EPS next 5Y"))


async def _finnhub_growth(symbol: str, client: httpx.AsyncClient) -> Optional[float]:
    """Trailing five-year EPS CAGR, falling back to the three-year.

    Finnhub reports these as percentages (17.91 meaning 17.91%).
    """
    token = (os.environ.get("FINNHUB_API") or "").strip()
    if not token:
        logger.warning("FINNHUB_API is not set; no growth fallback available.")
        return None

    try:
        response = await client.get(
            FINNHUB_URL, params={"symbol": symbol, "metric": "all", "token": token}
        )
    except httpx.HTTPError as exc:
        logger.warning("Finnhub unreachable for %s: %s", symbol, exc)
        return None

    if response.status_code >= 400:
        logger.warning("Finnhub refused %s: %s", symbol, response.status_code)
        return None

    metrics = (response.json() or {}).get("metric") or {}
    for field in ("epsGrowth5Y", "epsGrowth3Y"):
        raw = metrics.get(field)
        if raw is not None:
            try:
                return float(raw) / 100.0
            except (TypeError, ValueError):
                continue
    return None


def _clamped(symbol: str, rate: float, source: str, is_forward: bool) -> GrowthEstimate:
    ceiling = FORWARD_CEILING if is_forward else TRAILING_CEILING
    bounded = max(GROWTH_FLOOR, min(ceiling, rate))
    if bounded != rate:
        logger.info(
            "%s: %s reported %.1f%% growth, clamped to %.1f%%",
            symbol, source, rate * 100, bounded * 100,
        )
    return GrowthEstimate(
        symbol=symbol,
        growth_1_5=bounded,
        source=source,
        is_forward=is_forward,
        reported=rate,
        clamped=bounded != rate,
    )


async def fetch_growth(
    symbol: str,
    *,
    client: Optional[httpx.AsyncClient] = None,
    allow_finviz: bool = True,
) -> Optional[GrowthEstimate]:
    """One symbol's stage-1 growth: Finviz, then Finnhub, then None.

    None is a real answer, not an error -- it means neither provider covers
    this symbol, and the modal should ask for a manual override rather than
    value it on a guess. Pass `allow_finviz=False` to skip straight to the
    fallback once a batch has been blocked.
    """
    owned = client is None
    client = client or httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True)
    try:
        if allow_finviz:
            forward = await _finviz_growth(symbol, client)
            if forward is not None:
                return _clamped(symbol, forward, "finviz", is_forward=True)

        trailing = await _finnhub_growth(symbol, client)
        if trailing is not None:
            return _clamped(symbol, trailing, "finnhub", is_forward=False)

        logger.info("No growth estimate available for %s from either source.", symbol)
        return None
    finally:
        if owned:
            await client.aclose()


async def fetch_growth_many(
    symbols: Iterable[str],
    *,
    client: Optional[httpx.AsyncClient] = None,
    throttle: float = THROTTLE_SECONDS,
) -> dict[str, Optional[GrowthEstimate]]:
    """Growth for a whole book, paced so Finviz does not block partway.

    Sequential and deliberately slow. Concurrency here would trip the rate
    limit on the second symbol and convert most of the portfolio to trailing
    figures, which is the opposite of the point.

    Pass only the holdings worth valuing -- an ETF has no cash flows of its
    own, so spending a throttled request on VOO buys nothing.
    """
    symbols = list(symbols)
    results: dict[str, Optional[GrowthEstimate]] = {}
    finviz_open = True

    for index, symbol in enumerate(symbols):
        if index and throttle and finviz_open:
            await asyncio.sleep(throttle)

        try:
            results[symbol] = await fetch_growth(
                symbol, client=client, allow_finviz=finviz_open
            )
        except FinvizBlocked:
            # Blocked is about the IP, not the symbol. Stop paying the
            # throttle for a source that will refuse every remaining call,
            # and finish the run on Finnhub.
            logger.warning("Finviz blocked at %s; remainder of run uses Finnhub.",
                           symbol)
            finviz_open = False
            results[symbol] = await fetch_growth(
                symbol, client=client, allow_finviz=False
            )

    return results
