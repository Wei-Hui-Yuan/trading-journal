"""The monthly refresh, and the hand-tuned inputs it must never overwrite.

A trader who disagrees with a fetched beta or discount rate corrects it in
the valuation modal. That correction has to survive the next automatic
refresh, or the app quietly reverts a considered judgement to a vendor
number roughly once a month -- and the trader has no reason to look, because
nothing changed on screen at the moment it happened.

The behaviour is currently correct BY CONSTRUCTION rather than by test:
`refresh_valuation_inputs` reads and writes `variant='auto'` only, and
`_merge_inputs` lets an override's non-NULL fields win. Nothing asserted
either half, so nothing would have failed if a later edit widened the
refresh's write to cover override rows too.

That gap is not hypothetical. `refresh_valuation_inputs` had NO test of any kind
before this file, which is the same blind spot that let migration 037 ship a
column the refresh populated for new rows and no backfill ever populated for
existing ones -- every intrinsic value in the book went blank on deploy.

Stubbed session, stubbed fetches, no network and no database, matching
test_price_refresh.py: the free FMP tier is 250 calls a day and a suite that
spent them would break the thing it is testing.
"""

import asyncio
import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("CORS_ALLOW_ORIGINS", "https://trading-journal-test.vercel.app")

import main  # noqa: E402

NOW = datetime(2026, 8, 29, tzinfo=timezone.utc)


class FakeHolding:
    def __init__(self, ticker="MSFT"):
        self.ticker = ticker
        self.is_valuable = True
        self.country = "US"
        self.exchange_rate = 1
        # Also written by the refresh, from the same response.
        self.name = "Microsoft"
        self.sector = "Technology"
        self.current_price = None
        self.price_updated_at = None


class FakeFundamentals:
    """What market_data.fetch_fundamentals returns, in the fields read."""

    country = "US"
    currency = "USD"
    free_cash_flow_m = 70_000.0
    shares_outstanding_m = 7_400.0
    total_debt_ex_leases_m = 45_000.0
    cash_and_st_m = 80_000.0
    # The vendor's beta -- deliberately different from the trader's, below.
    beta = 0.91
    name = "Microsoft Corp"
    sector = "Technology"
    price = 499.99


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class RecordingSession:
    """Captures every statement, so what was written can be inspected.

    Returns holdings for the first SELECT and no existing auto rows for the
    second, which is what puts every holding in the 'due' list.
    """

    def __init__(self, holdings):
        self._holdings = holdings
        self._selects = 0
        self.statements = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        name = type(stmt).__name__
        if name == "Select":
            self._selects += 1
            return FakeResult(self._holdings if self._selects == 1 else [])
        return FakeResult([])

    async def commit(self):
        pass


def written_variants(session) -> list[str]:
    """The `variant` every INSERT in the run targeted."""
    variants = []
    for stmt in session.statements:
        if type(stmt).__name__ != "Insert":
            continue
        params = stmt.compile().params
        if "variant" in params:
            variants.append(params["variant"])
    return variants


def run_refresh(monkeypatch, holdings=None):
    """Run one refresh over a single holding, with the network stubbed."""

    async def go():
        holdings_ = holdings or [FakeHolding()]
        session = RecordingSession(holdings_)

        from services import growth as growth_service
        from services import market_data

        async def fake_growth_many(tickers):
            return {}

        async def fake_fundamentals(ticker, client=None):
            return FakeFundamentals()

        monkeypatch.setattr(growth_service, "fetch_growth_many", fake_growth_many)
        monkeypatch.setattr(market_data, "fetch_fundamentals", fake_fundamentals)
        monkeypatch.setattr(market_data, "region_for", lambda *a, **k: "US")

        await main.refresh_valuation_inputs(session=session)
        return session

    return asyncio.run(go())


def test_the_refresh_only_ever_writes_the_auto_variant(monkeypatch):
    """The single property the trader's corrections depend on.

    Everything else in this file is a consequence of it. If a refresh ever
    writes an override row, a hand-set beta is gone and nothing on screen
    says so.
    """
    session = run_refresh(monkeypatch)

    variants = written_variants(session)
    assert variants, "the refresh wrote nothing at all -- the stub is wrong"
    assert set(variants) == {main.VARIANT_AUTO}
    assert main.VARIANT_OVERRIDE not in variants


def test_a_hand_tuned_beta_and_discount_rate_survive_a_refresh(monkeypatch):
    """End to end, in the two steps the app actually performs.

    The refresh writes the auto row; the merge then decides what the DCF
    sees. Asserted together because neither half alone is the promise made
    to the trader.
    """
    session = run_refresh(monkeypatch)

    # What the refresh just fetched, as the auto row.
    auto = main.InvestmentValuationInput(
        ticker="MSFT",
        variant=main.VARIANT_AUTO,
        base_flow=FakeFundamentals.free_cash_flow_m,
        shares_outstanding=FakeFundamentals.shares_outstanding_m,
        total_debt=FakeFundamentals.total_debt_ex_leases_m,
        cash_and_st=FakeFundamentals.cash_and_st_m,
        beta=FakeFundamentals.beta,
        growth_1_5=0.08,
        region="US",
        statement_exchange_rate=1.0,
    )
    # What the trader corrected by hand, months ago. Only two fields -- the
    # rest are deliberately NULL, meaning "I did not touch this".
    override = main.InvestmentValuationInput(
        ticker="MSFT",
        variant=main.VARIANT_OVERRIDE,
        beta=1.35,
        discount_rate=0.11,
        region="US",
    )

    merged, overridden = main._merge_inputs(auto, override)

    # The judgement stands.
    assert merged["beta"] == 1.35
    assert merged["discount_rate"] == 0.11
    assert "beta" in overridden and "discount_rate" in overridden

    # The plumbing still updates.
    assert merged["total_debt"] == FakeFundamentals.total_debt_ex_leases_m
    assert merged["cash_and_st"] == FakeFundamentals.cash_and_st_m
    assert merged["base_flow"] == FakeFundamentals.free_cash_flow_m
    assert "total_debt" not in overridden
    assert "cash_and_st" not in overridden


def test_the_refresh_reads_only_auto_rows_when_judging_staleness(monkeypatch):
    """An override's age must not decide whether a refresh is due.

    Reading both variants here would let a hand-edit made yesterday mark the
    fetched data fresh, and the auto row would then go stale indefinitely.
    """
    session = run_refresh(monkeypatch)

    selects = [s for s in session.statements if type(s).__name__ == "Select"]
    params = selects[1].compile().params

    assert main.VARIANT_AUTO in params.values()
    assert main.VARIANT_OVERRIDE not in params.values()
