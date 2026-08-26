"""The daily price refresh: what it fetches, how fast, and when it stops.

Every test runs against a stubbed `fetch_quote` and a stubbed session. No
network, no API key, no database -- the free FMP tier is 250 calls a day and a
suite that spent them would break the thing it is testing.

The refresh used to be a plain `for` loop awaiting one symbol at a time, which
at this book's size is ~25 sequential round trips behind a spinner. It now runs
them concurrently under a semaphore. Three properties have to survive that, and
each is tested below:

  * Every holding still gets its own price, paired with the right symbol.
    `asyncio.gather` preserves input order; the zip back onto `holdings`
    depends on it, and a regression there would write GOOGL's price onto AMD.
  * A 429 still stops the run asking for more. The sequential version used
    `break`; concurrently there is no loop to break out of, so the intent
    lives in a flag checked after acquiring the semaphore.
  * The ORM objects are still mutated one at a time, after the network work,
    because an AsyncSession is not safe under concurrent use.
"""

import asyncio
import os

os.environ.setdefault("CORS_ALLOW_ORIGINS", "https://trading-journal-test.vercel.app")

from services import market_data as md  # noqa: E402


class FakeHolding:
    """Only the columns refresh_prices reads or writes."""

    def __init__(self, ticker: str):
        self.ticker = ticker
        self.current_price = None
        self.price_updated_at = None
        self.day_change_pct = None
        self.day_change_updated_at = None


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class FakeSession:
    """Enough of AsyncSession for this handler: one select, one commit."""

    def __init__(self, holdings):
        self._holdings = holdings
        self.commits = 0

    async def execute(self, *_args, **_kwargs):
        return FakeResult(self._holdings)

    async def commit(self):
        self.commits += 1


def run_refresh(holdings, fake_fetch, monkeypatch):
    """Drive the endpoint with `fetch_quote` replaced, and return its body."""
    import main  # noqa: PLC0415

    monkeypatch.setattr(md, "fetch_quote", fake_fetch)
    session = FakeSession(holdings)
    result = asyncio.run(main.refresh_prices(session=session))
    return result, session


def quote(symbol, price, change=None):
    return md.Quote(symbol=symbol, price=price, currency="USD", day_change_pct=change)


def test_every_holding_gets_its_own_price(monkeypatch):
    """The price written to a row is the one fetched for THAT symbol.

    gather returns in input order regardless of completion order, and the
    handler zips that back onto `holdings`. Prices are deliberately keyed to
    the ticker so a mispairing shows up as a wrong number, not a missing one.
    """
    prices = {"AAPL": 100.0, "AMD": 200.0, "GOOGL": 300.0, "NVDA": 400.0}
    holdings = [FakeHolding(t) for t in prices]

    async def fake(symbol, client=None):
        # Finish in REVERSE order, so a handler relying on completion order
        # rather than input order pairs everything wrongly.
        await asyncio.sleep(0.01 * (len(prices) - list(prices).index(symbol)))
        return quote(symbol, prices[symbol])

    body, session = run_refresh(holdings, fake, monkeypatch)

    assert body == {"updated": 4, "failed": 0, "failures": []}
    assert {h.ticker: h.current_price for h in holdings} == prices
    assert all(h.price_updated_at is not None for h in holdings)
    assert session.commits == 1


def test_the_quotes_are_actually_fetched_concurrently(monkeypatch):
    """The point of the change. Sequential would take len(holdings) x delay."""
    holdings = [FakeHolding(f"SYM{i}") for i in range(12)]
    peak = 0
    in_flight = 0

    async def fake(symbol, client=None):
        nonlocal peak, in_flight
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.02)
        in_flight -= 1
        return quote(symbol, 1.0)

    body, _ = run_refresh(holdings, fake, monkeypatch)

    assert body["updated"] == 12
    # Sequentially this peaks at 1. Anything above proves overlap.
    assert peak > 1, "quotes were still fetched one at a time"


def test_no_more_than_the_limit_are_in_flight_at_once(monkeypatch):
    """Bounded, not unbounded.

    A wide fan-out is what would newly trip a per-second ceiling that paced
    calls never approach -- the reason the semaphore exists rather than a bare
    gather over every holding.
    """
    import main  # noqa: PLC0415

    holdings = [FakeHolding(f"SYM{i}") for i in range(30)]
    peak = 0
    in_flight = 0

    async def fake(symbol, client=None):
        nonlocal peak, in_flight
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return quote(symbol, 1.0)

    run_refresh(holdings, fake, monkeypatch)

    assert peak <= main.PRICE_REFRESH_CONCURRENCY, (
        f"{peak} calls were in flight at once, over the "
        f"{main.PRICE_REFRESH_CONCURRENCY} limit"
    )


def test_a_rate_limit_stops_the_rest_being_asked(monkeypatch):
    """429 means the daily allowance is gone; asking again cannot help.

    The sequential version broke out of the loop. Here every symbol still
    queued checks the flag after acquiring the semaphore, so the run stops
    spending calls it already knows will fail.
    """
    import main  # noqa: PLC0415

    holdings = [FakeHolding(f"SYM{i:02d}") for i in range(40)]
    asked = []

    async def fake(symbol, client=None):
        asked.append(symbol)
        await asyncio.sleep(0.005)
        raise md.MarketDataError("allowance spent", status=429)

    body, _ = run_refresh(holdings, fake, monkeypatch)

    # Only the calls already in flight when the first 429 landed get made.
    assert len(asked) < len(holdings), "every symbol was asked despite the 429"
    assert len(asked) <= main.PRICE_REFRESH_CONCURRENCY
    # Those that were asked are reported; those never asked are not invented.
    assert body["failed"] == len(asked)
    assert body["updated"] == 0


def test_a_symbol_never_asked_is_neither_updated_nor_failed(monkeypatch):
    """Matches what the sequential `break` left behind, exactly.

    A symbol the run never reached was not a failure -- nothing was tried. It
    keeps its previous price and stays out of both counters.
    """
    holdings = [FakeHolding(f"SYM{i:02d}") for i in range(20)]
    for h in holdings:
        h.current_price = 42.0  # a price from an earlier, successful run

    async def fake(symbol, client=None):
        await asyncio.sleep(0.005)
        raise md.MarketDataError("allowance spent", status=429)

    body, _ = run_refresh(holdings, fake, monkeypatch)

    untouched = [h for h in holdings if h.price_updated_at is None]
    assert len(untouched) == len(holdings)
    # Every one keeps the price it already had rather than being blanked.
    assert all(h.current_price == 42.0 for h in holdings)
    assert body["updated"] + body["failed"] < len(holdings)


def test_one_dead_symbol_does_not_cost_the_others_their_prices(monkeypatch):
    """A 404 is about that symbol. It must not end the run."""
    holdings = [FakeHolding(t) for t in ("AAPL", "BADSYM", "GOOGL", "NVDA")]

    async def fake(symbol, client=None):
        await asyncio.sleep(0.005)
        if symbol == "BADSYM":
            raise md.MarketDataError("FMP returned no price for BADSYM.", status=404)
        return quote(symbol, 123.0)

    body, _ = run_refresh(holdings, fake, monkeypatch)

    assert body["updated"] == 3
    assert body["failed"] == 1
    assert body["failures"] == [
        {"ticker": "BADSYM", "detail": "FMP returned no price for BADSYM."}
    ]
    assert [h.current_price for h in holdings] == [123.0, None, 123.0, 123.0]


def test_a_missing_day_change_leaves_the_last_one_standing(monkeypatch):
    """None means "the response omitted it", not "the stock was flat".

    Pre-existing behaviour, kept under concurrency: 0.0 is a real flat day and
    must still be written.
    """
    holdings = [FakeHolding(t) for t in ("KEEP", "FLAT")]
    holdings[0].day_change_pct = -1.09
    stale_stamp = "2020-01-01T00:00:00+00:00"
    holdings[0].day_change_updated_at = stale_stamp

    async def fake(symbol, client=None):
        return quote(symbol, 10.0, change=None if symbol == "KEEP" else 0.0)

    body, _ = run_refresh(holdings, fake, monkeypatch)

    assert body["updated"] == 2
    assert holdings[0].day_change_pct == -1.09  # untouched
    assert holdings[1].day_change_pct == 0.0    # written

    # The failsafe (migration 036): price_updated_at still advances for BOTH
    # holdings on every successful quote, but day_change_updated_at only
    # advances when day_change_pct itself was actually rewritten. That gap is
    # what lets a reader detect that KEEP's day figure is now stale relative
    # to its own fresh price, rather than silently presenting it as current.
    assert holdings[0].day_change_updated_at == stale_stamp
    assert holdings[0].day_change_updated_at != holdings[0].price_updated_at
    assert holdings[1].day_change_updated_at == holdings[1].price_updated_at


def test_an_empty_book_asks_for_nothing(monkeypatch):
    """No holdings, no client, no commit path worth exercising."""

    async def fake(symbol, client=None):  # pragma: no cover - must not run
        raise AssertionError("fetch_quote was called with no holdings")

    body, session = run_refresh([], fake, monkeypatch)

    assert body == {"updated": 0, "failed": 0, "failures": []}
    assert session.commits == 0
