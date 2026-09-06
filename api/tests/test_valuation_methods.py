"""Three models off one engine, and the choice between them.

The reference tool draws DFCF-20, DCF-20 and DNI-20 as three separate bars.
They are not three models -- they are ONE model run on three different lines
of the financial statements, which is what makes putting them side by side
worth doing. Everything else is held identical by construction: same
discount rate, same growth schedule, same debt and cash bridge, same
currency hops. So the gap between the bars is attributable to exactly one
thing, and the tests below are mostly about proving that nothing else moved.

WHAT THE GAPS MEAN, which is why the ordering assertions matter:

    DCF-20 far above DFCF-20   operating cash is going into capex
    DNI-20 far above both      earnings the cash flow statement does not
                               corroborate

The other half of this file is about refusing. A method whose flow is
absent, or present but not positive, produces NO model rather than a zero --
`_value_holding`'s standing contract, which net income tests properly for
the first time: a loss-making year is a real reading, and rendering it as an
intrinsic value of 0.00 says the company is worth nothing when the truth is
that this model does not apply to it.
"""

import os

import pytest

os.environ.setdefault("CORS_ALLOW_ORIGINS", "https://trading-journal-test.vercel.app")

import main  # noqa: E402


def holding(**kw):
    return main.InvestmentHolding(
        ticker="TEST",
        exchange_rate=kw.pop("exchange_rate", 1),
        current_price=kw.pop("current_price", 100.0),
        is_valuable=True,
        **kw,
    )


def inputs(**over):
    """A complete set, with all three flows present.

    MSFT's real proportions -- operating cash flow well above free cash flow
    because of capex, net income between them.
    """
    merged = {
        "base_flow": 70_000.0,
        "operating_cash_flow": 136_000.0,
        "net_income": 88_000.0,
        "shares_outstanding": 7_400.0,
        "growth_1_5": 0.10,
        "beta": 1.1,
        "total_debt": 45_000.0,
        "cash_and_st": 80_000.0,
        "region": "US",
        "discount_rate": None,
        "metric": "free_cash_flow",
        "statement_exchange_rate": 1.0,
    }
    merged.update(over)
    return merged


class TestAllThreeRun:
    def test_every_method_with_a_flow_is_returned(self):
        result = main._value_holding(holding(), inputs(), [])

        assert set(result["models"]) == {
            "free_cash_flow", "operating_cash_flow", "net_income"
        }

    def test_they_differ_by_the_flow_and_by_nothing_else(self):
        """The load-bearing claim of the whole feature. If the discount rate
        or the growth schedule could differ between two bars, the gap between
        them would no longer be attributable to the input, and comparing them
        would mean nothing."""
        models = main._value_holding(holding(), inputs(), [])["models"]

        rates = {m["discount_rate"] for m in models.values()}
        growths = {
            (m["base"]["growth_1_5"], m["base"]["growth_6_10"],
             m["base"]["growth_11_20"])
            for m in models.values()
        }
        assert len(rates) == 1
        assert len(growths) == 1
        # And the values themselves are all different, so the test above is
        # not passing merely because nothing ran.
        assert len({m["average_intrinsic_value"] for m in models.values()}) == 3

    def test_a_larger_flow_values_higher(self):
        """Ordering follows the flows: operating cash flow (136k) > net
        income (88k) > free cash flow (70k), so the valuations must rank the
        same way. Anything else means a flow is reaching the wrong model."""
        models = main._value_holding(holding(), inputs(), [])["models"]

        assert (
            models["operating_cash_flow"]["average_intrinsic_value"]
            > models["net_income"]["average_intrinsic_value"]
            > models["free_cash_flow"]["average_intrinsic_value"]
        )

    def test_each_model_carries_its_own_premium_against_the_price(self):
        models = main._value_holding(holding(current_price=400.0), inputs(), [])[
            "models"
        ]

        assert len({m["premium_pct"] for m in models.values()}) == 3
        for model in models.values():
            assert model["premium_pct"] is not None

    def test_the_terminal_value_is_computed_for_each_of_them(self):
        """PR #94's second figure is per-model too, not only for the default
        one -- it is the same engine call."""
        models = main._value_holding(holding(), inputs(), [])["models"]

        for model in models.values():
            assert model["average_intrinsic_value_with_terminal"] is not None


class TestTheSelection:
    def test_the_default_is_free_cash_flow_and_matches_the_old_shape(self):
        """Every holding valued on free cash flow before this existed, and
        the top-level keys every other reader uses -- the portfolio table,
        premium_pct, the CSV export -- must still be that model's."""
        result = main._value_holding(holding(), inputs(), [])

        assert result["method"] == "free_cash_flow"
        assert result["available"] is True
        assert (
            result["average_intrinsic_value"]
            == result["models"]["free_cash_flow"]["average_intrinsic_value"]
        )
        assert result["base"] == result["models"]["free_cash_flow"]["base"]

    @pytest.mark.parametrize(
        "method", ["free_cash_flow", "operating_cash_flow", "net_income"]
    )
    def test_the_chosen_method_is_the_one_spread_across_the_top_level(self, method):
        result = main._value_holding(
            holding(valuation_method=method), inputs(), []
        )

        assert result["method"] == method
        assert (
            result["average_intrinsic_value"]
            == result["models"][method]["average_intrinsic_value"]
        )
        assert result["premium_pct"] == result["models"][method]["premium_pct"]

    def test_an_unrecognised_method_values_on_free_cash_flow(self):
        """A renamed or hand-edited row still gets a valuation, and the
        response says which method produced it rather than implying the
        stored one did."""
        result = main._value_holding(
            holding(valuation_method="ebitda"), inputs(), []
        )

        assert result["available"] is True
        assert result["method"] == "free_cash_flow"

    def test_a_holding_that_predates_the_column_values_as_before(self):
        """valuation_method is NULL on an object never persisted through the
        migration's default."""
        result = main._value_holding(holding(valuation_method=None), inputs(), [])

        assert result["method"] == "free_cash_flow"
        assert result["available"] is True


class TestRefusing:
    def test_a_method_with_no_flow_produces_no_model_for_it(self):
        result = main._value_holding(
            holding(), inputs(net_income=None, operating_cash_flow=None), []
        )

        assert set(result["models"]) == {"free_cash_flow"}
        # The chosen one still worked, so the holding is valued.
        assert result["available"] is True

    def test_choosing_a_method_with_no_flow_declines_and_names_the_field(self):
        result = main._value_holding(
            holding(valuation_method="net_income"), inputs(net_income=None), []
        )

        assert result["available"] is False
        assert result["missing"] == ["net_income"]
        assert result["method"] == "net_income"

    def test_it_does_not_quietly_fall_back_to_a_method_that_worked(self):
        """A number under the wrong label is worse than no number: the
        holding would show a valuation the trader did not choose,
        indistinguishable from the one they did. The models that DID run are
        still returned, so the modal can offer the switch."""
        result = main._value_holding(
            holding(valuation_method="net_income"), inputs(net_income=None), []
        )

        assert result["available"] is False
        assert "average_intrinsic_value" not in result
        # But the working models are still there to switch to.
        assert set(result["models"]) == {"free_cash_flow", "operating_cash_flow"}

    @pytest.mark.parametrize("loss", [0.0, -12_500.0])
    def test_a_non_positive_flow_reports_nothing_rather_than_a_zero_valuation(
        self, loss
    ):
        """The case net income introduces and free cash flow never really
        did. 0.00 on the screen reads as "this business is worth nothing",
        which is a claim about the company; the truth is about the model."""
        result = main._value_holding(
            holding(valuation_method="net_income"), inputs(net_income=loss), []
        )

        assert result["available"] is False
        assert result["non_positive_flow"] == "net_income"
        assert "net_income" not in result["models"]

    def test_a_loss_is_distinguished_from_an_absent_figure(self):
        """Different problems: one is a gap a refresh or a hand-keyed
        override fills, the other is a fact about the year that no amount of
        fetching changes."""
        absent = main._value_holding(
            holding(valuation_method="net_income"), inputs(net_income=None), []
        )
        loss = main._value_holding(
            holding(valuation_method="net_income"), inputs(net_income=-500.0), []
        )

        assert absent["missing"] == ["net_income"]
        assert absent["non_positive_flow"] is None
        assert loss["missing"] == []
        assert loss["non_positive_flow"] == "net_income"

    def test_a_missing_shared_input_stops_every_model_not_just_one(self):
        """shares_outstanding, growth and the statement rate feed all three,
        so there is nothing to offer a switch to and `models` is empty rather
        than partially filled."""
        result = main._value_holding(holding(), inputs(growth_1_5=None), [])

        assert result["available"] is False
        assert result["missing"] == ["growth_1_5"]
        assert result["models"] == {}

    def test_a_missing_statement_rate_still_declines_for_every_method(self):
        """Migration 037's guard must not have been widened by 039: three
        models mean three chances to silently treat a foreign filer's
        figures as dollars."""
        result = main._value_holding(
            holding(valuation_method="net_income"),
            inputs(statement_exchange_rate=None),
            [],
        )

        assert result["available"] is False
        assert result["missing"] == ["statement_exchange_rate"]
        assert result["models"] == {}


class TestBackwardsCompatibility:
    def test_a_merged_dict_without_the_new_keys_still_values(self):
        """Every caller written before migration 039 -- and every existing
        test -- passes a dict with base_flow and no other flow."""
        legacy = inputs()
        del legacy["operating_cash_flow"]
        del legacy["net_income"]

        result = main._value_holding(holding(), legacy, [])

        assert result["available"] is True
        assert set(result["models"]) == {"free_cash_flow"}

    def test_the_two_new_flows_go_through_the_override_merge(self):
        """So a holding no provider covers can have either hand-keyed, the
        same way base_flow already can."""
        assert "operating_cash_flow" in main.VALUATION_FIELDS
        assert "net_income" in main.VALUATION_FIELDS

        merged, overridden = main._merge_inputs(
            None,
            main.InvestmentValuationInput(
                ticker="NVO", variant="override", region="US",
                net_income=39_000.0,
            ),
        )
        assert merged["net_income"] == 39_000.0
        assert "net_income" in overridden

    def test_an_override_flow_wins_over_the_fetched_one(self):
        merged, _ = main._merge_inputs(
            main.InvestmentValuationInput(
                ticker="T", variant="auto", region="US",
                operating_cash_flow=100.0, net_income=90.0,
            ),
            main.InvestmentValuationInput(
                ticker="T", variant="override", region="US",
                operating_cash_flow=250.0,
            ),
        )

        assert merged["operating_cash_flow"] == 250.0
        # Untouched by the override, so it falls through to the fetched row.
        assert merged["net_income"] == 90.0


class TestTheMethodValidator:
    @pytest.mark.parametrize(
        "method", ["free_cash_flow", "operating_cash_flow", "net_income"]
    )
    def test_each_known_method_is_accepted(self, method):
        assert main.HoldingUpdate(
            valuation_method=method
        ).valuation_method == method

    def test_an_unknown_method_is_refused_with_the_allowed_values(self):
        with pytest.raises(ValueError, match="net_income"):
            main.HoldingUpdate(valuation_method="ebitda")

    def test_an_explicit_null_is_refused_rather_than_reaching_a_not_null_column(
        self,
    ):
        """The column is NOT NULL with a default, so "clear it" has no
        meaning -- there is always some method in force. Refusing here turns
        what would be a 500 from the driver into a 422 that says what to
        send."""
        with pytest.raises(ValueError, match="cannot be null"):
            main.HoldingUpdate(valuation_method=None)

    def test_omitting_it_entirely_still_means_leave_it_alone(self):
        """The other half: `exclude_unset` in the handler is what makes a
        partial edit partial, and the validator above must not have broken
        it for every other field on this model."""
        patch = main.HoldingUpdate(sector="Technology").model_dump(
            exclude_unset=True
        )

        assert patch == {"sector": "Technology"}
        assert "valuation_method" not in patch


class TestTheRegistry:
    def test_every_method_names_a_field_the_merge_supplies(self):
        """A method whose column is not in VALUATION_FIELDS would read as
        permanently absent -- the override merge is what puts a flow into
        `merged` at all."""
        for _method, (field, _label) in main.VALUATION_METHODS.items():
            assert field in main.VALUATION_FIELDS

    def test_the_default_is_one_of_them(self):
        assert main.DEFAULT_VALUATION_METHOD in main.VALUATION_METHODS
