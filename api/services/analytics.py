"""Dashboard analytics: core performance stats and the day/session heatmap.

Reads closed positions out of the `positions` table and aggregates them into a
payload the frontend can map straight onto a grid.

All money math stays in `Decimal` internally and is only widened to `float` at
the response boundary, so rounding never accumulates through the aggregation.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

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


# ---------------------------------------------------------------------------
# Dashboard timeframe windows
# ---------------------------------------------------------------------------
#
# Every figure the dashboard reports -- headline stats, heatmap and equity
# curve -- is computed over the same window, so the three cannot describe
# different spans while sitting on one screen.

# Built-in presets. Resolved here rather than in the browser on purpose: "YTD"
# and "1Y" are definitions, and a second copy of them in TypeScript would be
# free to drift. The client names the preset; the server decides what it means.
PRESET_YTD = "YTD"
PRESET_1Y = "1Y"
PRESET_ALL = "ALL"
PRESETS: tuple[str, ...] = (PRESET_YTD, PRESET_1Y, PRESET_ALL)

# What a dashboard with no parameters shows. A year of history is the window
# that answers "how am I trading", where ALL increasingly answers "how did I
# trade in 2024" as the ledger grows.
DEFAULT_PRESET = PRESET_1Y

# 1Y is 365 calendar days back, not "same date last year". Both are defensible;
# this one keeps the window a fixed length so two consecutive days are
# comparable, and does not have to decide what 1Y means on a 29 February.
ONE_YEAR_DAYS = 365

# The longest span the equity curve will ever plot, in calendar days.
#
# The curve emits one point per calendar day between the first and last close,
# which is deliberate -- gaps carry information, and a chart plotting only
# trading days compresses a three-month pause into one step. But the loop is
# driven by whatever dates are in the data, and it has no natural ceiling: a
# single execution carrying a corrupt timestamp (a 1970 epoch default, a
# mis-parsed statement) stretches it across five decades. Measured, one such
# row alongside ordinary 2026 data produced 20,637 points in a single JSON
# response -- megabytes over the wire, and enough series for Recharts to lock
# the tab on every dashboard load.
#
# Ten years is far past any window this journal is used to answer questions
# about, so clamping cannot hide real history; it can only cut off dates that
# should not exist. When it fires it says so, rather than quietly showing a
# shorter curve than was asked for.
MAX_CURVE_DAYS = 3650


@dataclass(frozen=True)
class Window:
    """The span of closes a dashboard payload covers.

    `start`/`end` are inclusive market-time calendar dates. Either may be None,
    which means unbounded on that side -- that is what ALL is, and it is
    distinct from a bound that happens to sit before the first trade.
    """

    start: Optional[date] = None
    end: Optional[date] = None
    # The built-in that produced these dates, when one did. None for a custom
    # range, so the UI can tell a saved preset from a built-in pill.
    preset: Optional[str] = None

    def contains(self, day: date) -> bool:
        if self.start is not None and day < self.start:
            return False
        if self.end is not None and day > self.end:
            return False
        return True

    @property
    def is_unbounded(self) -> bool:
        return self.start is None and self.end is None


def market_today() -> date:
    """Today's calendar date in market time.

    Not the server's date. A container in UTC rolls over at 19:00 or 20:00 ET,
    so between then and midnight a YTD window computed from the server clock
    would already include tomorrow -- and on 31 December it would jump a whole
    year ahead of the trader.
    """
    return datetime.now(MARKET_TZ).date()


def resolve_window(
    preset: Optional[str] = None,
    start: Optional[date] = None,
    end: Optional[date] = None,
    *,
    today: Optional[date] = None,
) -> Window:
    """Turn what the client asked for into a concrete pair of dates.

    Precedence, and why:

      1. Explicit `start`/`end` win outright. A saved custom preset sends
         dates, and it must mean exactly what it says.
      2. Otherwise a named built-in is expanded here.
      3. Otherwise the default (1Y), so a bare request is bounded rather than
         plotting the entire ledger.

    A partial explicit range is honoured as half-open -- `start` with no `end`
    means "since then", which is a reasonable thing to ask for and cheaper to
    support than to reject.

    Raises ValueError on an unknown preset or a backwards range; the endpoint
    turns both into a 422 naming the problem.
    """
    today = today or market_today()

    if start is not None or end is not None:
        if start is not None and end is not None and start > end:
            raise ValueError(
                f"start_date ({start.isoformat()}) is after end_date "
                f"({end.isoformat()})."
            )
        return Window(start=start, end=end, preset=None)

    name = (preset or DEFAULT_PRESET).strip().upper()
    if name not in PRESETS:
        raise ValueError(
            f"preset must be one of {', '.join(PRESETS)} (got {preset!r}), "
            "or pass explicit start_date/end_date."
        )

    if name == PRESET_ALL:
        # Genuinely unbounded. Not "a very early start date" -- the clamp below
        # is what protects the payload, and expressing ALL as a sentinel year
        # would make an ordinary window indistinguishable from a corrupt one.
        return Window(preset=PRESET_ALL)

    if name == PRESET_YTD:
        return Window(start=date(today.year, 1, 1), end=today, preset=PRESET_YTD)

    return Window(
        start=today - timedelta(days=ONE_YEAR_DAYS), end=today, preset=PRESET_1Y
    )


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
    gross_pnl: Optional[Decimal] = None
    commission: Optional[Decimal] = None


@dataclass(frozen=True)
class RealizedLeg:
    """One slice of realised money, and the moment it was realised.

    The unit every period figure buckets on. A `ClosedPosition` is one idea and
    carries a single exit date; a leg is one closing fill, so a round trip that
    scaled out across a boundary contributes to both sides of it -- which is
    the only way a windowed total can agree with a broker statement.

    `position_id` is None when the run never went flat: real money, banked out
    of a position still held. That is the P&L that had no home at all before
    migration 022.
    """

    realized_pnl: Decimal
    gross_pnl: Decimal
    # ALL-IN cost (migration 023): commission plus exchange, clearing and
    # regulatory charges, derived from the broker's own realised figure. This
    # is what makes gross - commission = net tie to the IBKR statement.
    commission: Decimal
    exit_time: datetime
    position_id: Optional[Any] = None
    # The raw apportioned ibCommission, for the "what did I pay IBKR" split.
    ib_commission: Decimal = Decimal("0")
    # None when the broker never reported one for this leg's closing fill, in
    # which case `commission` fell back to ib_commission and this leg's cost is
    # unverified. Counted rather than assumed.
    broker_realized_pnl: Optional[Decimal] = None


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


def compute_core_stats(
    positions: list[ClosedPosition],
    legs: Optional[list["RealizedLeg"]] = None,
) -> dict[str, Any]:
    """Headline performance numbers.

    TWO GRAINS, DELIBERATELY. Money comes from `legs`; counts come from
    `positions`.

    Money is realised by a closing FILL. Selling half a position banks that P&L
    whatever happens to the other half, and a `positions` row does not exist
    until the ticker returns to flat -- so summing positions silently omitted
    every dollar realised out of a position still held. On this account that
    was -72.34 year-to-date, enough to turn a reported +66.20 into the broker's
    actual -6.14. See migration 022.

    Counts stay on completed round trips, because that is what a trade IS. A
    partial exit is not an idea finished, and letting it increment trade_count
    or win rate would undo the flat-to-flat aggregation that keeps one trade
    counted once.

    So `net_pnl` can move while `total_trades` does not, and that is correct
    rather than an inconsistency. `open_run_pnl` is reported separately so the
    difference is nameable: it is exactly the money banked out of positions
    still open.

    `legs=None` falls back to summing positions, preserving the old behaviour
    for callers that have no legs to give (older tests, backtests).
    """
    total_trades = len(positions)
    if not total_trades and not legs:
        return {
            "net_pnl": 0.0,
            "gross_pnl": 0.0,
            "total_commission": 0.0,
            "ib_commission": 0.0,
            "unverified_legs": 0,
            "open_run_pnl": 0.0,
            "win_rate_pct": 0.0,
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "scratches": 0,
            "profit_factor": 0.0,
            "avg_roi_pct": 0.0,
        }

    net_pnl = Decimal("0")
    total_gross_pnl = Decimal("0")
    total_commission = Decimal("0")
    gross_profit = Decimal("0")  # sum of winning P&L
    gross_loss = Decimal("0")  # sum of |losing P&L|
    wins = 0
    # Counted, not just accumulated. `gross_loss` already sums the money; this
    # is the population behind it, and win_rate_pct alone cannot be inverted to
    # recover it once scratches exist -- see `scratches` in the return below.
    losses = 0
    # Capital-weighted, not a mean of per-position ROI%: summed separately and
    # divided once, below, so a $1 position's +300% cannot swing the figure as
    # hard as a $1,000 position's +5%. Both sums are restricted to the same
    # cost_basis > 0 positions in the loop, so the ratio is always taken over
    # money actually measured on both sides -- see the note at its use for why
    # that has to be `pnl` (position grain) and not the `net_pnl` this function
    # builds from `legs`.
    total_cost = Decimal("0")
    roi_pnl_sum = Decimal("0")

    open_run_pnl = Decimal("0")
    ib_commission = Decimal("0")
    # Legs whose closing fill carried no broker figure, so their cost is the
    # apportioned ibCommission alone and excludes exchange and regulatory
    # charges. Zero on a broker-only ledger; non-zero means the reconciliation
    # to the statement is approximate for that many slices, which the UI has to
    # be able to say rather than quietly present as exact.
    unverified = 0

    for position in positions:
        pnl = position.realized_pnl

        if pnl > 0:
            gross_profit += pnl
            wins += 1
        elif pnl < 0:
            gross_loss += -pnl
            losses += 1

        # ROI is measured against capital committed at entry. Guard the
        # denominator: a zero entry price or quantity would otherwise raise.
        cost_basis = position.entry_price * position.quantity
        if cost_basis > 0:
            total_cost += cost_basis
            roi_pnl_sum += pnl

    if legs is None:
        # No legs supplied: fall back to the round-trip sum. Understates by any
        # money banked out of a still-open position, which is precisely the bug
        # migration 022 fixed -- so this path exists only for callers that have
        # no legs to give, never for the dashboard.
        for position in positions:
            net_pnl += position.realized_pnl
            total_gross_pnl += (
                position.gross_pnl if position.gross_pnl is not None else position.realized_pnl
            )
            total_commission += (
                position.commission if position.commission is not None else Decimal("0")
            )
    else:
        for leg in legs:
            net_pnl += leg.realized_pnl
            total_gross_pnl += leg.gross_pnl
            total_commission += leg.commission
            ib_commission += leg.ib_commission
            if leg.broker_realized_pnl is None:
                unverified += 1
            if leg.position_id is None:
                open_run_pnl += leg.realized_pnl

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
        "gross_pnl": float(total_gross_pnl),
        # ALL-IN: commission plus exchange, clearing and regulatory charges.
        # gross_pnl - total_commission = net_pnl exactly, and net_pnl equals
        # what the IBKR statement says for the same window.
        "total_commission": float(total_commission),
        # The IBKR commission alone. The difference between the two is what the
        # broker charges beyond commission -- about 5.3c per closing fill --
        # which is worth being able to see rather than having it blended away.
        "ib_commission": float(ib_commission),
        # Slices whose cost could not be reconciled to a broker figure.
        "unverified_legs": unverified,
        # Of `net_pnl`, how much was banked scaling out of positions that are
        # still open. Named rather than buried: it is the reason net_pnl and
        # the sum of the round trips below it can differ, and an unexplained
        # difference between two figures on one screen reads as a bug.
        "open_run_pnl": float(open_run_pnl),
        "win_rate_pct": round(wins / total_trades * 100, 2) if total_trades else 0.0,
        "total_trades": total_trades,
        # The population behind win_rate_pct, split out so the dashboard can
        # show "47W / 89L" rather than a bare percentage.
        #
        # These need NOT sum to total_trades. A round trip that closed at
        # exactly break-even is neither -- it is a scratch, and it is named
        # here for the same reason `open_run_pnl` is: an unexplained
        # difference between two figures on one screen reads as a bug. Note
        # that win_rate_pct is wins/total_trades, so scratches DILUTE it
        # rather than being excluded from the denominator.
        "wins": wins,
        "losses": losses,
        "scratches": total_trades - wins - losses,
        "profit_factor": profit_factor,
        # roi_pnl_sum, not net_pnl: net_pnl is leg-grain when legs are given
        # (the dashboard's normal case) and includes open_run_pnl, money
        # banked out of a position that is still open and therefore has no
        # `positions` row -- none of its cost basis is in total_cost. Dividing
        # that money by a denominator that never counted it would overstate
        # or understate the figure by however much is currently banked on
        # open positions, which is not hypothetical: -75.33 on this ledger the
        # day this was measured. roi_pnl_sum sums the same position.realized_pnl
        # the loop above already gated on cost_basis > 0, so the ratio is
        # always over capital actually measured on both sides.
        "avg_roi_pct": (
            float(round(roi_pnl_sum / total_cost * 100, 2)) if total_cost > 0 else 0.0
        ),
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

    # The seven columns below, not the whole entity. `select(Position)` builds a
    # ~40-column ORM object per row -- notes, all three post-mortem prose
    # fields, the mistakes array, the hindsight prices -- and this function
    # reads none of them. The prose is the part that hurts: the more review a
    # trade has, the more text the dashboard pulls across the wire and
    # discards, every single load. Measured at 2000 rows, the ORM hydration
    # plus the Decimal conversions below cost 38.2ms against 10.2ms for a
    # column select.
    #
    # Named columns rather than a tuple index, so the comprehension below reads
    # exactly as it did against the entity and a reordering here cannot quietly
    # shift a value into the wrong field.
    rows = (
        await session.execute(
            select(
                Position.realized_pnl,
                Position.gross_pnl,
                Position.commission,
                Position.entry_price,
                Position.quantity,
                Position.entry_time,
                Position.exit_time,
            )
        )
    ).all()

    return [
        ClosedPosition(
            realized_pnl=Decimal(str(row.realized_pnl)),
            gross_pnl=Decimal(str(row.gross_pnl)) if row.gross_pnl is not None else Decimal(str(row.realized_pnl)),
            commission=Decimal(str(row.commission)) if row.commission is not None else Decimal("0"),
            entry_price=Decimal(str(row.entry_price)),
            quantity=Decimal(str(row.quantity)),
            entry_time=row.entry_time,
            exit_time=row.exit_time,
        )
        for row in rows
        if row.realized_pnl is not None and row.entry_time is not None
    ]


def _exit_day(position: ClosedPosition) -> Optional[date]:
    """The market-time calendar day a round trip closed on.

    One helper, used by the window filter, the clamp and the curve's own
    bucketing, so all three agree on which side of midnight a close falls --
    the whole point of expressing the window in market dates.
    """
    if position.exit_time is None:
        return None
    return position.exit_time.astimezone(MARKET_TZ).date()


def filter_by_exit_window(
    positions: Iterable[ClosedPosition], window: Window
) -> tuple[list[ClosedPosition], int]:
    """Keep round trips that CLOSED inside the window.

    Dated by exit for the same reason the curve is: a trade opened in March and
    closed in July moved the account in July, and a July window that excluded
    it would show a balance stepping up with nothing to account for it.

    Note what this means for the heatmap, which buckets by ENTRY. A 2025 window
    contains a trade entered on 2024-12-30 and closed on 2025-01-02, and that
    trade lands on the heatmap's Monday under its December entry. That is
    correct rather than a leak: the window selects which trades are in scope,
    and the heatmap answers when those trades were entered.

    Returns the kept positions and how many were dropped for having no exit
    time at all. `positions.exit_time` is NOT NULL, so that count is expected
    to be zero -- it is returned rather than assumed so a schema change cannot
    silently start shrinking every windowed figure.

    Filtered in Python rather than SQL deliberately: the boundary has to be the
    market-time date, and comparing a TIMESTAMPTZ against one in SQL needs
    `(exit_time AT TIME ZONE 'America/New_York')::date`, which no existing index
    covers. The dashboard already loads every position for the heatmap, so this
    costs a pass over a list that is in memory regardless. When the ledger
    outgrows that, the move is that expression plus a matching index -- not a
    UTC comparison, which would misfile every close in the last five hours of a
    trading day.
    """
    if window.is_unbounded:
        return list(positions), 0

    kept: list[ClosedPosition] = []
    undated = 0
    for position in positions:
        day = _exit_day(position)
        if day is None:
            # Cannot be placed in time, so it cannot be shown to be in the
            # window. Counted, never silently absorbed.
            undated += 1
            continue
        if window.contains(day):
            kept.append(position)
    return kept, undated


def clamp_to_max_span(
    positions: Iterable[ClosedPosition],
) -> tuple[list[ClosedPosition], bool]:
    """Drop closes more than MAX_CURVE_DAYS before the most recent one.

    The backstop for a corrupt date. See MAX_CURVE_DAYS for why an unbounded
    span is a real hazard rather than a theoretical one.

    Measured from the LAST close rather than from today, so a ledger that has
    been idle for a while keeps its full recent history instead of having the
    window walk off the end of it.

    Idempotent: clamping an already-clamped list reports False, which is what
    lets build_dashboard clamp once and build_equity_curve re-check without the
    two disagreeing about whether truncation happened.
    """
    materialised = list(positions)
    days = [day for day in map(_exit_day, materialised) if day is not None]
    if not days:
        return materialised, False

    last_day = max(days)
    floor = last_day - timedelta(days=MAX_CURVE_DAYS)
    if min(days) >= floor:
        return materialised, False

    kept = [
        position
        for position in materialised
        if (day := _exit_day(position)) is not None and day >= floor
    ]
    logger.warning(
        "Equity curve span clamped to %d days: %d of %d closed round trips are "
        "dated before %s and were excluded. A span this wide almost always "
        "means a corrupt execution timestamp -- check the oldest exit_time in "
        "`positions`.",
        MAX_CURVE_DAYS,
        len(materialised) - len(kept),
        len(materialised),
        floor.isoformat(),
    )
    return kept, True


def clamp_legs_to_max_span(
    legs: Iterable[RealizedLeg],
) -> tuple[list[RealizedLeg], bool]:
    """clamp_to_max_span, for legs. Same ceiling, same reasoning.

    Needed separately because the curve's span is driven by whatever it plots,
    and once it plots legs a corrupt timestamp on a leg stretches it exactly as
    a corrupt position date used to.
    """
    materialised = list(legs)
    days = [
        leg.exit_time.astimezone(MARKET_TZ).date()
        for leg in materialised
        if leg.exit_time is not None
    ]
    if not days:
        return materialised, False

    floor = max(days) - timedelta(days=MAX_CURVE_DAYS)
    if min(days) >= floor:
        return materialised, False

    kept = [
        leg
        for leg in materialised
        if leg.exit_time is not None
        and leg.exit_time.astimezone(MARKET_TZ).date() >= floor
    ]
    logger.warning(
        "Equity curve span clamped to %d days: %d of %d realised legs are dated "
        "before %s and were excluded.",
        MAX_CURVE_DAYS, len(materialised) - len(kept), len(materialised), floor.isoformat(),
    )
    return kept, True


async def load_realized_legs(session: AsyncSession) -> list[RealizedLeg]:
    """Every realised slice of money in the ledger.

    Imported locally so this module never participates in an import cycle with
    the FastAPI app that calls it.
    """
    from main import RealizedLeg as RealizedLegRow  # noqa: PLC0415

    # Seven columns, same reasoning as load_closed_positions above. This table
    # is the larger of the two -- one row per realisation event rather than per
    # round trip -- so it benefits more from not building an entity per row.
    rows = (
        await session.execute(
            select(
                RealizedLegRow.realized_pnl,
                RealizedLegRow.gross_pnl,
                RealizedLegRow.commission,
                RealizedLegRow.exit_time,
                RealizedLegRow.position_id,
                RealizedLegRow.ib_commission,
                RealizedLegRow.broker_realized_pnl,
            )
        )
    ).all()

    return [
        RealizedLeg(
            realized_pnl=Decimal(str(row.realized_pnl)),
            gross_pnl=Decimal(str(row.gross_pnl)),
            commission=Decimal(str(row.commission or 0)),
            exit_time=row.exit_time,
            position_id=row.position_id,
            ib_commission=Decimal(str(row.ib_commission or 0)),
            broker_realized_pnl=(
                Decimal(str(row.broker_realized_pnl))
                if row.broker_realized_pnl is not None
                else None
            ),
        )
        for row in rows
        if row.realized_pnl is not None and row.exit_time is not None
    ]


def filter_legs_by_window(
    legs: Iterable[RealizedLeg], window: Window
) -> list[RealizedLeg]:
    """Keep money realised inside the window, by the date it was realised."""
    if window.is_unbounded:
        return list(legs)
    return [
        leg
        for leg in legs
        if window.contains(leg.exit_time.astimezone(MARKET_TZ).date())
    ]


def build_equity_curve(
    positions: Iterable[ClosedPosition],
    *,
    truncated: bool = False,
    legs: Optional[Iterable[RealizedLeg]] = None,
) -> dict[str, Any]:
    """Cumulative realised P&L over time, with the drawdown it went through.

    NOT account equity, and deliberately not called that in the response. True
    equity needs a starting balance plus every deposit and withdrawal, none of
    which the broker feed carries -- inventing one by working backwards from
    today's account size would silently assume the account was never funded
    twice. This is the sum of closed P&L, which the data does support.

    Plotted from LEGS when they are supplied -- one point of money per closing
    fill, dated when that fill happened. A round trip carries a single exit
    date, so drawing the curve from round trips put every dollar of a
    scaled-out position on the day it finally closed, and omitted outright any
    money banked out of a position still open. Both are wrong on a chart whose
    whole job is when the account moved. `positions` remains the fallback for
    callers with no legs.

    Dated by EXIT time. A trade opened in March and closed in July moved the
    balance in July, and dating it by entry would draw a curve that recovered
    before the trade that recovered it.

    Every calendar day between the first and last close gets a point, including
    weekends. The gaps are the point: a chart that plots only trading days
    compresses a three-month pause into one step, and "how fast did it recover"
    is unanswerable if the x-axis is not proportional to time.

    That per-day loop is bounded by MAX_CURVE_DAYS. `truncated` is reported in
    the summary whenever the span was cut -- by this function or by a caller
    that clamped first -- because a curve silently starting later than the data
    does is indistinguishable from an account that simply began trading then.
    """
    # Enforced here as well as in build_dashboard, so a direct caller (a
    # backtest, a script, a future endpoint) cannot bypass the ceiling. A
    # second clamp over already-clamped input is a no-op.
    positions, clamped_here = clamp_to_max_span(positions)
    truncated = truncated or clamped_here

    if legs is not None:
        legs, legs_clamped = clamp_legs_to_max_span(legs)
        truncated = truncated or legs_clamped

    if legs is None:
        dated = [
            (day, p.realized_pnl)
            for p in positions
            if (day := _exit_day(p)) is not None
        ]
    else:
        dated = [
            (leg.exit_time.astimezone(MARKET_TZ).date(), leg.realized_pnl)
            for leg in legs
            if leg.exit_time is not None
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
                "truncated": truncated,
                "max_days": MAX_CURVE_DAYS,
            },
        }

    daily_pnl: dict[Any, Decimal] = {}
    for day, pnl in dated:
        daily_pnl[day] = daily_pnl.get(day, Decimal("0")) + pnl

    # Counted from ROUND TRIPS, not from the legs the money came from. The
    # tooltip says "3 trades" and has to mean three ideas finished; scaling out
    # of one position four times is one trade, and counting legs would report
    # four. Money and counts come from different grains on purpose -- the same
    # split core_stats makes.
    daily_count: dict[Any, int] = {}
    for position in positions:
        day = _exit_day(position)
        if day is not None:
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
            # Completed round trips, not the legs the money came from -- same
            # split as daily_count above and as core_stats.
            "closed_trades": sum(daily_count.values()),
            # True when closes older than the ceiling were dropped. The UI says
            # so on the chart rather than letting a cut curve read as history.
            "truncated": truncated,
            "max_days": MAX_CURVE_DAYS,
        },
    }


async def build_dashboard(
    session: AsyncSession, window: Optional[Window] = None
) -> dict[str, Any]:
    """Full dashboard payload for one window: stats, heatmap, equity curve.

    All three are computed from ONE filtered list. Windowing only the curve
    would put a 1Y chart beside an all-time win rate on the same screen, and
    nothing on it would say they covered different spans.

    The window is applied first, then the corrupt-date clamp -- in that order,
    because clamping first would measure the ceiling from a close the user just
    filtered out.
    """
    window = window or resolve_window()

    everything = await load_closed_positions(session)
    windowed, undated = filter_by_exit_window(everything, window)
    clamped, truncated = clamp_to_max_span(windowed)

    # Money, at its own grain. Selected by the date each slice was REALISED,
    # which is why a windowed total can now agree with a broker statement: a
    # round trip that scaled out either side of the boundary contributes to
    # both windows, and money banked out of a position still open contributes
    # at all. See migration 022.
    all_legs = await load_realized_legs(session)
    legs = filter_legs_by_window(all_legs, window)
    legs, legs_truncated = clamp_legs_to_max_span(legs)
    truncated = truncated or legs_truncated

    return {
        # Echoed back so the UI can label what it is showing, and so a
        # truncated or empty result carries its own explanation instead of
        # looking like an account with no history.
        "window": {
            "preset": window.preset,
            "start_date": window.start.isoformat() if window.start else None,
            "end_date": window.end.isoformat() if window.end else None,
            "truncated": truncated,
            "max_days": MAX_CURVE_DAYS,
            "closed_trades_in_window": len(clamped),
            "closed_trades_total": len(everything),
            # Expected to be zero -- positions.exit_time is NOT NULL. Surfaced
            # so that if it ever is not, the shortfall is visible rather than
            # showing up as figures that quietly do not add up.
            "excluded_undated": undated,
            # Realisation events behind the money. Higher than the trade count
            # whenever a position was scaled out of, and the difference is the
            # reason the two grains exist.
            "realized_legs_in_window": len(legs),
        },
        "core_stats": compute_core_stats(clamped, legs),
        # Stays on completed round trips: the heatmap answers "which session do
        # I ENTER well in", which is a question about finished ideas. Its cell
        # totals therefore need not sum to core_stats.net_pnl -- the gap is
        # core_stats.open_run_pnl, which is named for exactly that reason.
        "heatmap": build_heatmap(clamped),
        "equity_curve": build_equity_curve(clamped, truncated=truncated, legs=legs),
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
    # Decimal, not int: trades.quantity is NUMERIC(18,8), and a fractional
    # share position -- 0.65 shares is real on this account -- truncated to 0
    # under int().
    quantity: Decimal
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
    # What the timeframe filter dates this trade by. Exit rather than entry, so
    # a window means the same set of trades here as it does on the dashboard --
    # `filter_by_exit_window` selects round trips the same way, and two pages
    # disagreeing about which trades a window contains is the confusion this
    # field exists to prevent.
    exit_time: Optional[datetime] = None


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


def compute_discipline_score(disciplines: dict[str, bool]) -> Optional[float]:
    """One trade's compliance, as a percentage of the rules it was ANSWERED
    against -- never of however many rules exist today.

    That distinction matters twice over. A rule added last month has no
    opinion about a trade from a year ago, so scoring against today's rule
    count would silently fail every historical trade on rules it never saw.
    And None, not 0, when nothing was answered at all: an unreviewed trade
    has no compliance to report, and reading it as 0% would count a review
    backlog as total indiscipline -- the exact trap PositionDiscipline's own
    docstring warns against for the single-rule case.

    HALF-UP, not Python's built-in `round`. This formula is deliberately
    duplicated in the frontend (src/lib/discipline.ts) so a single row's score
    does not need a round trip to compute, and the two have to agree to the
    digit or the same trade reads differently in a journal row than it does
    behind a compliance bucket.

    `round(x, 2)` is half-to-EVEN, and JavaScript has no equivalent: `Math.round`
    is half-up, and reproducing Python's banker's rounding in JS means
    reimplementing it against the exact binary value of a double. So the
    agreement is bought on this side instead, where `floor(x * 10000 + 0.5)` is
    precisely what `Math.round(x * 10000)` does for a non-negative x -- and a
    percentage is never negative.

    The two used to diverge, by one hundredth, on any tie: 1 followed rule of 32
    gave 3.12 here and 3.13 in the browser. Unreachable in practice (it takes 32
    answered rules and there are five), which is exactly why it would have gone
    unnoticed until the playbook grew.
    """
    if not disciplines:
        return None
    followed = sum(1 for value in disciplines.values() if value)
    return math.floor(followed / len(disciplines) * 10000 + 0.5) / 100


# Fixed percentage ranges, not derived from the current rule count. The
# number of disciplines in play changes over time -- adding or retiring a
# rule must not reshuffle every historical trade into different buckets, and
# a fixed scale is also the only one two trades scored against a different
# NUMBER of rules can be compared on at all.
#
# 100% is kept as its own bucket rather than folded into "80-99%" because it
# is the one figure a trader actually asks about: "what do I shoot when I do
# everything right".
COMPLIANCE_BUCKET_ORDER: tuple[str, ...] = ("100%", "80-99%", "50-79%", "<50%")


def _compliance_bucket(score: float) -> str:
    if score >= 100:
        return "100%"
    if score >= 80:
        return "80-99%"
    if score >= 50:
        return "50-79%"
    return "<50%"


def compute_compliance_buckets(
    trades: list[ReviewedTrade],
    r_by_id: dict[str, float],
) -> list[dict[str, Any]]:
    """Win rate and average R, grouped by how much of the answered playbook a
    trade actually followed.

    `compute_discipline_breakdown` answers "is this ONE rule worth following";
    this answers the coarser question the same data can support -- does
    following your rules AS A WHOLE correlate with the outcome, regardless of
    which particular rules they were.

    A trade never checked against any rule contributes to no bucket, for the
    same reason `compute_discipline_score` returns None for it: an unreviewed
    trade is missing data, not a trade that broke every rule.

    All four buckets are always returned, in a fixed order, even ones with no
    trades in them -- matching `_r_distribution`'s own precedent of a complete
    shape a chart can render without special-casing absence. `_side_stats`
    already answers "0 trades" with nulls rather than a division by zero.
    """
    grouped: dict[str, dict[str, list]] = {
        label: {"pnl": [], "r": []} for label in COMPLIANCE_BUCKET_ORDER
    }

    for trade in trades:
        if trade.realized_pnl is None:
            continue
        score = compute_discipline_score(trade.disciplines)
        if score is None:
            continue
        bucket = grouped[_compliance_bucket(score)]
        bucket["pnl"].append(float(trade.realized_pnl))
        r = r_by_id.get(trade.trade_id)
        if r is not None:
            bucket["r"].append(r)

    return [
        {
            "compliance": label,
            **_side_stats(grouped[label]["pnl"], grouped[label]["r"]),
        }
        for label in COMPLIANCE_BUCKET_ORDER
    ]


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


def filter_trades_by_exit_window(
    trades: Iterable[ReviewedTrade], window: Window
) -> list[ReviewedTrade]:
    """Keep trades that CLOSED inside the window.

    The ReviewedTrade counterpart to `filter_by_exit_window`, and deliberately
    the same rule: dated by exit, compared as a MARKET-time calendar date. The
    analytics page and the dashboard have to mean the same thing by "1Y" or the
    two win rates diverge again -- for a third reason, and one nothing on screen
    would explain.

    A trade that cannot be placed in time is dropped from a BOUNDED window
    rather than kept: not knowing when it closed is not evidence that it closed
    inside the span. `positions.exit_time` is NOT NULL and this loader only
    reads closed positions, so that branch is defensive rather than expected.

    Filtered in Python for the same reason the sibling is -- the boundary is a
    market-time date, which no index covers -- and over a list already in
    memory.
    """
    if window.is_unbounded:
        return list(trades)

    kept: list[ReviewedTrade] = []
    for trade in trades:
        if trade.exit_time is None:
            continue
        if window.contains(trade.exit_time.astimezone(MARKET_TZ).date()):
            kept.append(trade)
    return kept


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
    #
    # Iterates every trade, scored or not -- the same reasoning
    # compute_strategy_breakdown already applies. A trade with no stop cannot
    # be scored, but a mistake tag on it is still a real instance of that
    # mistake; the old version iterated `scored` only, so tagging "Oversized"
    # on three trades where one lacks a stop silently reported "2 trades",
    # with nothing on screen hinting a third existed.
    r_by_id = {t.trade_id: r for t, r in scored}
    by_mistake: dict[str, dict[str, Any]] = {}
    for trade in trades:
        for tag in trade.mistakes or []:
            bucket = by_mistake.setdefault(tag, {"r": [], "unscored": 0, "wins": 0})
            r = r_by_id.get(trade.trade_id)
            if r is None:
                bucket["unscored"] += 1
            else:
                bucket["r"].append(r)
                if r > 0:
                    bucket["wins"] += 1

    mistake_breakdown = [
        {
            "mistake": tag,
            # Every trade tagged with this mistake, scoreable or not.
            "trade_count": len(b["r"]) + b["unscored"],
            "scored": len(b["r"]),
            "unscored": b["unscored"],
            "total_r": round(sum(b["r"]), 4) if b["r"] else 0.0,
            "avg_r": round(sum(b["r"]) / len(b["r"]), 4) if b["r"] else None,
            "win_rate_pct": round(b["wins"] / len(b["r"]) * 100, 2) if b["r"] else None,
        }
        for tag, b in sorted(by_mistake.items(), key=lambda kv: sum(kv[1]["r"]))
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
        "compliance_buckets": compute_compliance_buckets(
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

    # Both narrowed to the columns actually read below. `trades` is the one
    # that matters most: it holds one row per FILL rather than per round trip,
    # so it is strictly the larger table, and the entity carries `thesis` (TEXT)
    # and `broker_original` (JSONB) that nothing here touches. Four columns are
    # read from it.
    positions = (
        await session.execute(
            select(
                Position.id,
                Position.symbol,
                Position.direction,
                Position.quantity,
                Position.entry_price,
                Position.exit_price,
                Position.open_trade_id,
                Position.mistakes,
                Position.review_status,
                Position.realized_pnl,
                Position.strategy_id,
                Position.entry_time,
                Position.exit_time,
            )
        )
    ).all()
    trades = (
        await session.execute(
            select(
                Trade.id,
                Trade.planned_entry,
                Trade.stop_loss,
                Trade.strategy_id,
            )
        )
    ).all()
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

        # Stored on the position since migration 022, in the matcher's own
        # LONG/SHORT vocabulary. It used to be reconstructed from `opening` and
        # defaulted to "BUY" when that came back empty, which reports a short's
        # entry slippage with the sign reversed.
        direction = "SELL" if (position.direction or "").upper() == "SHORT" else "BUY"

        reviewed.append(
            ReviewedTrade(
                trade_id=str(position.id),
                ticker=position.symbol,
                direction=direction,
                quantity=Decimal(str(position.quantity or 0)),
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
                exit_time=position.exit_time,
            )
        )
    return reviewed


async def build_advanced_analytics(
    session: AsyncSession, window: Optional[Window] = None
) -> dict[str, Any]:
    """Advanced metrics payload for the Analytics & Review tab.

    `window` governs the whole payload -- R distribution, expectancy, slippage,
    the strategy ranking and both discipline tables -- for the reason
    build_dashboard's docstring gives: a 1Y figure beside an all-time one on a
    single screen, with nothing saying they cover different spans, is worse than
    either alone.

    Defaults to None (everything) rather than to 1Y, so a caller that has no
    opinion still gets the old behaviour. The ENDPOINT defaults to 1Y, matching
    the dashboard; the difference is deliberate, because a bare in-process call
    asking for "the advanced metrics" has not asked to be narrowed.

    The window is NOT applied to the pending-review queue, which this does not
    build. That queue is a work list rather than a statistic, and one that
    silently hid an older unreviewed trade would be worse than a long one.
    """
    trades = await load_reviewed_trades(session)
    if window is None:
        return compute_advanced_metrics(trades)

    in_window = filter_trades_by_exit_window(trades, window)
    payload = compute_advanced_metrics(in_window)
    # Only the fields the toolbar actually renders. Deliberately NOT the
    # dashboard's full window block: `truncated` and `max_days` describe the
    # equity curve's clamp and there is no curve here, so reporting them would
    # be inventing an answer to a question this payload never asks.
    payload["window"] = {
        "preset": window.preset,
        "start_date": window.start.isoformat() if window.start else None,
        "end_date": window.end.isoformat() if window.end else None,
        "closed_trades_in_window": len(in_window),
        "closed_trades_total": len(trades),
    }
    return payload
