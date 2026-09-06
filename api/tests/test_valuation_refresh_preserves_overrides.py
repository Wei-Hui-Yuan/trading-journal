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
    # The quote currency and the filing currency, which differ for an ADR.
    currency = "USD"
    statement_currency = "USD"
    free_cash_flow_m = 70_000.0
    # The two flows migration 039 keeps, in MSFT's real proportions: cash
    # from operations well above free cash flow (the gap is capex), and net
    # income between the two. Kept realistic rather than round because the
    # ordering is what the three models are read for.
    operating_cash_flow_m = 136_000.0
    net_income_m = 88_000.0
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
        self._existing = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        name = type(stmt).__name__
        if name == "Select":
            self._selects += 1
            return FakeResult(self._holdings if self._selects == 1 else self._existing)
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


class FakeAutoRow:
    """An existing auto row, as the staleness check reads it."""

    def __init__(self, updated_at, statement_currency=None, statement_exchange_rate=None):
        self.ticker = "MSFT"
        self.variant = "auto"
        self.updated_at = updated_at
        self.statement_currency = statement_currency
        self.statement_exchange_rate = statement_exchange_rate


def run_refresh_with_existing(monkeypatch, row):
    """One refresh where an auto row already exists, and may be skipped."""

    async def go():
        session = RecordingSession([FakeHolding()])
        session._existing = [row]

        from services import growth as growth_service
        from services import market_data

        async def fake_growth_many(tickers):
            return {}

        async def fake_fundamentals(ticker, client=None):
            return FakeFundamentals()

        monkeypatch.setattr(growth_service, "fetch_growth_many", fake_growth_many)
        monkeypatch.setattr(market_data, "fetch_fundamentals", fake_fundamentals)
        monkeypatch.setattr(market_data, "region_for", lambda *a, **k: "US")

        return await main.refresh_valuation_inputs(session=session)

    return asyncio.run(go())


def test_a_row_that_cannot_be_valued_is_due_however_recently_it_was_fetched(monkeypatch):
    """The bug that left every intrinsic value blank with no way back.

    Migration 037 added an input the DCF requires. Every existing row lacked
    it instantly -- unusable, but fetched days ago, so the staleness check
    called it fresh and skipped it. The book showed "0 refreshed, 24 still
    fresh" while every valuation read "no model", and would have kept saying
    so until the rows aged past 25 days.
    """
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    result = run_refresh_with_existing(
        monkeypatch,
        FakeAutoRow(yesterday, statement_currency=None, statement_exchange_rate=None),
    )

    assert result["refreshed"] == 1
    assert result["skipped"] == 0


def test_a_usable_recent_row_is_still_skipped(monkeypatch):
    """The guard that makes a monthly schedule idempotent has to survive.

    Without this the fix above would re-fetch the whole book on every run,
    against a 250-call daily allowance.
    """
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    result = run_refresh_with_existing(
        monkeypatch,
        FakeAutoRow(yesterday, statement_currency="USD", statement_exchange_rate=1.0),
    )

    assert result["refreshed"] == 0
    assert result["skipped"] == 1


def test_a_foreign_filer_awaiting_a_manual_rate_is_not_re_fetched_forever(monkeypatch):
    """ASML files in EUR and lists as a USD ADR.

    Its rate is NULL on purpose -- there is no FX provider here, so a human
    supplies it. That NULL must not read as "never fetched", or every run
    spends calls re-learning a currency it already knows and can do nothing
    about.
    """
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    result = run_refresh_with_existing(
        monkeypatch,
        FakeAutoRow(yesterday, statement_currency="EUR", statement_exchange_rate=None),
    )

    assert result["refreshed"] == 0
    assert result["skipped"] == 1


def written_values(session) -> dict:
    """The bound parameters of the auto-row upsert."""
    for stmt in session.statements:
        if type(stmt).__name__ != "Insert":
            continue
        params = stmt.compile().params
        if params.get("variant") == main.VARIANT_AUTO:
            return params
    raise AssertionError("the refresh wrote no auto row")


def run_refresh_with_currency(monkeypatch, statement_currency, quote_currency="USD"):
    """One refresh where the provider reports these two currencies."""

    class Fundamentals(FakeFundamentals):
        pass

    Fundamentals.currency = quote_currency
    Fundamentals.statement_currency = statement_currency

    async def go():
        session = RecordingSession([FakeHolding()])

        from services import growth as growth_service
        from services import market_data

        async def fake_growth_many(tickers):
            return {}

        async def fake_fundamentals(ticker, client=None):
            return Fundamentals()

        monkeypatch.setattr(growth_service, "fetch_growth_many", fake_growth_many)
        monkeypatch.setattr(market_data, "fetch_fundamentals", fake_fundamentals)
        monkeypatch.setattr(market_data, "region_for", lambda *a, **k: "US")

        await main.refresh_valuation_inputs(session=session)
        return session

    return asyncio.run(go())


def test_a_foreign_filer_listed_in_usd_declines_to_value(monkeypatch):
    """TSM: quoted in USD on the NYSE, filed in TWD.

    The profile says USD and the statements say TWD. Taking the profile made
    the DCF read TWD cash flows as dollars and report an intrinsic value
    roughly 32x too high -- 17,017 against a share price of 417 -- which is
    the exact conflation migration 037 was written to prevent and did not,
    because it was reading the wrong field.

    A NULL rate is the correct outcome: there is no FX provider here, so the
    trader supplies the rate and the model stays silent until they do.
    """
    values = written_values(
        run_refresh_with_currency(monkeypatch, statement_currency="TWD")
    )

    assert values["statement_currency"] == "TWD"
    assert values["statement_exchange_rate"] is None


def test_a_usd_filer_is_valued_as_before(monkeypatch):
    values = written_values(
        run_refresh_with_currency(monkeypatch, statement_currency="USD")
    )

    assert values["statement_currency"] == "USD"
    assert values["statement_exchange_rate"] == 1.0


def test_a_non_usd_quote_with_no_reported_currency_declines_to_value(monkeypatch):
    """The one case the profile-currency fallback actually decides.

    Written after a mutation exposed the first version of this test as
    worthless: it asserted the fallback prevented "blanking the book", which
    removing the fallback did not change, because `or "USD"` already resolves
    an unknown currency to 1.0. The fallback's real and only effect is here --
    a holding quoted in HKD whose statements did not report a currency. With
    it the rate goes NULL and the model stays silent; without it the holding
    is pinned at 1.0 and HKD is read as dollars.
    """
    values = written_values(
        run_refresh_with_currency(
            monkeypatch, statement_currency=None, quote_currency="HKD"
        )
    )

    assert values["statement_exchange_rate"] is None


def test_a_missing_reported_currency_cannot_blank_a_usd_quoted_holding(monkeypatch):
    """The safety property, correctly attributed.

    There is no FMP key outside the deployed environment, so whether every
    statement response carries reportedCurrency could not be confirmed before
    shipping. What guarantees a missing field is survivable is not the
    fallback but the `or "USD"` default on the rate: a USD-quoted holding
    still resolves to 1.0 and keeps its valuation.
    """
    values = written_values(
        run_refresh_with_currency(
            monkeypatch, statement_currency=None, quote_currency="USD"
        )
    )

    assert values["statement_exchange_rate"] == 1.0


def run_refresh_for_filer(
    monkeypatch, statement_currency, quote_currency="USD", country="US"
):
    """One refresh for a company with this domicile and these currencies."""

    class Fundamentals(FakeFundamentals):
        pass

    Fundamentals.currency = quote_currency
    Fundamentals.statement_currency = statement_currency
    Fundamentals.country = country

    async def go():
        holding = FakeHolding()
        holding.country = country
        session = RecordingSession([holding])

        from services import growth as growth_service
        from services import market_data

        async def fake_growth_many(tickers):
            return {}

        async def fake_fundamentals(ticker, client=None):
            return Fundamentals()

        monkeypatch.setattr(growth_service, "fetch_growth_many", fake_growth_many)
        monkeypatch.setattr(market_data, "fetch_fundamentals", fake_fundamentals)
        monkeypatch.setattr(market_data, "region_for", lambda *a, **k: "US")

        await main.refresh_valuation_inputs(session=session)
        return session

    return asyncio.run(go())


def test_a_foreign_filer_with_no_statements_asks_rather_than_assuming(monkeypatch):
    """ASML: files a 20-F, so FMP returns no statements at all.

    There is no reportedCurrency to read, and the profile says USD because
    the ADR trades in USD. Taking that pinned the rate at 1.0 and read ASML's
    EUR figures as dollars -- understating it by the whole EUR/USD factor
    with no warning anywhere on screen.

    TSM was caught because its statements arrived and said TWD. ASML was not,
    because nothing arrived. Unknown is now recorded as unknown.
    """
    values = written_values(
        run_refresh_for_filer(
            monkeypatch, statement_currency=None, quote_currency="USD", country="NL"
        )
    )

    assert values["statement_currency"] is None
    assert values["statement_exchange_rate"] is None


def test_a_us_filer_with_no_statements_is_still_valued(monkeypatch):
    """A US company files in USD by definition, so the profile can stand in.

    Without this the guardrail would blank most of the book: the Finnhub
    branch supplies no reportedCurrency at all, and it is the fallback for
    every domestic holding FMP declines to serve statements for.
    """
    values = written_values(
        run_refresh_for_filer(
            monkeypatch, statement_currency=None, quote_currency="USD", country="US"
        )
    )

    assert values["statement_currency"] == "USD"
    assert values["statement_exchange_rate"] == 1.0


def test_reported_currency_still_wins_over_domicile(monkeypatch):
    """A US-domiciled company that reports in another currency is believed.

    The domicile rule is a fallback for silence, not an override of what the
    statements actually said.
    """
    values = written_values(
        run_refresh_for_filer(
            monkeypatch, statement_currency="EUR", quote_currency="USD", country="US"
        )
    )

    assert values["statement_currency"] == "EUR"
    assert values["statement_exchange_rate"] is None
