"""A perpetuity after year 20, and the guard that stops it inventing one.

The twenty-year model already here answers "what are two decades of this
flow worth". It says nothing about year 21 onwards, which for a durable
business is most of the value -- so the two figures side by side, and the gap
between them, is the actual output of this pair. A terminal value that
doubles the number is telling you the thesis rests on the far end.

WHY THIS NEEDED ITS OWN GROWTH CONSTANT. `TERMINAL_GROWTH` is what years
11-20 grow at: a finite stretch, where 4% for a decade is ordinary. A
PERPETUAL rate is a different claim -- it says the company outgrows the
economy for the rest of time. Reusing stage 3 is not merely aggressive, it is
arithmetically unstable on this book: Gordon divides by (rate - growth), and
4% against derived discount rates of 5.37%-7.77% puts HALF the holdings on a
spread under 2%, which is a terminal multiple of 53x-76x final-year cash
flow. Those figures are driven entirely by the gap between two unmeasured
assumptions and would swamp the explicit forecast while looking precise.

So the guard below is the load-bearing part of this module, not the formula.
"""

import os

import pytest

os.environ.setdefault("CORS_ALLOW_ORIGINS", "https://trading-journal-test.vercel.app")

from services import valuation as v  # noqa: E402


def inputs(**over):
    """MSFT's real figures, which is what the model was checked against."""
    base = dict(
        ticker="MSFT", base_flow=66987.0, shares_outstanding=7425.55,
        growth_1_5=0.1829, beta=1.1, total_debt=40294.0,
        cash_and_st_investments=76843.0, region="US",
    )
    base.update(over)
    return v.ValuationInputs(**base)


class TestTheGuard:
    def test_a_thin_spread_returns_nothing_rather_than_a_huge_number(self):
        """Gordon has no upper bound as the denominator approaches zero. Half
        this book sits at a 1.37% spread against stage-3 growth, which is a
        76x terminal multiple -- a number with no information in it."""
        rate = v.PERPETUAL_GROWTH[("US", "base")] + v.MIN_TERMINAL_SPREAD - 0.001

        assert v.present_value_of_terminal(
            100.0, rate, v.PERPETUAL_GROWTH[("US", "base")]
        ) is None

    def test_a_comfortable_spread_is_allowed(self):
        """Just above the threshold, not exactly on it. `0.045 - 0.025` is
        0.019999999999999997 in binary floating point, so the exact boundary
        lands on whichever side the representation error falls -- pinning it
        would be pinning float noise, and no real holding sits on it. What
        matters is that a spread clearly above the floor produces a value."""
        rate = v.PERPETUAL_GROWTH[("US", "base")] + v.MIN_TERMINAL_SPREAD + 0.001

        assert v.present_value_of_terminal(
            100.0, rate, v.PERPETUAL_GROWTH[("US", "base")]
        ) is not None

    def test_growth_above_the_discount_rate_returns_none_not_a_negative(self):
        """The denominator goes negative, and so would the terminal value. A
        negative reads as "the future is a liability" rather than as "this
        model does not apply here"."""
        assert v.present_value_of_terminal(100.0, 0.02, 0.05) is None

    def test_a_non_positive_flow_has_no_perpetuity(self):
        assert v.present_value_of_terminal(0.0, 0.08, 0.025) is None
        assert v.present_value_of_terminal(-50.0, 0.08, 0.025) is None

    def test_the_users_own_two_percent_override_is_refused(self):
        """Not hypothetical: the MSFT modal carries a hand-set 2% discount
        rate, which is BELOW any plausible perpetual growth. Compounding
        forever faster than you discount is an infinite value, and the guard
        is what stops that reaching the screen."""
        result = v.value(inputs(discount_rate_override=0.02))

        assert result.base.intrinsic_value_with_terminal is None
        assert result.average_intrinsic_value_with_terminal is None
        # The twenty-year figure is unaffected and still reported.
        assert result.base.intrinsic_value == 598.58


class TestTheFlowSchedule:
    def test_the_final_flow_uses_the_three_stage_schedule(self):
        # 5 years at 10%, 5 at 8%, 10 at 4%.
        expected = 100.0 * (1.10 ** 5) * (1.08 ** 5) * (1.04 ** 10)

        assert v.final_year_flow(100.0, 0.10, 0.08, 0.04) == pytest.approx(expected)

    def test_it_agrees_with_the_sum_the_pv_walks(self):
        """The two functions walk the same schedule in separate loops, which
        is a duplication that could drift. At rate 0 the PV is just the sum
        of the flows, so rebuilding that sum here and comparing pins them
        together -- if either loop's stage boundaries move, this fails."""
        stages = (0.12, 0.09, 0.03)
        pv_at_zero_rate = v.present_value_of_flows(100.0, 0.0, *stages)

        flow, total = 100.0, 0.0
        for year in range(1, v.HORIZON + 1):
            g = stages[0] if year <= v.STAGE_1_END else (
                stages[1] if year <= v.STAGE_2_END else stages[2]
            )
            flow *= (1.0 + g)
            total += flow

        assert total == pytest.approx(pv_at_zero_rate)
        assert flow == pytest.approx(v.final_year_flow(100.0, *stages))


class TestTheValue:
    def test_it_adds_to_the_twenty_year_figure_rather_than_replacing_it(self):
        result = v.value(inputs())

        assert result.base.intrinsic_value == 369.71
        assert result.base.intrinsic_value_with_terminal == 871.11
        # Both reported: the gap between them IS the output.
        assert result.base.intrinsic_value_with_terminal > result.base.intrinsic_value

    def test_it_reports_how_much_of_the_value_is_the_perpetuity(self):
        """A model whose answer is 80% terminal is a statement about the
        discount rate, not about the business. Shown so that is visible."""
        result = v.value(inputs())

        assert result.base.terminal_share_pct == pytest.approx(57.9, abs=0.1)
        assert 0 < result.base.terminal_share_pct < 100

    def test_debt_cash_and_both_currency_hops_apply_to_the_terminal_too(self):
        """The whole reason the per-share conversion was factored out. A
        second copy is how one variant ends up missing a hop -- the failure
        issue #5 of the calculation audit already found once.

        TSM's shape: files in TWD, trades as a USD ADR.
        """
        plain = v.value(inputs(statement_exchange_rate=1.0))
        in_twd = v.value(inputs(statement_exchange_rate=30.0))

        # Both figures divide by the same rate, so their ratio is unchanged.
        # abs=0.01, because the engine rounds each figure to cents before
        # returning it -- dividing an already-rounded number by 30 cannot
        # match to more precision than that.
        assert in_twd.base.intrinsic_value == pytest.approx(
            plain.base.intrinsic_value / 30.0, abs=0.01
        )
        assert in_twd.base.intrinsic_value_with_terminal == pytest.approx(
            plain.base.intrinsic_value_with_terminal / 30.0, abs=0.01
        )

    def test_the_conservative_scenario_uses_a_lower_perpetual_rate(self):
        result = v.value(inputs())

        assert result.base.perpetual_growth == 0.025
        assert result.conservative.perpetual_growth == 0.020
        assert (
            result.conservative.intrinsic_value_with_terminal
            < result.base.intrinsic_value_with_terminal
        )


class TestTheAverage:
    def test_it_means_the_two_with_terminal_figures(self):
        result = v.value(inputs())
        expected = round(
            (
                result.base.intrinsic_value_with_terminal
                + result.conservative.intrinsic_value_with_terminal
            ) / 2.0,
            2,
        )

        assert result.average_intrinsic_value_with_terminal == expected

    def test_it_is_none_unless_both_scenarios_produced_one(self, monkeypatch):
        """Averaging a with-terminal base against a twenty-year conservative
        would silently mix two models into a number that is neither."""
        real = v.present_value_of_terminal

        def only_base(final_flow, rate, perpetual_growth):
            # The conservative scenario is the one with the lower perpetual
            # rate, so refusing that one leaves base with a value.
            if perpetual_growth == v.PERPETUAL_GROWTH[("US", "conservative")]:
                return None
            return real(final_flow, rate, perpetual_growth)

        monkeypatch.setattr(v, "present_value_of_terminal", only_base)
        result = v.value(inputs())

        assert result.base.intrinsic_value_with_terminal is not None
        assert result.conservative.intrinsic_value_with_terminal is None
        assert result.average_intrinsic_value_with_terminal is None


class TestUnvaluable:
    def test_no_shares_reports_nothing_rather_than_a_terminal_of_zero(self):
        result = v.value(inputs(shares_outstanding=0.0))

        assert result.base.intrinsic_value == 0.0
        assert result.base.intrinsic_value_with_terminal is None
        assert result.base.terminal_share_pct is None
