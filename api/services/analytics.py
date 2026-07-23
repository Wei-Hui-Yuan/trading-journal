"""Dashboard analytics: core performance stats and the day/session heatmap.

Reads closed positions out of the `positions` table and aggregates them into a
payload the frontend can map straight onto a grid.

All money math stays in `Decimal` internally and is only widened to `float` at
the response boundary, so rounding never accumulates through the aggregation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# US equities trade on Eastern time; broker timestamps are stored in UTC, so
# every bucketing decision has to happen after converting into this zone.
# ZoneInfo (not a fixed offset) is required so DST is handled correctly.
MARKET_TZ = ZoneInfo("America/New_York")

# Session boundaries, in Eastern local time.
SESSION_MORNING = "Morning"
SESSION_MIDDAY = "Midday"
SESSION_AFTERNOON = "Afternoon"
SESSION_AFTER_HOURS = "After-Hours"

# Ordered for direct rendering as heatmap columns.
SESSIONS: list[str] = [
    SESSION_MORNING,
    SESSION_MIDDAY,
    SESSION_AFTERNOON,
    SESSION_AFTER_HOURS,
]

# Ordered for direct rendering as heatmap rows. Weekend entries are dropped:
# US equities do not trade Sat/Sun, so a weekend timestamp is bad data.
DAYS: list[str] = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]

_MARKET_OPEN = time(9, 30)
_MIDDAY_START = time(12, 0)
_AFTERNOON_START = time(14, 0)
_CLOSE = time(16, 0)


def classify_session(entry_time: datetime) -> tuple[Optional[str], Optional[str]]:
    """Map a position's entry timestamp to (day_name, session_name).

    Returns (None, None) when the entry falls outside the Mon-Fri grid, so
    callers can count it as excluded rather than silently misfiling it.

    Boundaries are half-open and lower-inclusive: 12:00 belongs to Midday, not
    Morning; 16:00 belongs to After-Hours, not Afternoon.

    Pre-market entries (before 09:30 ET) are folded into After-Hours. The spec
    defines no pre-open bucket, and grouping them with the other
    outside-regular-hours trades keeps every row on the grid accounted for.
    """
    # A naive timestamp is assumed UTC -- that is what the DB column stores.
    if entry_time.tzinfo is None:
        entry_time = entry_time.replace(tzinfo=timezone.utc)

    local = entry_time.astimezone(MARKET_TZ)

    weekday = local.weekday()  # Monday == 0
    if weekday > 4:
        return None, None

    clock = local.time()
    if clock < _MARKET_OPEN:
        session = SESSION_AFTER_HOURS  # pre-market
    elif clock < _MIDDAY_START:
        session = SESSION_MORNING
    elif clock < _AFTERNOON_START:
        session = SESSION_MIDDAY
    elif clock < _CLOSE:
        session = SESSION_AFTERNOON
    else:
        session = SESSION_AFTER_HOURS

    return DAYS[weekday], session


@dataclass(frozen=True)
class ClosedPosition:
    """A closed round trip, decoupled from the ORM row it came from."""

    realized_pnl: Decimal
    entry_price: Decimal
    quantity: Decimal
    entry_time: datetime
    # When the P&L was actually realised. Distinct from entry_time and not
    # interchangeable with it: the heatmap asks "which session do I trade
    # well?" and belongs on entry, while an equity curve plots money hitting
    # the account and belongs on exit. A trade opened in March and closed in
    # July moved the balance in July.
    #
    # Optional because a position row could in principle lack it; such rows are
    # dropped from the curve rather than dated by a guess.
    exit_time: Optional[datetime] = None


@dataclass
class _Bucket:
    """Running totals for one heatmap cell."""

    trade_count: int = 0
    net_pnl: Decimal = Decimal("0")
    wins: int = 0
    gross_profit: Decimal = Decimal("0")
    gross_loss: Decimal = Decimal("0")  # stored positive

    def add(self, position: ClosedPosition) -> None:
        self.trade_count += 1
        self.net_pnl += position.realized_pnl
        if position.realized_pnl > 0:
            self.wins += 1
            self.gross_profit += position.realized_pnl
        elif position.realized_pnl < 0:
            self.gross_loss += -position.realized_pnl

    def as_dict(self) -> dict[str, Any]:
        win_rate = (
            round(self.wins / self.trade_count * 100, 2) if self.trade_count else 0.0
        )

        # Same contract as core_stats.profit_factor: null means "no losing
        # trades, ratio unbounded". Never float('inf') -- that is not valid
        # JSON and Starlette's encoder rejects it.
        #
        # This is not derivable from win_rate on the client: a scratch trade
        # (P&L exactly 0) is neither a win nor a loss, so a cell can have zero
        # losses while its win rate sits below 100%.
        if self.gross_loss > 0:
            profit_factor: Optional[float] = float(
                round(self.gross_profit / self.gross_loss, 2)
            )
        elif self.gross_profit > 0:
            profit_factor = None
        else:
            profit_factor = 0.0

        return {
            "trade_count": self.trade_count,
            "net_pnl": float(self.net_pnl),
            "win_rate_pct": win_rate,
            "profit_factor": profit_factor,
        }


def compute_core_stats(positions: list[ClosedPosition]) -> dict[str, Any]:
    """Headline performance numbers across every closed position."""
    total_trades = len(positions)
    if not total_trades:
        return {
            "net_pnl": 0.0,
            "win_rate_pct": 0.0,
            "total_trades": 0,
            "profit_factor": 0.0,
            "avg_roi_pct": 0.0,
        }

    net_pnl = Decimal("0")
    gross_profit = Decimal("0")  # sum of winning P&L
    gross_loss = Decimal("0")  # sum of |losing P&L|
    wins = 0
    roi_sum = Decimal("0")
    roi_count = 0

    for position in positions:
        pnl = position.realized_pnl
        net_pnl += pnl

        if pnl > 0:
            gross_profit += pnl
            wins += 1
        elif pnl < 0:
            gross_loss += -pnl

        # ROI is measured against capital committed at entry. Guard the
        # denominator: a zero entry price or quantity would otherwise raise.
        cost_basis = position.entry_price * position.quantity
        if cost_basis > 0:
            roi_sum += pnl / cost_basis * Decimal("100")
            roi_count += 1

    # Profit factor is gross wins / gross losses, which is undefined when there
    # are no losses. It must NOT be float('inf'): JSON has no infinity literal,
    # so Starlette's encoder (allow_nan=False) raises and the endpoint 500s.
    # `None` serializes to null; the UI renders that as an unbounded ratio.
    if gross_loss > 0:
        profit_factor: Optional[float] = float(round(gross_profit / gross_loss, 2))
    elif gross_profit > 0:
        profit_factor = None  # wins, no losses -> unbounded
    else:
        profit_factor = 0.0  # no closed P&L either way

    return {
        "net_pnl": float(net_pnl),
        "win_rate_pct": round(wins / total_trades * 100, 2),
        "total_trades": total_trades,
        "profit_factor": profit_factor,
        "avg_roi_pct": float(round(roi_sum / roi_count, 2)) if roi_count else 0.0,
    }


def build_heatmap(positions: Iterable[ClosedPosition]) -> dict[str, Any]:
    """Aggregate positions into a Mon-Fri x session grid.

    Every cell is pre-seeded so the frontend can index `cells[day][session]`
    without null checks, and the grid renders at a stable size even when sparse.
    """
    cells: dict[str, dict[str, _Bucket]] = {
        day: {session: _Bucket() for session in SESSIONS} for day in DAYS
    }

    excluded_weekend = 0

    for position in positions:
        day, session = classify_session(position.entry_time)
        if day is None or session is None:
            excluded_weekend += 1
            continue
        cells[day][session].add(position)

    # Row/column totals let the UI render margins without recomputing.
    day_totals = {
        day: {
            "trade_count": sum(cells[day][s].trade_count for s in SESSIONS),
            "net_pnl": float(sum((cells[day][s].net_pnl for s in SESSIONS), Decimal("0"))),
        }
        for day in DAYS
    }
    session_totals = {
        session: {
            "trade_count": sum(cells[d][session].trade_count for d in DAYS),
            "net_pnl": float(sum((cells[d][session].net_pnl for d in DAYS), Decimal("0"))),
        }
        for session in SESSIONS
    }

    return {
        "days": DAYS,
        "sessions": SESSIONS,
        "cells": {
            day: {session: cells[day][session].as_dict() for session in SESSIONS}
            for day in DAYS
        },
        "day_totals": day_totals,
        "session_totals": session_totals,
        "timezone": str(MARKET_TZ),
        "excluded_weekend_trades": excluded_weekend,
    }


async def load_closed_positions(session: AsyncSession) -> list[ClosedPosition]:
    """Read every closed position out of the database.

    Imported locally so this module never participates in an import cycle with
    the FastAPI app that calls it.
    """
    from main import Position  # noqa: PLC0415 - deferred to avoid circular import

    rows = (await session.execute(select(Position))).scalars().all()

    return [
        ClosedPosition(
            realized_pnl=Decimal(str(row.realized_pnl)),
            entry_price=Decimal(str(row.entry_price)),
            quantity=Decimal(str(row.quantity)),
            entry_time=row.entry_time,
            exit_time=row.exit_time,
        )
        for row in rows
        if row.realized_pnl is not None and row.entry_time is not None
    ]


def build_equity_curve(positions: Iterable[ClosedPosition]) -> dict[str, Any]:
    """Cumulative realised P&L over time, with the drawdown it went through.

    NOT account equity, and deliberately not called that in the response. True
    equity needs a starting balance plus every deposit and withdrawal, none of
    which the broker feed carries -- inventing one by working backwards from
    today's account size would silently assume the account was never funded
    twice. This is the sum of closed P&L, which the data does support.

    Dated by EXIT time. A trade opened in March and closed in July moved the
    balance in July, and dating it by entry would draw a curve that recovered
    before the trade that recovered it.

    Every calendar day between the first and last close gets a point, including
    weekends. The gaps are the point: a chart that plots only trading days
    compresses a three-month pause into one step, and "how fast did it recover"
    is unanswerable if the x-axis is not proportional to time.
    """
    dated = [
        (p.exit_time.astimezone(MARKET_TZ).date(), p.realized_pnl)
        for p in positions
        if p.exit_time is not None
    ]

    if not dated:
        return {
            "points": [],
            "summary": {
                "start_date": None,
                "end_date": None,
                "net_pnl": 0.0,
                "peak_pnl": 0.0,
                "max_drawdown": 0.0,
                "current_drawdown": 0.0,
                "trading_days": 0,
                "calendar_days": 0,
                "closed_trades": 0,
            },
        }

    daily_pnl: dict[Any, Decimal] = {}
    daily_count: dict[Any, int] = {}
    for day, pnl in dated:
        daily_pnl[day] = daily_pnl.get(day, Decimal("0")) + pnl
        daily_count[day] = daily_count.get(day, 0) + 1

    first_day, last_day = min(daily_pnl), max(daily_pnl)

    points: list[dict[str, Any]] = []
    cumulative = Decimal("0")
    peak = Decimal("0")
    max_drawdown = Decimal("0")

    # Anchored at zero the day before the first close, so the first day reads
    # as a move rather than as a starting level.
    points.append(
        {
            "date": (first_day - timedelta(days=1)).isoformat(),
            "realized_pnl": 0.0,
            "cumulative_pnl": 0.0,
            "peak_pnl": 0.0,
            "drawdown": 0.0,
            "trades": 0,
        }
    )

    day = first_day
    while day <= last_day:
        realized = daily_pnl.get(day, Decimal("0"))
        cumulative += realized
        # Peak floors at zero: before the curve has ever been profitable there
        # is no high-water mark to be below, and measuring drawdown from a
        # negative peak would report a losing account as having recovered.
        peak = max(peak, cumulative)
        drawdown = cumulative - peak  # <= 0
        max_drawdown = min(max_drawdown, drawdown)

        points.append(
            {
                "date": day.isoformat(),
                "realized_pnl": float(realized),
                "cumulative_pnl": float(cumulative),
                "peak_pnl": float(peak),
                # Signed negative, so "deeper" is unambiguously "lower" on both
                # the chart and the number.
                "drawdown": float(drawdown),
                "trades": daily_count.get(day, 0),
            }
        )
        day += timedelta(days=1)

    return {
        "points": points,
        "summary": {
            "start_date": first_day.isoformat(),
            "end_date": last_day.isoformat(),
            "net_pnl": float(cumulative),
            "peak_pnl": float(peak),
            "max_drawdown": float(max_drawdown),
            "current_drawdown": float(cumulative - peak),
            "trading_days": len(daily_pnl),
            "calendar_days": (last_day - first_day).days + 1,
            "closed_trades": len(dated),
        },
    }


async def build_dashboard(session: AsyncSession) -> dict[str, Any]:
    """Full dashboard payload: headline stats, the heatmap, the equity curve."""
    positions = await load_closed_positions(session)
    return {
        "core_stats": compute_core_stats(positions),
        "heatmap": build_heatmap(positions),
        "equity_curve": build_equity_curve(positions),
    }


# ---------------------------------------------------------------------------
# Advanced trade-level performance metrics
# ---------------------------------------------------------------------------
#
# These read the `trades` ledger rather than `positions`, because R-multiple
# and slippage need the PLAN -- stop_loss and planned_entry -- and those live
# only on trades. `positions` records what happened, not what was intended.


@dataclass(frozen=True)
class ReviewedTrade:
    """One closed execution with enough plan data to score."""

    trade_id: str
    ticker: str
    direction: str  # BUY (long) / SELL (short)
    quantity: int
    actual_entry: Decimal
    exit_price: Optional[Decimal]
    planned_entry: Optional[Decimal]
    stop_loss: Optional[Decimal]
    mistakes: list[str]
    review_status: Optional[str]
    # Rule name -> whether it was followed on this trade (migration 014).
    # Rules the trade was never reviewed against are absent, not False.
    disciplines: dict[str, bool] = field(default_factory=dict)
    # Needed by the discipline breakdown, which measures win rate from money
    # so it works on trades that carry no stop and cannot be scored in R.
    realized_pnl: Optional[Decimal] = None
    # The playbook entry this trade followed, resolved to a name so the
    # breakdown reads as the trader wrote it. None means unassigned, which is
    # reported as its own bucket rather than dropped -- a large unassigned pile
    # is itself worth seeing.
    strategy: Optional[str] = None
    entry_time: Optional[datetime] = None


def compute_r_multiple(trade: ReviewedTrade) -> Optional[float]:
    """Realized reward measured in units of the risk actually taken.

    Long:  (exit - entry) / (entry - stop)
    Short: (entry - exit) / (stop - entry)

    Returns None -- never 0.0 -- when the trade cannot be scored: no exit, no
    stop, or a stop at the entry price. Zero is a real R value (a scratch),
    so conflating "no risk defined" with "broke even" would corrupt every
    aggregate built on top of this.
    """
    if trade.exit_price is None or trade.stop_loss is None:
        return None

    is_long = (trade.direction or "").upper() == "BUY"

    if is_long:
        reward = trade.exit_price - trade.actual_entry
        risk = trade.actual_entry - trade.stop_loss
    else:
        reward = trade.actual_entry - trade.exit_price
        risk = trade.stop_loss - trade.actual_entry

    # A non-positive denominator means the stop was at or beyond the entry --
    # there was no defined risk to measure the return against.
    if risk <= 0:
        return None

    return float(round(reward / risk, 4))


def compute_slippage(trade: ReviewedTrade) -> Optional[float]:
    """Difference between the fill and the plan, signed against the trader.

    Positive = worse than planned (paid up on a long, sold lower on a short).
    Returns None when no entry was planned.
    """
    if trade.planned_entry is None:
        return None

    is_long = (trade.direction or "").upper() == "BUY"
    diff = (
        trade.actual_entry - trade.planned_entry
        if is_long
        else trade.planned_entry - trade.actual_entry
    )
    return float(round(diff, 4))


def compute_expectancy(r_multiples: list[float]) -> Optional[float]:
    """(win rate x avg win R) - (loss rate x avg loss R).

    Expressed in R, so it answers "what do I earn per unit risked?".
    Returns None on an empty sample rather than a misleading 0.0.
    """
    if not r_multiples:
        return None

    wins = [r for r in r_multiples if r > 0]
    losses = [abs(r) for r in r_multiples if r < 0]
    total = len(r_multiples)

    win_rate = len(wins) / total
    loss_rate = len(losses) / total
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0

    return round(win_rate * avg_win - loss_rate * avg_loss, 4)


def _r_distribution(r_multiples: list[float]) -> dict[str, int]:
    """Bucket R outcomes so the UI can render a distribution without binning."""
    buckets = {"<-2R": 0, "-2R..-1R": 0, "-1R..0R": 0, "0R..1R": 0, "1R..2R": 0, ">2R": 0}
    for r in r_multiples:
        if r < -2:
            buckets["<-2R"] += 1
        elif r < -1:
            buckets["-2R..-1R"] += 1
        elif r < 0:
            buckets["-1R..0R"] += 1
        elif r < 1:
            buckets["0R..1R"] += 1
        elif r <= 2:
            buckets["1R..2R"] += 1
        else:
            buckets[">2R"] += 1
    return buckets


def _side_stats(pnls: list[float], r_multiples: list[float]) -> dict[str, Any]:
    """One side of a discipline split.

    Win rate comes from realised P&L and is therefore available for every
    closed round trip. Average R comes only from the subset that can be scored
    -- it needs a stop to divide by -- so `r_sample` reports how many trades
    actually stand behind `avg_r` rather than letting one scored trade look
    like a verdict on twenty.
    """
    if not pnls:
        return {
            "trade_count": 0,
            "win_rate_pct": None,
            "avg_r": None,
            "r_sample": 0,
        }
    wins = sum(1 for p in pnls if p > 0)
    return {
        "trade_count": len(pnls),
        "win_rate_pct": round(wins / len(pnls) * 100, 2),
        "avg_r": (
            round(sum(r_multiples) / len(r_multiples), 4) if r_multiples else None
        ),
        "r_sample": len(r_multiples),
    }


def compute_discipline_breakdown(
    trades: list[ReviewedTrade],
    r_by_id: dict[str, float],
) -> list[dict[str, Any]]:
    """What each of the trader's own rules is actually worth.

    The question a journal exists to answer is not "did I follow my plan on
    this trade" but "what happens when I do". Each rule is split into the
    trades that honoured it and the trades that did not, so the two can be
    compared directly.

    Deliberately driven by realised P&L rather than R. Scoring in R alone
    would need a stop on every trade, and a journal whose habit analysis stays
    empty until the user backfills stops across their entire history is a
    feature nobody ever sees. R is still reported where it can be computed,
    alongside the sample it rests on.

    Trades never reviewed against a rule appear on NEITHER side -- they are
    absent from the mapping rather than defaulting to False, so an unreviewed
    backlog cannot masquerade as a discipline failure.

    `edge_*` is None unless both sides have trades: a rule followed every
    single time has no counterfactual, and manufacturing one from an empty
    sample would be the most flattering possible lie.
    """
    followed: dict[str, dict[str, list[float]]] = {}
    broken: dict[str, dict[str, list[float]]] = {}

    for trade in trades:
        if trade.realized_pnl is None:
            continue
        pnl = float(trade.realized_pnl)
        r = r_by_id.get(trade.trade_id)
        for name, was_followed in (trade.disciplines or {}).items():
            side = followed if was_followed else broken
            bucket = side.setdefault(name, {"pnl": [], "r": []})
            bucket["pnl"].append(pnl)
            if r is not None:
                bucket["r"].append(r)

    breakdown: list[dict[str, Any]] = []
    for name in sorted(set(followed) | set(broken)):
        y = followed.get(name, {"pnl": [], "r": []})
        n = broken.get(name, {"pnl": [], "r": []})
        yes = _side_stats(y["pnl"], y["r"])
        no = _side_stats(n["pnl"], n["r"])

        both = yes["trade_count"] > 0 and no["trade_count"] > 0
        edge_win_rate = (
            round(yes["win_rate_pct"] - no["win_rate_pct"], 2) if both else None
        )
        edge_r = (
            round(yes["avg_r"] - no["avg_r"], 4)
            if yes["avg_r"] is not None and no["avg_r"] is not None
            else None
        )

        breakdown.append(
            {
                "discipline": name,
                "followed": yes,
                "not_followed": no,
                "edge_win_rate_pct": edge_win_rate,
                "edge_r": edge_r,
                "sample": yes["trade_count"] + no["trade_count"],
            }
        )

    # Biggest measured edge first; rules with no counterfactual sink to the
    # bottom rather than sorting as though their edge were zero.
    breakdown.sort(
        key=lambda b: (b["edge_win_rate_pct"] is None, -(b["edge_win_rate_pct"] or 0))
    )
    return breakdown


def compute_strategy_breakdown(
    trades: list[ReviewedTrade],
    r_by_id: dict[str, float],
) -> list[dict[str, Any]]:
    """Which playbook entries earn their place, measured in R.

    Total R is the headline rather than win rate or dollars, because it is the
    only figure that compares a strategy fairly against another. Win rate
    rewards a setup that scratches often and loses big; dollars reward whichever
    setup happened to be sized largest. Total R answers the question actually
    being asked -- for every unit of risk I committed to this setup, what came
    back.

    Both totals are reported: `total_r` is what the strategy contributed
    overall, `avg_r` is what one trade of it is worth. A setup can carry a fine
    average and still be a rounding error if it was only taken twice, so
    `trade_count` sits alongside them and the UI shows it in the label.

    Trades with no stop cannot be scored and are counted in `unscored` rather
    than silently treated as 0R -- otherwise a strategy whose trades mostly
    lack stops would be flattered toward the middle of the chart.

    Strategy is resolved through trades.strategy_id, so this stays tied to the
    playbook: rename an entry there and every figure here follows, because the
    join is on id and never on the label.
    """
    buckets: dict[str, dict[str, Any]] = {}

    for trade in trades:
        name = trade.strategy or "Unassigned"
        b = buckets.setdefault(
            name,
            {"r": [], "pnl": [], "unscored": 0, "first": None, "last": None},
        )

        r = r_by_id.get(trade.trade_id)
        if r is None:
            b["unscored"] += 1
        else:
            b["r"].append(r)
        if trade.realized_pnl is not None:
            b["pnl"].append(float(trade.realized_pnl))

        if trade.entry_time is not None:
            if b["first"] is None or trade.entry_time < b["first"]:
                b["first"] = trade.entry_time
            if b["last"] is None or trade.entry_time > b["last"]:
                b["last"] = trade.entry_time

    out: list[dict[str, Any]] = []
    for name, b in buckets.items():
        rs, pnls = b["r"], b["pnl"]
        wins = sum(1 for r in rs if r > 0)
        out.append({
            "strategy": name,
            # Every trade attributed to the setup, scoreable or not.
            "trade_count": len(rs) + b["unscored"],
            "scored": len(rs),
            "unscored": b["unscored"],
            "total_r": round(sum(rs), 4) if rs else 0.0,
            "avg_r": round(sum(rs) / len(rs), 4) if rs else None,
            "win_rate_pct": round(wins / len(rs) * 100, 2) if rs else None,
            "best_r": round(max(rs), 4) if rs else None,
            "worst_r": round(min(rs), 4) if rs else None,
            "net_pnl": round(sum(pnls), 2) if pnls else 0.0,
            "first_traded": b["first"].isoformat() if b["first"] else None,
            "last_traded": b["last"].isoformat() if b["last"] else None,
        })

    # Best contributor first, so the chart reads top-to-bottom as a ranking.
    out.sort(key=lambda s: s["total_r"], reverse=True)
    return out


def compute_advanced_metrics(trades: list[ReviewedTrade]) -> dict[str, Any]:
    """R-multiples, slippage, expectancy, and per-mistake breakdown."""
    scored: list[tuple[ReviewedTrade, float]] = []
    slippages: list[float] = []

    for trade in trades:
        r = compute_r_multiple(trade)
        if r is not None:
            scored.append((trade, r))
        s = compute_slippage(trade)
        if s is not None:
            slippages.append(s)

    r_multiples = [r for _, r in scored]

    # Profit factor in R terms. None (not 0.0) when there are no losses, so it
    # matches the contract the heatmap and core stats already use.
    gross_win_r = sum(r for r in r_multiples if r > 0)
    gross_loss_r = sum(-r for r in r_multiples if r < 0)
    if gross_loss_r > 0:
        profit_factor_r: Optional[float] = round(gross_win_r / gross_loss_r, 2)
    elif gross_win_r > 0:
        profit_factor_r = None
    else:
        profit_factor_r = 0.0

    wins = [r for r in r_multiples if r > 0]

    # Group performance by behavioural tag. One trade with several tags counts
    # toward each, so these buckets intentionally overlap.
    by_mistake: dict[str, dict[str, Any]] = {}
    for trade, r in scored:
        for tag in trade.mistakes or []:
            bucket = by_mistake.setdefault(
                tag, {"trade_count": 0, "total_r": 0.0, "wins": 0}
            )
            bucket["trade_count"] += 1
            bucket["total_r"] += r
            if r > 0:
                bucket["wins"] += 1

    mistake_breakdown = [
        {
            "mistake": tag,
            "trade_count": b["trade_count"],
            "total_r": round(b["total_r"], 4),
            "avg_r": round(b["total_r"] / b["trade_count"], 4),
            "win_rate_pct": round(b["wins"] / b["trade_count"] * 100, 2),
        }
        for tag, b in sorted(by_mistake.items(), key=lambda kv: kv[1]["total_r"])
    ]

    return {
        "scored_trades": len(r_multiples),
        "unscored_trades": len(trades) - len(r_multiples),
        "total_r": round(sum(r_multiples), 4) if r_multiples else 0.0,
        "avg_r": round(sum(r_multiples) / len(r_multiples), 4) if r_multiples else None,
        "win_rate_pct": (
            round(len(wins) / len(r_multiples) * 100, 2) if r_multiples else 0.0
        ),
        "profit_factor_r": profit_factor_r,
        "expectancy_r": compute_expectancy(r_multiples),
        "avg_slippage": (
            round(sum(slippages) / len(slippages), 4) if slippages else None
        ),
        "slippage_sample": len(slippages),
        "r_distribution": _r_distribution(r_multiples),
        "mistake_breakdown": mistake_breakdown,
        "discipline_breakdown": compute_discipline_breakdown(
            trades, {t.trade_id: r for t, r in scored}
        ),
        "strategy_breakdown": compute_strategy_breakdown(
            trades, {t.trade_id: r for t, r in scored}
        ),
    }


async def load_reviewed_trades(session: AsyncSession) -> list[ReviewedTrade]:
    """Assemble scoreable round trips by joining positions to their opening fill.

    Neither table alone is sufficient:

      * `positions` holds what happened -- entry, exit, and the qualitative
        review (notes, mistake tags, review status).
      * `trades` holds what was INTENDED -- stop_loss and planned_entry, which
        R-multiple and slippage are measured against.

    Reading exits from `trades` does not work: the matching engine writes the
    exit onto the position and leaves the execution ledger immutable, so a
    matched round trip has `trades.exit_price = NULL`.
    """
    from main import (  # noqa: PLC0415 - deferred, avoids a cycle
        Discipline,
        Position,
        PositionDiscipline,
        Strategy,
        Trade,
    )

    positions = (await session.execute(select(Position))).scalars().all()
    trades = (await session.execute(select(Trade))).scalars().all()
    trade_by_id = {str(t.id): t for t in trades}

    # Playbook names, so the breakdown reads as the trader wrote them. Joined
    # on id, so renaming an entry in the playbook carries through everywhere.
    strategy_name = {
        s.id: s.name for s in (await session.execute(select(Strategy))).scalars().all()
    }

    # Rule answers, keyed by position. One query for the whole set.
    discipline_rows = (
        await session.execute(
            select(
                PositionDiscipline.position_id,
                Discipline.name,
                PositionDiscipline.followed,
            ).join(Discipline, Discipline.id == PositionDiscipline.discipline_id)
        )
    ).all()
    disciplines_by_position: dict[str, dict[str, bool]] = {}
    for position_id, name, followed in discipline_rows:
        disciplines_by_position.setdefault(str(position_id), {})[name] = followed

    reviewed: list[ReviewedTrade] = []
    for position in positions:
        if position.entry_price is None or position.exit_price is None:
            continue

        # The opening execution carries the plan for this round trip.
        opening = trade_by_id.get(str(position.open_trade_id))

        # Direction comes from the opening fill; a position does not store it.
        direction = (opening.direction if opening else "") or "BUY"

        reviewed.append(
            ReviewedTrade(
                trade_id=str(position.id),
                ticker=position.symbol,
                direction=direction,
                quantity=int(position.quantity or 0),
                actual_entry=Decimal(str(position.entry_price)),
                exit_price=Decimal(str(position.exit_price)),
                planned_entry=(
                    Decimal(str(opening.planned_entry))
                    if opening is not None and opening.planned_entry is not None
                    else None
                ),
                stop_loss=(
                    Decimal(str(opening.stop_loss))
                    if opening is not None and opening.stop_loss is not None
                    else None
                ),
                mistakes=list(position.mistakes or []),
                review_status=position.review_status,
                disciplines=disciplines_by_position.get(str(position.id), {}),
                realized_pnl=(
                    Decimal(str(position.realized_pnl))
                    if position.realized_pnl is not None
                    else None
                ),
                # The position's own strategy wins when set (assigned during
                # review); otherwise fall back to the opening execution's,
                # which is where the journal's plan editor writes it.
                strategy=strategy_name.get(
                    position.strategy_id
                    or (opening.strategy_id if opening is not None else None)
                ),
                entry_time=position.entry_time,
            )
        )
    return reviewed


async def build_advanced_analytics(session: AsyncSession) -> dict[str, Any]:
    """Advanced metrics payload for the Analytics & Review tab."""
    trades = await load_reviewed_trades(session)
    return compute_advanced_metrics(trades)
