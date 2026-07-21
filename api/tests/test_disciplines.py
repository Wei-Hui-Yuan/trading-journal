"""Discipline rules, and what following them is worth.

`compute_discipline_breakdown` is the payoff of migration 014: the difference
between a checklist that records ticks and one that tells you which rules are
earning their place.

Two properties matter more than the arithmetic, and both encode decisions that
would be easy to "simplify" back into bugs:

  * A rule a trade was never reviewed against must land on NEITHER side. The
    obvious shortcut -- treating a missing answer as False -- would let an
    unreviewed backlog read as indiscipline and drag every rule's compliance
    toward zero.
  * `edge` must be None, never 0.0, when one side is empty. A rule followed on
    every single trade has no counterfactual, and reporting "no difference" is
    a stronger claim than the data can support.
"""

import os
import uuid
from decimal import Decimal

import pytest

os.environ.setdefault("CORS_ALLOW_ORIGINS", "https://trading-journal-test.vercel.app")

import main  # noqa: E402
from services.analytics import (  # noqa: E402
    ReviewedTrade,
    compute_discipline_breakdown,
)


# ---------------------------------------------------------------------------
# Rule payloads
# ---------------------------------------------------------------------------


def test_discipline_create_trims_whitespace():
    assert main.DisciplineCreate(name="  Followed plan  ").name == "Followed plan"


def test_discipline_create_rejects_empty_name():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        main.DisciplineCreate(name="   ")


def test_discipline_out_model_validation():
    d_id = uuid.uuid4()
    d_out = main.DisciplineOut(id=d_id, name="Hard Stop", created_at=None)
    assert d_out.id == d_id
    assert d_out.name == "Hard Stop"


def test_review_payload_accepts_answers_keyed_by_id():
    """Keyed by id, not name: a renamed rule must keep its history."""
    rule_id = uuid.uuid4()
    payload = main.PositionReviewUpdate(disciplines={rule_id: True})
    assert payload.disciplines == {rule_id: True}


def test_review_payload_without_disciplines_leaves_them_unset():
    """Omission must stay distinguishable from an empty answer set."""
    payload = main.PositionReviewUpdate(trade_grade="A")
    assert "disciplines" not in payload.model_dump(exclude_unset=True)


# ---------------------------------------------------------------------------
# The breakdown
# ---------------------------------------------------------------------------


def trade(tid, pnl, disciplines):
    return ReviewedTrade(
        trade_id=tid,
        ticker="ZZT",
        direction="BUY",
        quantity=1,
        actual_entry=Decimal("100"),
        exit_price=Decimal("110") if pnl > 0 else Decimal("90"),
        planned_entry=None,
        stop_loss=None,
        mistakes=[],
        review_status="reviewed",
        disciplines=disciplines,
        realized_pnl=Decimal(str(pnl)),
    )


def by_name(rows, name):
    return next(r for r in rows if r["discipline"] == name)


def test_splits_trades_by_whether_the_rule_was_followed():
    rows = compute_discipline_breakdown(
        [
            trade("1", 100, {"Followed the plan": True}),
            trade("2", 50, {"Followed the plan": True}),
            trade("3", -80, {"Followed the plan": False}),
        ],
        {},
    )
    row = by_name(rows, "Followed the plan")
    assert row["followed"]["trade_count"] == 2
    assert row["not_followed"]["trade_count"] == 1


def test_win_rate_comes_from_realised_pnl():
    """Not from R -- otherwise the panel stays empty until stops are backfilled."""
    rows = compute_discipline_breakdown(
        [
            trade("1", 100, {"Rule": True}),
            trade("2", -10, {"Rule": True}),
            trade("3", -80, {"Rule": False}),
        ],
        {},
    )
    row = by_name(rows, "Rule")
    assert row["followed"]["win_rate_pct"] == 50.0
    assert row["not_followed"]["win_rate_pct"] == 0.0


def test_edge_is_the_gap_between_the_two_sides():
    rows = compute_discipline_breakdown(
        [
            trade("1", 100, {"Rule": True}),
            trade("2", 100, {"Rule": True}),
            trade("3", -80, {"Rule": False}),
        ],
        {},
    )
    assert by_name(rows, "Rule")["edge_win_rate_pct"] == 100.0


def test_a_rule_with_no_edge_reports_zero_not_none():
    """Zero edge is a real finding and must be distinguishable from unknown."""
    rows = compute_discipline_breakdown(
        [
            trade("1", 100, {"Rule": True}),
            trade("2", 100, {"Rule": False}),
        ],
        {},
    )
    assert by_name(rows, "Rule")["edge_win_rate_pct"] == 0.0


def test_unanswered_rules_count_on_neither_side():
    """The whole reason answers live in a join table keyed by position."""
    rows = compute_discipline_breakdown(
        [
            trade("1", 100, {"Answered": True}),
            trade("2", -50, {}),  # reviewed, but never against "Answered"
        ],
        {},
    )
    row = by_name(rows, "Answered")
    assert row["followed"]["trade_count"] == 1
    assert row["not_followed"]["trade_count"] == 0
    assert row["sample"] == 1


def test_edge_is_none_when_one_side_is_empty():
    rows = compute_discipline_breakdown(
        [
            trade("1", 100, {"Always followed": True}),
            trade("2", 60, {"Always followed": True}),
        ],
        {},
    )
    row = by_name(rows, "Always followed")
    assert row["edge_win_rate_pct"] is None
    assert row["edge_r"] is None
    assert row["not_followed"]["win_rate_pct"] is None


def test_open_trades_without_pnl_are_excluded():
    """A round trip with no realised P&L cannot be placed on either side."""
    open_trade = ReviewedTrade(
        trade_id="2",
        ticker="ZZT",
        direction="BUY",
        quantity=1,
        actual_entry=Decimal("100"),
        exit_price=None,
        planned_entry=None,
        stop_loss=None,
        mistakes=[],
        review_status=None,
        disciplines={"Rule": False},
        realized_pnl=None,
    )
    rows = compute_discipline_breakdown(
        [trade("1", 100, {"Rule": True}), open_trade], {}
    )
    row = by_name(rows, "Rule")
    assert row["sample"] == 1
    assert row["not_followed"]["trade_count"] == 0


def test_avg_r_uses_only_scoreable_trades_and_reports_its_sample():
    """One scored trade must not look like a verdict on three."""
    trades = [
        trade("1", 100, {"Rule": True}),
        trade("2", 80, {"Rule": True}),
        trade("3", 60, {"Rule": True}),
    ]
    row = by_name(compute_discipline_breakdown(trades, {"1": 2.0}), "Rule")
    assert row["followed"]["trade_count"] == 3
    assert row["followed"]["r_sample"] == 1
    assert row["followed"]["avg_r"] == 2.0


def test_avg_r_is_none_when_nothing_can_be_scored():
    """The live journal's normal state: no stops recorded, so no R at all."""
    row = by_name(
        compute_discipline_breakdown([trade("1", 100, {"Rule": True})], {}), "Rule"
    )
    assert row["followed"]["avg_r"] is None
    assert row["followed"]["r_sample"] == 0
    # ...but the win rate still works, which is the entire point.
    assert row["followed"]["win_rate_pct"] == 100.0


def test_rules_with_no_counterfactual_sort_last():
    """An unmeasurable rule must not outrank a measured one by defaulting to 0."""
    rows = compute_discipline_breakdown(
        [
            trade("1", 100, {"Measured": True, "Unmeasured": True}),
            trade("2", -50, {"Measured": False}),
        ],
        {},
    )
    assert [r["discipline"] for r in rows] == ["Measured", "Unmeasured"]


def test_empty_input_produces_no_rows():
    assert compute_discipline_breakdown([], {}) == []
