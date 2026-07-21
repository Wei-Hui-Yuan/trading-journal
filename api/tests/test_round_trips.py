"""Scoring a round trip in R.

`_score_r` is the number the journal puts in front of the trader, and it
deliberately delegates to the analytics implementation rather than restating
the formula. These tests pin both halves of that: the arithmetic, and the
refusal to guess.

The refusal matters more than it looks. Returning 0.0 for "cannot be scored"
would be indistinguishable from a scratch trade, and every aggregate built on
top -- expectancy, average R, the R distribution -- would quietly absorb the
unscoreable trades as break-evens.
"""

import os
from decimal import Decimal

import pytest

os.environ.setdefault("CORS_ALLOW_ORIGINS", "https://example.test")

import main  # noqa: E402


def score(direction, entry, exit_price, stop):
    return main._score_r(
        direction,
        Decimal(str(entry)) if entry is not None else None,
        Decimal(str(exit_price)) if exit_price is not None else None,
        Decimal(str(stop)) if stop is not None else None,
    )


# --- arithmetic ---------------------------------------------------------


def test_long_winner_scores_positive_r():
    # risk 2 (100 -> 98), reward 4 (100 -> 104)
    assert score("BUY", 100, 104, 98) == 2.0


def test_long_loser_stopped_at_exactly_one_r():
    assert score("BUY", 100, 98, 98) == -1.0


def test_short_winner_scores_positive_r():
    # short from 100, stop 102 (risk 2), covered at 96 (reward 4)
    assert score("SELL", 100, 96, 102) == 2.0


def test_short_loser_scores_negative_r():
    assert score("SELL", 100, 102, 102) == -1.0


def test_crwd_round_trip_matches_hand_calculation():
    """The real case that motivated the feature.

    Two buys at 696.87 and two exits averaging 690.18, stop at 690.
    reward = 690.18 - 696.87 = -6.69 ; risk = 696.87 - 690 = 6.87
    """
    assert score("BUY", 696.87, 690.18, 690) == pytest.approx(-0.9738, abs=1e-4)


def test_scratch_trade_scores_zero_not_none():
    """Zero is a real result and must survive as one."""
    assert score("BUY", 100, 100, 98) == 0.0


# --- refusal to guess ---------------------------------------------------


def test_no_stop_is_unscoreable():
    """The reason 12 of 13 live round trips scored as None before migration 012."""
    assert score("BUY", 100, 104, None) is None


def test_open_trade_is_unscoreable():
    assert score("BUY", 100, None, 98) is None


def test_stop_at_entry_is_unscoreable():
    """Zero risk is not zero reward -- dividing by it would be meaningless."""
    assert score("BUY", 100, 104, 100) is None


def test_stop_on_the_wrong_side_is_unscoreable():
    """A long with a stop above entry defines no downside to measure against."""
    assert score("BUY", 100, 104, 105) is None


def test_unscoreable_is_none_never_zero():
    """Guards the distinction every R aggregate depends on."""
    for args in (
        ("BUY", 100, 104, None),
        ("BUY", 100, None, 98),
        ("BUY", 100, 104, 100),
    ):
        assert score(*args) is not 0.0  # noqa: F632 - identity is the point
        assert score(*args) is None


# --- plan fields --------------------------------------------------------


def test_plan_fields_of_missing_trade_is_empty():
    """An orphaned position must not crash the journal."""
    assert main._plan_fields(None) == {}


def test_round_trip_defaults_leave_every_optional_unset():
    """A row with no plan and no review still validates."""
    rt = main.RoundTripOut(
        kind="open",
        key="open:ZZT",
        symbol="ZZT",
        direction="BUY",
        quantity=1.0,
        entry_price=10.0,
        entry_time=main.datetime.now(),
        execution_count=1,
    )
    assert rt.r_multiple is None
    assert rt.mistakes == []
    assert rt.fills == []
    assert rt.position_id is None


def test_conviction_is_bounded_to_the_scale():
    """Out-of-range conviction would silently poison the conviction/R link."""
    from pydantic import ValidationError

    for bad in (0, 6, -1):
        with pytest.raises(ValidationError):
            main.TradeAnnotationUpdate(conviction=bad)

    assert main.TradeAnnotationUpdate(conviction=3).conviction == 3
    # Unrated must stay expressible.
    assert main.TradeAnnotationUpdate().conviction is None
