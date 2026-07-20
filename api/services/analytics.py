"""Dashboard analytics: core performance stats and the day/session heatmap.

Reads closed positions out of the `positions` table and aggregates them into a
payload the frontend can map straight onto a grid.

All money math stays in `Decimal` internally and is only widened to `float` at
the response boundary, so rounding never accumulates through the aggregation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timezone
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
        )
        for row in rows
        if row.realized_pnl is not None and row.entry_time is not None
    ]


async def build_dashboard(session: AsyncSession) -> dict[str, Any]:
    """Full dashboard payload: headline stats plus the heatmap grid."""
    positions = await load_closed_positions(session)
    return {
        "core_stats": compute_core_stats(positions),
        "heatmap": build_heatmap(positions),
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
    }


async def load_reviewed_trades(session: AsyncSession) -> list[ReviewedTrade]:
    """Read closed executions from the trades ledger."""
    from main import Trade  # noqa: PLC0415 - deferred to avoid circular import

    rows = (await session.execute(select(Trade))).scalars().all()

    return [
        ReviewedTrade(
            trade_id=str(row.id),
            ticker=row.ticker,
            direction=row.direction or "",
            quantity=int(row.quantity or 0),
            actual_entry=Decimal(str(row.actual_entry)),
            exit_price=Decimal(str(row.exit_price)) if row.exit_price is not None else None,
            planned_entry=(
                Decimal(str(row.planned_entry)) if row.planned_entry is not None else None
            ),
            stop_loss=Decimal(str(row.stop_loss)) if row.stop_loss is not None else None,
            mistakes=list(row.mistakes or []),
            review_status=row.review_status,
        )
        for row in rows
        if row.actual_entry is not None
    ]


async def build_advanced_analytics(session: AsyncSession) -> dict[str, Any]:
    """Advanced metrics payload for the Analytics & Review tab."""
    trades = await load_reviewed_trades(session)
    return compute_advanced_metrics(trades)
