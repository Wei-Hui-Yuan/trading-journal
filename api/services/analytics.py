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

    def add(self, position: ClosedPosition) -> None:
        self.trade_count += 1
        self.net_pnl += position.realized_pnl
        if position.realized_pnl > 0:
            self.wins += 1

    def as_dict(self) -> dict[str, Any]:
        win_rate = (
            round(self.wins / self.trade_count * 100, 2) if self.trade_count else 0.0
        )
        return {
            "trade_count": self.trade_count,
            "net_pnl": float(self.net_pnl),
            "win_rate_pct": win_rate,
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
