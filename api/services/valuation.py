"""Twenty-year multi-stage DCF, matching the "True Value Finder" workbook.

This is a faithful reimplementation of the Excel model the user already
values by hand -- Piranha Ltd's calculator, sheet "VMI IV Calculator (20
years)" -- so that a valuation produced here and one produced there are the
same number, not merely similar ones.

The model, in the workbook's own terms:

    row 33/38  cash flow grown for 20 years in three stages
    row 34/39  discount factor, 1/(1+r)^n
    row 35/40  discounted value
    N14        = SUM(F35:O35) + SUM(F40:O40)      present value of 20 years
    N16        = N14 / shares                      PV per share
    N18        = debt / shares                     debt per share
    N20        = cash / shares                     cash per share
    N24        = N16 - N18 + N20                   intrinsic value per share
    M28        = price / N26 - 1                   (discount)/premium

THERE IS NO TERMINAL VALUE, and that is deliberate rather than an omission.
The horizon stops at year 20 and the business is not assumed to continue.
Adding a perpetuity -- the reflex for anyone who has built a DCF before --
roughly doubles every output and breaks agreement with the reference. This
was verified: StockOracle publishes a full input set for GOOGL, and feeding
those inputs to the function below reproduces its $175.08 to the cent.

The discount rate is NOT textbook CAPM either. The workbook uses a market
risk premium of ~3.0% for US names where a conventional equity risk premium
would be 5-6%, and it BUCKETS beta rather than using it raw. Substituting a
textbook CAPM rate understates every valuation by roughly half, which was
measured against 18 reference values before the workbook was inspected.

Nothing here touches the trading journal. This module imports no application
code, holds no database session, and is called by nothing that existed
before it -- it is pure arithmetic over its arguments.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal, Optional

Region = Literal["US", "HK"]
Scenario = Literal["base", "conservative"]

# Years in each growth stage. The workbook lays these out as two blocks of
# ten columns; the split at 5/10/20 is what the three growth inputs mean.
STAGE_1_END = 5
STAGE_2_END = 10
HORIZON = 20


@dataclass(frozen=True)
class RiskParameters:
    """Risk-free rate and market risk premium for one region.

    Both come from market-risk-premia.com, which the workbook cites and
    refreshes periodically -- the sheet records an "Updated on" date beside
    them. They are inputs with a shelf life, not constants, which is why they
    are passed in rather than hardcoded at the point of use.
    """

    risk_free_rate: float
    market_risk_premium: float
    updated_on: str = ""


# The workbook's own figures, as last recorded in the sheet. The US premium
# is far below a textbook equity risk premium; that is the model's choice and
# copying it is what makes the outputs agree.
US_RISK = RiskParameters(0.02958, 0.03010, "2025-02")
HK_RISK = RiskParameters(0.02604, 0.07412, "2025-02")

REGION_RISK: dict[str, RiskParameters] = {"US": US_RISK, "HK": HK_RISK}

# Beta is looked up in a table rather than used directly, so two companies
# whose betas differ in the third decimal get the same discount rate. The
# rungs are the workbook's, including the gap between 0.8 and 1.0 -- a beta of
# 0.95 takes the 0.8 rung, which is generous, and is what the sheet does.
BETA_RUNGS: tuple[float, ...] = (0.8, 1.0, 1.1, 1.2, 1.3, 1.4, 1.5, 1.6)

# Terminal-stage growth (years 11-20). Not a perpetuity rate -- there is no
# perpetuity -- just the rate for the last ten modelled years.
TERMINAL_GROWTH: dict[tuple[str, str], float] = {
    ("US", "base"): 0.04,
    ("US", "conservative"): 0.025,
    ("HK", "base"): 0.06,
    ("HK", "conservative"): 0.045,
}

# Ceiling applied to years 6-10 in the base case. A company compounding at
# 30% for five years is not assumed to keep doing so for another five.
STAGE_2_CAP = 0.15


def bucket_beta(beta: Optional[float]) -> float:
    """Snap a raw beta onto the workbook's lookup rungs.

    Approximate-match lookup, like the sheet's VLOOKUP: take the highest rung
    at or below the value, floor at the lowest rung and cap at the highest.
    A missing beta reads as 1.0, the market, rather than as zero risk.
    """
    if beta is None:
        return 1.0
    if beta <= BETA_RUNGS[0]:
        return BETA_RUNGS[0]
    if beta >= BETA_RUNGS[-1]:
        return BETA_RUNGS[-1]
    chosen = BETA_RUNGS[0]
    for rung in BETA_RUNGS:
        if rung <= beta:
            chosen = rung
        else:
            break
    return chosen


def discount_rate(beta: Optional[float], region: Region = "US",
                  risk: Optional[RiskParameters] = None) -> float:
    """Risk-free rate + bucketed beta x market risk premium."""
    params = risk or REGION_RISK.get(region, US_RISK)
    return params.risk_free_rate + bucket_beta(beta) * params.market_risk_premium


def growth_stages(growth_1_5: float, scenario: Scenario = "base",
                  region: Region = "US") -> tuple[float, float, float]:
    """The three growth rates, derived from the single fetched estimate.

    Only the first is data. Years 6-10 are a rule applied to it -- capped at
    15% in the base case, halved in the conservative one -- and years 11-20
    are a regional constant. Stating it this way keeps the one number that
    came from outside clearly separated from the two that are assumptions.
    """
    stage_1 = growth_1_5
    if scenario == "base":
        stage_2 = min(stage_1, STAGE_2_CAP)
    else:
        stage_2 = stage_1 / 2.0
    stage_3 = TERMINAL_GROWTH[(region, scenario)]
    return stage_1, stage_2, stage_3


@dataclass(frozen=True)
class ValuationInputs:
    """Everything the model needs, and nothing it does not.

    Money figures share one unit (the workbook says "usually millions") and
    must all be in the financial STATEMENT's currency -- as filed, which is
    not necessarily what the stock is listed in or priced in. Getting from
    there to the listing currency is TWO hops, not one, and each rate
    follows the same convention: 1 USD in that currency.

        statement currency --(statement_exchange_rate)--> USD
                            --(exchange_rate)--> listing currency

    Both default to 1.0, so a USD-filing, USD-listed company (the ordinary
    case) needs neither and the two hops collapse to a no-op, exactly as
    before this field existed. A company that files in a different currency
    than it lists in -- ASML (EUR, listed as a USD ADR), Novo Nordisk (DKK)
    -- needs statement_exchange_rate supplied, or the result is silently
    wrong: multiplying a EUR-denominated per_share by exchange_rate alone
    (issue #5, part 2 of the calculation audit) treats it as if it were
    already USD.
    """

    ticker: str
    # Free cash flow, operating cash flow, or net income depending on the
    # method chosen -- the model does not care which, only that it is the
    # flow being grown.
    base_flow: float
    shares_outstanding: float
    growth_1_5: float
    beta: Optional[float] = None
    total_debt: float = 0.0
    cash_and_st_investments: float = 0.0
    region: Region = "US"
    # USD -> listing currency (the second hop).
    exchange_rate: float = 1.0
    # Statement currency -> USD (the first hop). See the class docstring.
    statement_exchange_rate: float = 1.0
    # Overrides the CAPM-style derivation entirely when supplied, which is
    # what the editable modal writes.
    discount_rate_override: Optional[float] = None


@dataclass(frozen=True)
class ScenarioResult:
    scenario: str
    intrinsic_value: float
    growth_1_5: float
    growth_6_10: float
    growth_11_20: float
    present_value: float
    # The same trade valued WITH a perpetuity after year 20. Reported beside
    # the twenty-year figure rather than replacing it: the two answer
    # different questions, and the gap between them is the point -- a
    # terminal value that doubles the number is telling you the thesis rests
    # on year 21 onwards.
    perpetual_growth: Optional[float] = None
    #: PV of the perpetuity, in statement currency. None when the spread
    #: guard refused it.
    terminal_present_value: Optional[float] = None
    intrinsic_value_with_terminal: Optional[float] = None
    #: What share of the with-terminal value comes from the perpetuity, 0-100.
    #: Surfaced because a model whose answer is 80% terminal is really a
    #: statement about the discount rate, not about the business.
    terminal_share_pct: Optional[float] = None


@dataclass(frozen=True)
class ValuationResult:
    ticker: str
    discount_rate: float
    base: ScenarioResult
    conservative: ScenarioResult
    # Simple mean of the two, matching how the reference tool reports its
    # "Average IV" -- verified against a row carrying both: (3837 + 3172)/2
    # = 3504.50 exactly. Deliberately NOT a probability weighting.
    average_intrinsic_value: float
    #: The same mean for the with-terminal figures. None unless BOTH
    #: scenarios produced one -- averaging a with-terminal base against a
    #: twenty-year conservative would silently mix two models into a number
    #: that is neither.
    average_intrinsic_value_with_terminal: Optional[float] = None

    def premium_pct(self, price: float, against: str = "average") -> Optional[float]:
        """(price / intrinsic value - 1) x 100.

        Positive means the market is asking more than the model says it is
        worth. Returns None rather than a number when the model produced no
        value, because 0% would read as "fairly priced".
        """
        iv = {"average": self.average_intrinsic_value,
              "base": self.base.intrinsic_value,
              "conservative": self.conservative.intrinsic_value}[against]
        if iv <= 0:
            return None
        return (price / iv - 1.0) * 100.0


def present_value_of_flows(base_flow: float, rate: float,
                           stage_1: float, stage_2: float, stage_3: float) -> float:
    """Sum 20 years of grown, discounted cash flow. No terminal value."""
    total = 0.0
    flow = base_flow
    for year in range(1, HORIZON + 1):
        if year <= STAGE_1_END:
            growth = stage_1
        elif year <= STAGE_2_END:
            growth = stage_2
        else:
            growth = stage_3
        # Grown first, then discounted: year 1 is already one year's growth
        # on the current figure, exactly as the sheet's F33 does.
        flow *= (1.0 + growth)
        total += flow / ((1.0 + rate) ** year)
    return total


# ---------------------------------------------------------------------------
# Terminal value
# ---------------------------------------------------------------------------

#: Growth assumed to continue FOREVER, after the twenty explicit years.
#:
#: NOT `TERMINAL_GROWTH`, despite the name of that constant, and the
#: distinction is the whole reason this model needed its own number. Stage 3
#: is what years 11-20 grow at -- a finite stretch, and 4% for a decade is an
#: ordinary assumption. A PERPETUAL rate is a different claim: it says the
#: company outgrows the economy for the rest of time, so it cannot plausibly
#: exceed long-run nominal GDP.
#:
#: Reusing stage 3 here is not merely aggressive, it is arithmetically
#: unstable on this book. Gordon growth divides by (rate - growth), and with
#: stage 3 at 4% against derived discount rates of 5.37%-7.77%, HALF of the
#: holdings land on a spread under 2%: CHRW, FDS, MBGL, NVO, PANW, SHLD, TMO
#: and UNH all sit at 1.37%, which is a terminal multiple of 76x final-year
#: cash flow. MSFT lands at 52.8x. Those figures are driven entirely by the
#: gap between two assumptions, neither of them measured, and they would
#: dominate the valuation while looking precise.
#:
#: At 2.5% every holding in the book clears the guard below, with multiples
#: between 19x and 36x -- a range a reader can argue with rather than one
#: that swamps the explicit forecast.
PERPETUAL_GROWTH: dict[tuple[str, str], float] = {
    ("US", "base"): 0.025,
    ("US", "conservative"): 0.020,
    ("HK", "base"): 0.030,
    ("HK", "conservative"): 0.025,
}

#: Minimum (discount rate - perpetual growth) before a terminal value is
#: reported at all.
#:
#: Gordon growth has no upper bound as the denominator approaches zero, so
#: without a floor a low-beta holding produces an enormous number rather than
#: an error. Below this the model does not return a smaller value, it returns
#: NOTHING -- the same "None rather than a fabricated figure" contract
#: premium_pct and compute_r_multiple already keep. A caller that wants a
#: value anyway can raise the discount rate by hand, which is a decision
#: rather than a default.
MIN_TERMINAL_SPREAD = 0.02


def final_year_flow(base_flow: float, stage_1: float, stage_2: float,
                    stage_3: float) -> float:
    """The flow in year `HORIZON`, which is what perpetuity grows from.

    Deliberately a separate walk rather than a second return value from
    `present_value_of_flows`: that function's signature is called directly by
    a dozen tests, and widening it to serve this model would change every one
    of them for a number they do not use. The loop is four lines and the two
    must agree, so the growth schedule is applied identically here -- if they
    ever diverge, `test_the_final_flow_matches_the_last_year_of_the_sum`
    fails.
    """
    flow = base_flow
    for year in range(1, HORIZON + 1):
        if year <= STAGE_1_END:
            flow *= (1.0 + stage_1)
        elif year <= STAGE_2_END:
            flow *= (1.0 + stage_2)
        else:
            flow *= (1.0 + stage_3)
    return flow


def present_value_of_terminal(final_flow: float, rate: float,
                              perpetual_growth: float) -> Optional[float]:
    """Gordon growth value of everything after year 20, discounted to today.

        TV      = final_flow x (1 + g) / (rate - g)
        PV(TV)  = TV / (1 + rate) ** HORIZON

    None -- never a number -- when the spread is thinner than
    MIN_TERMINAL_SPREAD, or when the flow being grown is not positive. A
    negative denominator would return a NEGATIVE terminal value, which reads
    as "the future is a liability" rather than as "this model does not apply
    here", and a near-zero one returns a figure with no information in it.
    """
    if final_flow <= 0:
        return None
    spread = rate - perpetual_growth
    if spread < MIN_TERMINAL_SPREAD:
        return None
    terminal = final_flow * (1.0 + perpetual_growth) / spread
    return terminal / ((1.0 + rate) ** HORIZON)

def value_scenario(inputs: ValuationInputs, rate: float,
                   scenario: Scenario) -> ScenarioResult:
    """One scenario's intrinsic value per share."""
    stage_1, stage_2, stage_3 = growth_stages(
        inputs.growth_1_5, scenario, inputs.region
    )

    if inputs.shares_outstanding <= 0 or inputs.base_flow <= 0:
        # A company with no shares or no positive flow cannot be valued this
        # way. Zero is returned rather than a negative or a crash, and
        # premium_pct reports None for it rather than inventing a percentage.
        # Terminal fields stay None: an unvaluable company has no
        # perpetuity either, and a 0.0 there would read as a computed
        # answer rather than an absent one.
        return ScenarioResult(scenario, 0.0, stage_1, stage_2, stage_3, 0.0)

    pv = present_value_of_flows(inputs.base_flow, rate, stage_1, stage_2, stage_3)

    def per_share_value(total_pv: float) -> float:
        """The workbook's N16 - N18 + N20, then the two currency hops.

        Shared by the twenty-year and with-terminal figures rather than
        written twice. Debt, cash and BOTH exchange rates apply identically
        to either present value, and two copies of this arithmetic is exactly
        how one variant ends up quietly missing a hop -- the failure issue #5
        of the calculation audit already found once.
        """
        per_share = total_pv / inputs.shares_outstanding
        per_share -= inputs.total_debt / inputs.shares_outstanding
        per_share += inputs.cash_and_st_investments / inputs.shares_outstanding
        # Statement currency -> USD -> listing currency. See ValuationInputs'
        # docstring for why this is two hops rather than one multiplication.
        return (per_share / inputs.statement_exchange_rate) * inputs.exchange_rate

    intrinsic_value = per_share_value(pv)

    perpetual = PERPETUAL_GROWTH[(inputs.region, scenario)]
    terminal_pv = present_value_of_terminal(
        final_year_flow(inputs.base_flow, stage_1, stage_2, stage_3),
        rate,
        perpetual,
    )
    with_terminal: Optional[float] = None
    terminal_share: Optional[float] = None
    if terminal_pv is not None:
        with_terminal = per_share_value(pv + terminal_pv)
        # Share of the COMBINED present value, not of the per-share figure --
        # debt and cash shift the latter and would make the percentage read
        # as something other than "how much of this comes from year 21 on".
        combined = pv + terminal_pv
        if combined > 0:
            terminal_share = round(terminal_pv / combined * 100.0, 1)

    return ScenarioResult(
        scenario=scenario,
        intrinsic_value=round(intrinsic_value, 2),
        growth_1_5=stage_1,
        growth_6_10=stage_2,
        growth_11_20=stage_3,
        present_value=pv,
        perpetual_growth=perpetual,
        terminal_present_value=terminal_pv,
        intrinsic_value_with_terminal=(
            None if with_terminal is None else round(with_terminal, 2)
        ),
        terminal_share_pct=terminal_share,
    )


def value(inputs: ValuationInputs) -> ValuationResult:
    """Run both scenarios and report them with their average."""
    rate = inputs.discount_rate_override
    if rate is None:
        rate = discount_rate(inputs.beta, inputs.region)

    base = value_scenario(inputs, rate, "base")
    conservative = value_scenario(inputs, rate, "conservative")

    both_have_terminal = (
        base.intrinsic_value_with_terminal is not None
        and conservative.intrinsic_value_with_terminal is not None
    )

    return ValuationResult(
        ticker=inputs.ticker,
        discount_rate=rate,
        base=base,
        conservative=conservative,
        average_intrinsic_value=round(
            (base.intrinsic_value + conservative.intrinsic_value) / 2.0, 2
        ),
        average_intrinsic_value_with_terminal=(
            round(
                (
                    base.intrinsic_value_with_terminal
                    + conservative.intrinsic_value_with_terminal
                )
                / 2.0,
                2,
            )
            if both_have_terminal
            else None
        ),
    )
