"""Compliance as a single number, and whether it correlates with the outcome.

`compute_discipline_breakdown` (see test_disciplines.py) answers "is this ONE
rule worth following". This file covers the coarser question the same answers
support: for a trade with several rules answered, what share were followed,
and does that share -- 100%, 80%, 50% -- track the win rate at all.

The property that matters most, and the one every test here ultimately
protects: a trade never checked against any rule must be invisible to this
whole feature, not scored as 0% and lumped in with genuine indiscipline. That
is the same rule PositionDiscipline's own docstring states for a single
answer, applied here to the trade as a whole.
"""

import os
from decimal import Decimal

os.environ.setdefault("CORS_ALLOW_ORIGINS", "https://trading-journal-test.vercel.app")

from services.analytics import (  # noqa: E402
    COMPLIANCE_BUCKET_ORDER,
    ReviewedTrade,
    _compliance_bucket,
    compute_compliance_buckets,
    compute_discipline_score,
)


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


def by_bucket(rows, label):
    return next(r for r in rows if r["compliance"] == label)


# ---------------------------------------------------------------------------
# compute_discipline_score
# ---------------------------------------------------------------------------


def test_all_rules_followed_scores_100():
    assert compute_discipline_score({"a": True, "b": True}) == 100.0


def test_no_rules_followed_scores_0():
    assert compute_discipline_score({"a": False, "b": False}) == 0.0


def test_a_mixed_answer_set_scores_the_share_followed():
    assert compute_discipline_score({"a": True, "b": True, "c": False}) == round(
        2 / 3 * 100, 2
    )


def test_an_unanswered_trade_scores_none_not_zero():
    """The whole point. A trade never reviewed against any rule has no
    compliance to report, and 0% would count a review backlog as total
    indiscipline -- exactly the trap the single-rule case already avoids."""
    assert compute_discipline_score({}) is None


def test_score_only_counts_rules_this_trade_was_actually_answered_against():
    """A rule added after this trade closed has no opinion about it. Scoring
    against "however many rules exist today" would fail every historical
    trade on rules it never had the chance to see."""
    assert compute_discipline_score({"only_rule_at_the_time": True}) == 100.0


# ---------------------------------------------------------------------------
# _compliance_bucket boundaries
# ---------------------------------------------------------------------------


def test_bucket_boundaries():
    assert _compliance_bucket(100) == "100%"
    assert _compliance_bucket(99.99) == "80-99%"
    assert _compliance_bucket(80) == "80-99%"
    assert _compliance_bucket(79.99) == "50-79%"
    assert _compliance_bucket(50) == "50-79%"
    assert _compliance_bucket(49.99) == "<50%"
    assert _compliance_bucket(0) == "<50%"


# ---------------------------------------------------------------------------
# compute_compliance_buckets
# ---------------------------------------------------------------------------


def test_every_bucket_is_always_present_even_when_empty():
    """Matches _r_distribution's own precedent: a chart should never have to
    special-case a bucket that happens to be empty right now."""
    rows = compute_compliance_buckets([], {})
    assert [r["compliance"] for r in rows] == list(COMPLIANCE_BUCKET_ORDER)
    for row in rows:
        assert row["trade_count"] == 0
        assert row["win_rate_pct"] is None


def test_a_perfectly_compliant_trade_lands_in_the_100_bucket():
    rows = compute_compliance_buckets(
        [trade("1", 100, {"a": True, "b": True})], {}
    )
    assert by_bucket(rows, "100%")["trade_count"] == 1
    assert by_bucket(rows, "<50%")["trade_count"] == 0


def test_win_rate_is_computed_within_each_bucket_independently():
    """Reproduces the headline scenario this feature exists to surface: does
    perfect compliance actually win more often than sloppy compliance."""
    rows = compute_compliance_buckets(
        [
            trade("1", 100, {"a": True, "b": True}),   # 100%, win
            trade("2", 50, {"a": True, "b": True}),    # 100%, win
            trade("3", -10, {"a": True, "b": True}),   # 100%, loss
            trade("4", -20, {"a": False, "b": False}), # 0%, loss
            trade("5", -30, {"a": False, "b": False}), # 0%, loss
            trade("6", 10, {"a": False, "b": False}),  # 0%, win
        ],
        {},
    )
    full = by_bucket(rows, "100%")
    assert full["trade_count"] == 3
    assert full["win_rate_pct"] == round(2 / 3 * 100, 2)

    none = by_bucket(rows, "<50%")
    assert none["trade_count"] == 3
    assert none["win_rate_pct"] == round(1 / 3 * 100, 2)


def test_an_unreviewed_trade_is_excluded_from_every_bucket():
    """Not "<50%" -- absent entirely. A trade with no answers is missing
    data, not a trade that broke every rule."""
    rows = compute_compliance_buckets(
        [trade("1", 100, {})],
        {},
    )
    assert sum(r["trade_count"] for r in rows) == 0


def test_an_open_trade_is_excluded_even_if_reviewed():
    """realized_pnl is None for anything still open; there is no outcome yet
    for a bucket's win rate to be computed from."""
    open_trade = ReviewedTrade(
        trade_id="1",
        ticker="ZZT",
        direction="BUY",
        quantity=1,
        actual_entry=Decimal("100"),
        exit_price=None,
        planned_entry=None,
        stop_loss=None,
        mistakes=[],
        review_status="reviewed",
        disciplines={"a": True},
        realized_pnl=None,
    )
    rows = compute_compliance_buckets([open_trade], {})
    assert sum(r["trade_count"] for r in rows) == 0


def test_r_sample_only_counts_trades_with_a_computable_r():
    rows = compute_compliance_buckets(
        [
            trade("1", 100, {"a": True}),
            trade("2", 50, {"a": True}),
        ],
        {"1": 2.0},  # trade 2 has no stop, so no R
    )
    full = by_bucket(rows, "100%")
    assert full["trade_count"] == 2
    assert full["r_sample"] == 1
    assert full["avg_r"] == 2.0


def test_boundary_trades_land_in_the_bucket_their_own_score_implies():
    """80% and 50% are inclusive lower bounds, exercised end to end rather
    than just on the bucketing function in isolation."""
    rows = compute_compliance_buckets(
        [
            trade("1", 10, {"a": True, "b": True, "c": True, "d": True, "e": False}),  # 80%
            trade("2", 10, {"a": True, "b": False}),  # 50%
        ],
        {},
    )
    assert by_bucket(rows, "80-99%")["trade_count"] == 1
    assert by_bucket(rows, "50-79%")["trade_count"] == 1
