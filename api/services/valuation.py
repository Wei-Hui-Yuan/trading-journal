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
    must all be in the financial statement's currency. `exchange_rate`
    converts the final per-share figure into the listing currency, which is
    the only place the two can differ.
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
    exchange_rate: float = 1.0
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
        return ScenarioResult(scenario, 0.0, stage_1, stage_2, stage_3, 0.0)

    pv = present_value_of_flows(inputs.base_flow, rate, stage_1, stage_2, stage_3)

    # The workbook's N16 - N18 + N20: everything per share, debt removed and
    # cash added AFTER the division rather than folded into the flow.
    per_share = pv / inputs.shares_outstanding
    per_share -= inputs.total_debt / inputs.shares_outstanding
    per_share += inputs.cash_and_st_investments / inputs.shares_outstanding

    return ScenarioResult(
        scenario=scenario,
        intrinsic_value=round(per_share * inputs.exchange_rate, 2),
        growth_1_5=stage_1,
        growth_6_10=stage_2,
        growth_11_20=stage_3,
        present_value=pv,
    )


def value(inputs: ValuationInputs) -> ValuationResult:
    """Run both scenarios and report them with their average."""
    rate = inputs.discount_rate_override
    if rate is None:
        rate = discount_rate(inputs.beta, inputs.region)

    base = value_scenario(inputs, rate, "base")
    conservative = value_scenario(inputs, rate, "conservative")

    return ValuationResult(
        ticker=inputs.ticker,
        discount_rate=rate,
        base=base,
        conservative=conservative,
        average_intrinsic_value=round(
            (base.intrinsic_value + conservative.intrinsic_value) / 2.0, 2
        ),
    )
