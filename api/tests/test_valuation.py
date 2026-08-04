"""The 20-year DCF has to agree with the workbook it replaces.

This is not a test of whether the model is a GOOD model -- it is a test of
whether it is the SAME model the user already values by hand. A valuation
that is merely plausible is worthless here: the whole point is that a number
produced by the app and one produced in "True Value Finder" are the same
number, so the app can be trusted to replace the spreadsheet.

The anchor is StockOracle's published GOOGL card, which lists every input
alongside its output. If `test_reproduces_the_published_reference_exactly`
ever fails, the model has drifted and every figure the portfolio shows is
suspect.

Two properties are pinned hard because both are counter-intuitive and both
would be "fixed" by anyone applying textbook DCF reflexes:

  * NO TERMINAL VALUE. The horizon stops dead at year 20.
  * The discount rate uses a ~3% market risk premium and a BUCKETED beta,
    where textbook CAPM would use 5-6% and the raw figure.

Each was measured before being believed. A textbook CAPM rate understated 18
reference valuations by a median of 49%; adding a perpetuity roughly doubles
every output and destroys agreement with the reference.
"""

import os

import pytest

os.environ.setdefault("CORS_ALLOW_ORIGINS", "https://trading-journal-test.vercel.app")

from services import valuation as v  # noqa: E402


# ---------------------------------------------------------------------------
# The anchor: a published card with every input and its output
# ---------------------------------------------------------------------------
#
# StockOracle, GOOGL. Free cash flow 66,728M; total debt excluding lease
# obligations 24,607M; cash and short-term investments 95,148M; discount rate
# 5.97%; shares 12,198M; growth 14.94% / 9.77% / 4%. It reports an intrinsic
# value of $175.08 against a price of $202.49, a 15.66% premium.
#
# Growth years 6-10 are given directly here rather than derived, because
# 9.77% is neither min(14.94%, 15%) nor 14.94%/2 -- the reference fetches it
# as its own figure. The rule this module applies is the user's, and is
# tested separately below.


def _googl_pv() -> float:
    return v.present_value_of_flows(
        base_flow=66_728.0, rate=0.0597,
        stage_1=0.1494, stage_2=0.0977, stage_3=0.04,
    )


def test_reproduces_the_published_reference_exactly():
    """To the cent. This is the test that says the model is the right one."""
    pv = _googl_pv()
    iv = (pv - 24_607.0 + 95_148.0) / 12_198.0
    assert round(iv, 2) == 175.08


def test_reproduces_the_published_premium_exactly():
    pv = _googl_pv()
    iv = (pv - 24_607.0 + 95_148.0) / 12_198.0
    assert round((202.49 / iv - 1.0) * 100.0, 2) == 15.66


def test_the_same_figures_through_the_public_entry_point():
    """Not just the maths in isolation -- the shape callers actually use."""
    result = v.value(v.ValuationInputs(
        ticker="GOOGL", base_flow=66_728.0, shares_outstanding=12_198.0,
        growth_1_5=0.1494, total_debt=24_607.0,
        cash_and_st_investments=95_148.0, discount_rate_override=0.0597,
    ))
    # The reference's 9.77% for years 6-10 is its own fetched figure; this
    # module's base-case rule caps at 15%, which leaves 14.94% untouched and
    # therefore values HIGHER than the reference. Directionally correct and
    # deliberately not forced to match.
    assert result.base.intrinsic_value > 175.08
    assert result.base.growth_6_10 == pytest.approx(0.1494)


# ---------------------------------------------------------------------------
# No terminal value
# ---------------------------------------------------------------------------


def test_the_horizon_stops_at_twenty_years():
    """A 21st year must contribute nothing. Adding a perpetuity here is the
    single most likely 'improvement' someone would make, and it would break
    agreement with every reference value."""
    pv = v.present_value_of_flows(100.0, 0.06, 0.10, 0.08, 0.04)

    manual = 0.0
    flow = 100.0
    for year in range(1, 21):
        g = 0.10 if year <= 5 else (0.08 if year <= 10 else 0.04)
        flow *= (1 + g)
        manual += flow / (1.06 ** year)

    assert pv == pytest.approx(manual)


def test_present_value_is_finite_when_growth_exceeds_the_discount_rate():
    """A perpetuity would divide by a negative number and explode. A fixed
    horizon simply produces a large finite number, which is why the model can
    value a fast grower at all."""
    pv = v.present_value_of_flows(100.0, 0.05, 0.20, 0.15, 0.08)
    assert pv > 0 and pv < 1e9


# ---------------------------------------------------------------------------
# The discount rate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("beta,expected", [
    (0.42, 0.8), (0.80, 0.8), (0.95, 0.8),   # no 0.9 rung; 0.95 floors to 0.8
    (1.00, 1.0), (1.05, 1.0),
    (1.247, 1.2),                             # GOOGL's actual beta from FMP
    (1.55, 1.5), (1.60, 1.6), (3.10, 1.6),    # capped
])
def test_beta_snaps_to_the_workbook_rungs(beta, expected):
    assert v.bucket_beta(beta) == expected


def test_a_missing_beta_reads_as_the_market():
    """1.0, not 0. A company with no published beta is not risk-free."""
    assert v.bucket_beta(None) == 1.0


def test_the_us_rate_matches_the_reference_card():
    """StockOracle's GOOGL card shows 5.97%, which is the beta-1.0 rung:
    2.958% + 1.0 x 3.010%."""
    assert round(v.discount_rate(1.0, "US") * 100, 2) == 5.97


def test_hong_kong_names_use_their_own_premium():
    """The workbook keeps a separate table, and the difference is large --
    7.4% against 3.0% -- so routing by region is not cosmetic."""
    assert v.discount_rate(1.0, "HK") > v.discount_rate(1.0, "US")
    assert round(v.discount_rate(1.0, "HK") * 100, 2) == 10.02


def test_a_textbook_capm_rate_would_be_far_higher():
    """Guards the decision, not the arithmetic. Textbook CAPM on a 4.2%
    risk-free rate and a 5.5% equity risk premium gives ~9.7% where this
    model gives ~6.0% -- and that gap understated 18 reference valuations by
    a median of 49%."""
    textbook = 0.042 + 1.0 * 0.055
    assert v.discount_rate(1.0, "US") < textbook * 0.65


def test_an_override_replaces_the_derived_rate_entirely():
    """What the editable modal writes."""
    result = v.value(v.ValuationInputs(
        ticker="X", base_flow=1000.0, shares_outstanding=100.0,
        growth_1_5=0.10, beta=2.0, discount_rate_override=0.08,
    ))
    assert result.discount_rate == 0.08


# ---------------------------------------------------------------------------
# Growth staging
# ---------------------------------------------------------------------------


def test_base_case_caps_the_middle_stage_at_fifteen_percent():
    _, stage_2, _ = v.growth_stages(0.30, "base", "US")
    assert stage_2 == 0.15


def test_base_case_leaves_a_slower_grower_untouched():
    """min(), not a flat rate: a 9% grower keeps growing at 9%."""
    _, stage_2, _ = v.growth_stages(0.09, "base", "US")
    assert stage_2 == pytest.approx(0.09)


def test_conservative_case_halves_the_middle_stage():
    _, stage_2, _ = v.growth_stages(0.30, "conservative", "US")
    assert stage_2 == pytest.approx(0.15)
    _, stage_2_slow, _ = v.growth_stages(0.08, "conservative", "US")
    assert stage_2_slow == pytest.approx(0.04)


def test_the_middle_stage_actually_moderates():
    """The bug in the original plan: `min(g, 0.15)` where g was already
    capped at 0.15 made the base case's 'moderated' years identical to years
    1-5. Here a fast grower must genuinely slow down."""
    _, stage_2, _ = v.growth_stages(0.28, "base", "US")
    assert stage_2 < 0.28


def test_conservative_is_worth_less_than_base():
    inputs = v.ValuationInputs(
        ticker="X", base_flow=1000.0, shares_outstanding=100.0,
        growth_1_5=0.20, beta=1.0,
    )
    result = v.value(inputs)
    assert result.conservative.intrinsic_value < result.base.intrinsic_value


def test_terminal_stage_differs_by_region_and_scenario():
    assert v.growth_stages(0.1, "base", "US")[2] == 0.04
    assert v.growth_stages(0.1, "conservative", "US")[2] == 0.025
    assert v.growth_stages(0.1, "base", "HK")[2] == 0.06


# ---------------------------------------------------------------------------
# Average, and the premium formula
# ---------------------------------------------------------------------------


def test_average_is_a_simple_mean_not_a_probability_weighting():
    """Pinned against a reference row carrying both figures: conservative
    3172.00 and base 3837.00 report an average of 3504.50, which is the plain
    mean. The original plan's 60/25/15 weighting would give 3670.75."""
    assert round((3837.00 + 3172.00) / 2, 2) == 3504.50


def test_the_result_reports_the_mean_of_its_two_scenarios():
    result = v.value(v.ValuationInputs(
        ticker="X", base_flow=1000.0, shares_outstanding=100.0,
        growth_1_5=0.12, beta=1.0,
    ))
    expected = (result.base.intrinsic_value + result.conservative.intrinsic_value) / 2
    assert result.average_intrinsic_value == pytest.approx(round(expected, 2))


def test_premium_is_positive_when_the_market_asks_more_than_the_model():
    result = v.value(v.ValuationInputs(
        ticker="X", base_flow=1000.0, shares_outstanding=100.0,
        growth_1_5=0.05, beta=1.0,
    ))
    iv = result.average_intrinsic_value
    assert result.premium_pct(iv * 1.20) == pytest.approx(20.0)
    assert result.premium_pct(iv * 0.80) == pytest.approx(-20.0)


def test_an_unvaluable_company_reports_no_premium_rather_than_zero():
    """An ETF, or anything with no positive flow. 0% would read as 'fairly
    priced', which is a claim the model cannot make -- roughly a quarter of
    the user's holdings are ETFs."""
    result = v.value(v.ValuationInputs(
        ticker="VOO", base_flow=0.0, shares_outstanding=0.0, growth_1_5=0.0,
    ))
    assert result.average_intrinsic_value == 0.0
    assert result.premium_pct(616.72) is None


# ---------------------------------------------------------------------------
# Cash, debt and currency
# ---------------------------------------------------------------------------


def test_net_cash_moves_the_valuation_by_exactly_its_per_share_amount():
    """Applied after the division, per the workbook's N16 - N18 + N20."""
    common = dict(ticker="X", base_flow=1000.0, shares_outstanding=100.0,
                  growth_1_5=0.10, beta=1.0)
    bare = v.value(v.ValuationInputs(**common)).base.intrinsic_value
    with_cash = v.value(v.ValuationInputs(
        **common, cash_and_st_investments=500.0
    )).base.intrinsic_value
    assert with_cash - bare == pytest.approx(5.0, abs=0.01)


def test_debt_reduces_it_by_its_per_share_amount():
    common = dict(ticker="X", base_flow=1000.0, shares_outstanding=100.0,
                  growth_1_5=0.10, beta=1.0)
    bare = v.value(v.ValuationInputs(**common)).base.intrinsic_value
    with_debt = v.value(v.ValuationInputs(**common, total_debt=300.0)).base.intrinsic_value
    assert bare - with_debt == pytest.approx(3.0, abs=0.01)


def test_the_exchange_rate_converts_only_the_final_per_share_figure():
    """The workbook's N26 = N24 x rate. Statement currency in, listing
    currency out -- the user holds HK and European names."""
    common = dict(ticker="X", base_flow=1000.0, shares_outstanding=100.0,
                  growth_1_5=0.10, beta=1.0)
    at_par = v.value(v.ValuationInputs(**common)).base.intrinsic_value
    converted = v.value(v.ValuationInputs(**common, exchange_rate=7.8)).base.intrinsic_value
    # `at_par` is already rounded to cents, so scaling it up re-scales that
    # rounding error too -- the tolerance is that error (0.005 x rate), not
    # slack. The engine rounds ONCE, after converting, which is the correct
    # order and the reason these are not exactly equal.
    assert converted == pytest.approx(at_par * 7.8, abs=0.005 * 7.8)


# ---------------------------------------------------------------------------
# The guardrail
# ---------------------------------------------------------------------------


def test_the_module_imports_no_application_code():
    """The investment work must not be able to break the trading journal.
    This module is pure arithmetic: no database session, no main.py, no
    matching engine. If that ever stops being true, the blast radius of a
    change here stops being zero and this test says so."""
    import inspect

    source = inspect.getsource(v)
    for forbidden in ("import main", "from main", "matching_engine",
                      "AsyncSession", "sqlalchemy"):
        assert forbidden not in source, f"valuation.py must not reference {forbidden}"
