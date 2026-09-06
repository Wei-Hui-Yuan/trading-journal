"""Losses measured against the stop they were planned against.

The distinction the whole metric rests on is the -1R boundary, and it is one
comparison away from being wrong in a way nothing else on the page would
contradict. A trade stopped out cleanly is EXACTLY -1R and cost what it was
supposed to; a trade at -1.0001R spent more than the plan allowed. Getting
that edge wrong either flatters the account (every clean stop counted as an
overrun -> a leak that is not there) or hides the leak entirely.

`recoverable_r` is the figure worth guarding hardest, because it is the one a
decision gets made on: the R that comes back if every overrun had stopped
where it was planned to. On the real account it is 11.68R against a total of
-5.91R, which is the difference between "I need a better strategy" and "my
existing strategies work and my stops do not".

Deliberately NOT tested here, because it is deliberately not implemented: any
split of overruns into gapped-through versus stop-moved. See the docstring on
compute_stop_integrity -- `actual_stop_loss` is populated on 122 of this
account's 140 round trips and differs from `stop_loss` on none of them, so a
classification built on it would report a finding it has no evidence for.
"""

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from services.analytics import (
    STOP_OVERRUN_SAMPLE,
    ReviewedTrade,
    compute_stop_integrity,
)


def loss(r_target=None, *, stop="98", exit_=None, pnl="-10", ticker="AAA", when=None):
    """A long entered at 100. `r_target` picks the exit that lands on that R.

    Entry 100 / stop 98 is 2 points of risk, so R = (exit - 100) / 2 and an
    exit of 100 + 2R gives exactly the R asked for. Spelled out rather than
    hand-computed per test, since the -1R boundary is what these tests are
    about and an arithmetic slip there would quietly agree with the bug.
    """
    if exit_ is None:
        exit_ = str(100 + 2 * r_target)
    return ReviewedTrade(
        trade_id=str(uuid.uuid4()),
        ticker=ticker,
        direction="BUY",
        quantity=Decimal("10"),
        actual_entry=Decimal("100"),
        exit_price=Decimal(exit_),
        planned_entry=None,
        stop_loss=Decimal(stop) if stop is not None else None,
        mistakes=[],
        review_status="reviewed",
        realized_pnl=Decimal(pnl),
        exit_time=when or datetime(2026, 6, 1, tzinfo=timezone.utc),
    )


class TestTheBoundary:
    def test_a_clean_stop_out_is_within_plan_not_an_overrun(self):
        """Exactly -1R is the stop doing its job. Counting it as an overrun
        would invent a leak on every disciplined trade."""
        result = compute_stop_integrity([loss(-1.0)])

        assert result["overrun_count"] == 0
        assert result["within_count"] == 1
        assert result["recoverable_r"] == 0.0

    def test_a_hair_past_the_stop_is_an_overrun(self):
        result = compute_stop_integrity([loss(-1.05)])

        assert result["overrun_count"] == 1
        assert result["within_count"] == 0

    def test_a_loss_cut_before_the_stop_is_within_plan(self):
        result = compute_stop_integrity([loss(-0.4)])

        assert result["within_count"] == 1
        assert result["overrun_count"] == 0


class TestRecoverableR:
    def test_it_sums_how_far_past_the_stop_each_overrun_went(self):
        # -1.5R and -2.0R are 0.5R and 1.0R past the stop respectively.
        result = compute_stop_integrity([loss(-1.5), loss(-2.0)])

        assert result["overrun_count"] == 2
        assert result["recoverable_r"] == 1.5

    def test_it_is_a_positive_magnitude_not_more_negative_r(self):
        """It sits beside avg_r and total_r, both of which are negative on a
        losing account. A negative figure here would read as further loss
        rather than as loss that need not have happened."""
        result = compute_stop_integrity([loss(-3.0)])

        assert result["recoverable_r"] > 0

    def test_winners_contribute_nothing(self):
        result = compute_stop_integrity([loss(2.0, pnl="40"), loss(-1.5)])

        assert result["overrun_count"] == 1
        assert result["assessed_losses"] == 1
        assert result["recoverable_r"] == 0.5


class TestUnassessable:
    def test_a_loss_with_no_stop_is_counted_separately_not_as_compliant(self):
        """It has no planned risk to be measured against. Folding it into
        `within_count` would report it as a trade that respected a stop it
        never had -- the same reasoning behind `unscored_trades`."""
        result = compute_stop_integrity([loss(stop=None, exit_="95")])

        assert result["unassessable_losses"] == 1
        assert result["within_count"] == 0
        assert result["overrun_count"] == 0
        assert result["assessed_losses"] == 0

    def test_an_unscoreable_winner_is_not_counted_as_an_unassessed_loss(self):
        result = compute_stop_integrity([loss(stop=None, exit_="110", pnl="100")])

        assert result["unassessable_losses"] == 0


class TestWorstOverruns:
    def test_they_are_ordered_worst_first(self):
        result = compute_stop_integrity(
            [loss(-1.2, ticker="MILD"), loss(-3.0, ticker="WORST"), loss(-1.8, ticker="MID")]
        )

        assert [o["ticker"] for o in result["worst_overruns"]] == ["WORST", "MID", "MILD"]

    def test_the_list_is_capped_so_the_payload_stays_a_summary(self):
        result = compute_stop_integrity([loss(-1.5) for _ in range(STOP_OVERRUN_SAMPLE + 5)])

        assert result["overrun_count"] == STOP_OVERRUN_SAMPLE + 5
        assert len(result["worst_overruns"]) == STOP_OVERRUN_SAMPLE

    def test_each_row_carries_what_it_takes_to_find_the_trade(self):
        result = compute_stop_integrity(
            [loss(-1.5, ticker="NVDA", when=datetime(2026, 3, 4, tzinfo=timezone.utc))]
        )
        row = result["worst_overruns"][0]

        assert row["ticker"] == "NVDA"
        assert row["exit_time"].startswith("2026-03-04")
        assert row["r_multiple"] == -1.5
        assert row["realized_pnl"] == -10.0


class TestEmpty:
    def test_the_keys_are_present_with_nulls_rather_than_fabricated_zeros(self):
        """An average of nothing is not 0.0 -- that would render as "your
        average overrun is 0R", a claim about trades that do not exist. Same
        None-not-zero contract as avg_r and avg_slippage."""
        result = compute_stop_integrity([])

        assert result["overrun_count"] == 0
        assert result["avg_overrun_r"] is None
        assert result["worst_overrun_r"] is None
        assert result["recoverable_r"] == 0.0
        assert result["worst_overruns"] == []

    def test_it_rides_along_on_the_advanced_payload(self):
        from services.analytics import compute_advanced_metrics

        payload = compute_advanced_metrics([loss(-1.5)])

        assert payload["stop_integrity"]["overrun_count"] == 1
