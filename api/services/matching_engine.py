"""FIFO trade matching engine.

The `trades` table stores raw broker executions (one row per fill, each with a
BUY/SELL direction). This module pairs those fills into round-trip positions
using first-in-first-out accounting, computes realized P&L, and classifies the
holding period into a trading style.

Clean-room separation: `trades` is treated as an immutable execution log and is
never written to. Every derived round trip is appended to the `positions` table
instead (see migrations/001_create_positions.sql).

The core matcher is intentionally pure: it operates on lightweight `Execution`
values rather than ORM rows, so it can be unit tested without a database and
reused for backtests. `run_matching_for_ticker` is the thin database adapter.
"""

from __future__ import annotations

import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

# Style buckets. All values fit the trades.style VARCHAR(15) column.
STYLE_SCALP = "Scalp"
STYLE_DAY_TRADE = "Day Trade"
STYLE_SWING_TRADE = "Swing Trade"

# A position held under this long is a scalp regardless of session boundaries.
SCALP_MAX_HOLD = timedelta(hours=2)

# Rows the sync engine inserts before a human classifies them.
UNCLASSIFIED_STYLE = "Unclassified"

# Every freshly matched position lands in the Trade Inbox awaiting review.
DEFAULT_REVIEW_STATUS = "pending"

LONG = "LONG"
SHORT = "SHORT"
BUY = "BUY"
SELL = "SELL"

ROLE_OPEN = "OPEN"
ROLE_CLOSE = "CLOSE"

# Money columns are NUMERIC(_, 4). Weighted averages are quantized to match, so
# what is stored is what was computed rather than whatever Postgres rounds to.
PRICE_PRECISION = Decimal("0.0001")


@dataclass(frozen=True)
class Execution:
    """One broker fill, decoupled from the ORM row it came from."""

    trade_id: uuid.UUID
    ticker: str
    direction: str  # BUY or SELL
    # Decimal, not int: fractional fills are ordinary (migration 010).
    quantity: Decimal
    price: Decimal
    executed_at: datetime


@dataclass
class _OpenLot:
    """An execution with quantity still open, waiting to be closed out.

    `remaining` shrinks as later executions consume the lot, which is what
    makes partial fills work: one lot can be closed by several executions and
    one execution can close several lots.
    """

    execution: Execution
    remaining: Decimal


@dataclass(frozen=True)
class PositionFill:
    """One execution's contribution to a round trip.

    A single fill can be split across two round trips -- an oversell closes the
    long and opens a short with the same execution -- so `quantity` is the
    portion attributed to *this* position, not the fill's full size.
    """

    trade_id: uuid.UUID
    role: str  # OPEN or CLOSE
    quantity: Decimal
    price: Decimal
    executed_at: datetime


@dataclass
class _Leg:
    """One FIFO pairing: part of an open lot closed by part of a later fill.

    Legs are the unit of correct arithmetic. They are aggregated into a
    MatchedPosition for presentation, but P&L is always summed from here --
    never recomputed from the rounded weighted averages -- so display rounding
    can never leak into the money.
    """

    direction: str
    quantity: Decimal
    open_trade_id: uuid.UUID
    close_trade_id: uuid.UUID
    entry_price: Decimal
    exit_price: Decimal
    entry_date: datetime
    exit_date: datetime
    realized_pnl: Decimal


@dataclass
class MatchedPosition:
    """One round trip: the whole span from flat to flat on a ticker.

    Scaling in or out produces several fills but a single position here, which
    is how a trader thinks about it and what keeps trade counts and win rate
    honest -- one idea counted once. The individual executions stay available
    in `fills` for drill-down and slippage work.
    """

    ticker: str
    direction: str  # LONG or SHORT
    quantity: Decimal  # total shares round-tripped
    # First entry and last exit. The pair identifies the round trip, which is
    # what lets uq_positions_open_close keep re-runs idempotent.
    open_trade_id: uuid.UUID
    close_trade_id: uuid.UUID
    entry_price: Decimal  # quantity-weighted
    exit_price: Decimal  # quantity-weighted
    entry_date: datetime  # first entry
    exit_date: datetime  # final exit
    realized_pnl: Decimal
    style: str
    fills: list[PositionFill] = field(default_factory=list)

    @property
    def holding_period(self) -> timedelta:
        return self.exit_date - self.entry_date


def _weighted_average(pairs: list[tuple[Decimal, Decimal]]) -> Decimal:
    """Quantity-weighted mean price, quantized to the money column's scale."""
    total_qty = sum((qty for _, qty in pairs), Decimal("0"))
    if total_qty == 0:
        return Decimal("0")
    total = sum((price * qty for price, qty in pairs), Decimal("0"))
    return (total / Decimal(total_qty)).quantize(PRICE_PRECISION, rounding=ROUND_HALF_UP)


def _aggregate_round_trip(ticker: str, legs: list[_Leg]) -> MatchedPosition:
    """Fold the legs of one flat-to-flat span into a single position."""
    # Attribution per execution. A fill closing several lots appears once with
    # its quantities summed, rather than once per lot it happened to touch.
    opens: dict[uuid.UUID, PositionFill] = {}
    closes: dict[uuid.UUID, PositionFill] = {}

    for leg in legs:
        for bucket, trade_id, role, price, at in (
            (opens, leg.open_trade_id, "OPEN", leg.entry_price, leg.entry_date),
            (closes, leg.close_trade_id, "CLOSE", leg.exit_price, leg.exit_date),
        ):
            existing = bucket.get(trade_id)
            bucket[trade_id] = PositionFill(
                trade_id=trade_id,
                role=role,
                quantity=leg.quantity + (existing.quantity if existing else Decimal("0")),
                price=price,
                executed_at=at,
            )

    entry_date = min(leg.entry_date for leg in legs)
    exit_date = max(leg.exit_date for leg in legs)

    first_open = min(opens.values(), key=lambda f: (f.executed_at, str(f.trade_id)))
    last_close = max(closes.values(), key=lambda f: (f.executed_at, str(f.trade_id)))

    return MatchedPosition(
        ticker=ticker,
        direction=legs[0].direction,
        quantity=sum((leg.quantity for leg in legs), Decimal("0")),
        open_trade_id=first_open.trade_id,
        close_trade_id=last_close.trade_id,
        entry_price=_weighted_average([(l.entry_price, l.quantity) for l in legs]),
        exit_price=_weighted_average([(l.exit_price, l.quantity) for l in legs]),
        entry_date=entry_date,
        exit_date=exit_date,
        # Summed from the legs, deliberately: deriving this from the weighted
        # averages above would fold their rounding into reported P&L.
        realized_pnl=sum((leg.realized_pnl for leg in legs), Decimal("0")),
        style=classify_style(entry_date, exit_date),
        fills=sorted(
            [*opens.values(), *closes.values()],
            key=lambda f: (f.executed_at, f.role, str(f.trade_id)),
        ),
    )


@dataclass
class MatchingResult:
    """Everything the matcher learned about one ticker."""

    ticker: str
    positions: list[MatchedPosition] = field(default_factory=list)
    # Lots still open at the end of the run (an unclosed position).
    open_lots: list[_OpenLot] = field(default_factory=list)
    # Executions that could not be matched because they would close more
    # quantity than was ever opened (e.g. a sync gap or a pre-existing holding).
    unmatched_closing_quantity: int = 0
    # P&L already banked by scaling out of a round trip that has not gone flat.
    # No position is emitted for it -- the trade is still open, and emitting one
    # would count a half-finished idea as a completed trade -- but the figure is
    # surfaced here so it is visibly deferred rather than silently dropped.
    open_round_trip_realized_pnl: Decimal = Decimal("0")

    @property
    def total_realized_pnl(self) -> Decimal:
        return sum((p.realized_pnl for p in self.positions), Decimal("0"))

    @property
    def open_quantity(self) -> Decimal:
        return sum((lot.remaining for lot in self.open_lots), Decimal("0"))


def classify_style(entry_date: datetime, exit_date: datetime) -> str:
    """Bucket a holding period into a trading style.

    Precedence matters: the duration test runs before the session test, so a
    90-minute position that happens to straddle midnight is still a Scalp
    rather than a Swing Trade.
    """
    held = exit_date - entry_date

    if held < SCALP_MAX_HOLD:
        return STYLE_SCALP

    # Same calendar day -> opened and closed within one session.
    if entry_date.date() == exit_date.date():
        return STYLE_DAY_TRADE

    return STYLE_SWING_TRADE


def _realized_pnl(
    direction: str, entry_price: Decimal, exit_price: Decimal, quantity: Decimal
) -> Decimal:
    """P&L for `quantity` shares of a closed position.

    Longs profit when price rises; shorts profit when it falls.
    """
    if direction == LONG:
        move = exit_price - entry_price
    else:
        move = entry_price - exit_price
    return move * quantity


def match_executions(executions: Iterable[Execution]) -> MatchingResult:
    """Pair executions into round trips using FIFO accounting.

    Walks fills in chronological order maintaining a queue of open lots. An
    execution on the opposite side of the queue closes the oldest lots first;
    whatever quantity is left over opens a new lot. This single loop covers
    long and short positions, and partial fills fall out of it naturally
    because lots and executions are consumed in `min()`-sized slices.
    """
    ordered = sorted(executions, key=lambda e: (e.executed_at, str(e.trade_id)))
    if not ordered:
        return MatchingResult(ticker="")

    result = MatchingResult(ticker=ordered[0].ticker)
    open_lots: deque[_OpenLot] = deque()
    # Legs of the round trip currently in progress, flushed when it goes flat.
    legs: list[_Leg] = []

    for execution in ordered:
        # Quantity from this fill still looking for a counterparty.
        unallocated = execution.quantity

        # An execution closes existing lots only if it is on the opposite side.
        # A queue of BUY lots is closed by a SELL, and vice versa.
        while unallocated > 0 and open_lots and open_lots[0].execution.direction != execution.direction:
            lot = open_lots[0]

            # Consume the smaller of the two so neither side goes negative.
            matched_qty = min(unallocated, lot.remaining)

            # The lot direction determines whether this was a long or a short.
            position_direction = LONG if lot.execution.direction == BUY else SHORT

            legs.append(
                _Leg(
                    direction=position_direction,
                    quantity=matched_qty,
                    open_trade_id=lot.execution.trade_id,
                    close_trade_id=execution.trade_id,
                    entry_price=lot.execution.price,
                    exit_price=execution.price,
                    entry_date=lot.execution.executed_at,
                    exit_date=execution.executed_at,
                    realized_pnl=_realized_pnl(
                        position_direction,
                        lot.execution.price,
                        execution.price,
                        matched_qty,
                    ),
                )
            )

            lot.remaining -= matched_qty
            unallocated -= matched_qty

            # Lot fully closed -> retire it so the next one becomes the head.
            if lot.remaining == 0:
                open_lots.popleft()

        # Going flat ends the round trip. Checked before the leftover opens a
        # new lot, so an oversell that flips long->short closes the long here
        # and starts the short cleanly rather than merging the two.
        if legs and not open_lots:
            result.positions.append(_aggregate_round_trip(execution.ticker, legs))
            legs = []

        # Leftover quantity opens a new lot. If the queue already held lots on
        # this side, this simply adds to the position; if the queue was drained
        # by this execution, it flips the position to the other side.
        if unallocated > 0:
            open_lots.append(_OpenLot(execution=execution, remaining=unallocated))

    # Legs left here belong to a round trip that never closed. They are not
    # emitted: a position is only real once it is flat, and the residual shows
    # up as open_lots instead.
    result.open_round_trip_realized_pnl = sum(
        (leg.realized_pnl for leg in legs), Decimal("0")
    )
    result.open_lots = list(open_lots)
    return result


async def load_executions_for_ticker(
    session: AsyncSession,
    ticker: str,
    *,
    only_unclassified: bool = False,
) -> list[Execution]:
    """Read raw fills for one ticker out of the trades table.

    Imported locally so this module never participates in an import cycle with
    the FastAPI app that will eventually call it.
    """
    from main import Trade  # noqa: PLC0415 - deferred to avoid circular import

    stmt = select(Trade).where(Trade.ticker == ticker)
    if only_unclassified:
        # Rows the IBKR sync has not had a style assigned to yet.
        stmt = stmt.where(Trade.style == UNCLASSIFIED_STYLE)

    rows = (await session.execute(stmt)).scalars().all()

    return [
        Execution(
            trade_id=row.id,
            ticker=row.ticker,
            direction=(row.direction or "").upper(),
            quantity=Decimal(str(row.quantity)),
            # Numeric(10, 4) comes back as Decimal; keep it exact for money math.
            price=Decimal(str(row.actual_entry)),
            executed_at=row.entry_date,
        )
        for row in rows
        # Skip anything that cannot participate in matching.
        if row.direction and row.quantity and row.actual_entry is not None and row.entry_date
    ]


async def run_matching_for_ticker(
    session: AsyncSession,
    ticker: str,
    *,
    persist: bool = True,
    only_unclassified: bool = False,
) -> MatchingResult:
    """Match one ticker's executions and append the round trips to `positions`.

    Rows in `trades` are never modified: they stay an immutable record of what
    the broker actually filled. Everything derived lands in `positions`.

    Set `persist=False` to compute the numbers without writing (useful for
    previews and backtests).
    """
    executions = await load_executions_for_ticker(
        session, ticker, only_unclassified=only_unclassified
    )
    result = match_executions(executions)
    result.ticker = ticker

    if persist and result.positions:
        await insert_positions(session, result.positions)
        await session.commit()

    return result


async def insert_positions(
    session: AsyncSession, positions: list[MatchedPosition]
) -> int:
    """Batch-insert closed round trips into the `positions` table.

    One statement for the whole batch rather than a write per position.

    `open_trade_id` / `close_trade_id` identify the two executions behind each
    position and must always be set: the uq_positions_open_close unique index
    is what makes ON CONFLICT DO NOTHING turn a re-run into a no-op, and
    Postgres treats NULLs as distinct, so null keys would never collide.
    """
    if not positions:
        return 0

    # Deferred to avoid a circular import with the FastAPI app.
    from main import Position, PositionFill  # noqa: PLC0415

    rows = [
        {
            "id": uuid.uuid4(),
            "symbol": position.ticker,
            "style": position.style,
            "quantity": position.quantity,
            "entry_price": position.entry_price,
            "exit_price": position.exit_price,
            "entry_time": position.entry_date,
            "exit_time": position.exit_date,
            "realized_pnl": position.realized_pnl,
            "open_trade_id": position.open_trade_id,
            "close_trade_id": position.close_trade_id,
            # New positions enter the Trade Inbox awaiting review. DO NOTHING
            # (rather than an upsert) is what preserves tags and grades a user
            # has already set when the engine is re-run over the same fills.
            "review_status": DEFAULT_REVIEW_STATUS,
        }
        for position in positions
    ]

    stmt = (
        pg_insert(Position)
        .values(rows)
        .on_conflict_do_nothing()
        .returning(Position.id, Position.open_trade_id, Position.close_trade_id)
    )
    inserted = (await session.execute(stmt)).fetchall()

    # DO NOTHING returns nothing for rows that already existed, so this maps
    # only the genuinely new positions. That is what we want: a position present
    # from an earlier run already has its fills, and re-inserting them would be
    # a no-op against uq_position_fills_position_trade_role anyway.
    new_ids = {(row.open_trade_id, row.close_trade_id): row.id for row in inserted}

    fill_rows = [
        {
            "id": uuid.uuid4(),
            "position_id": position_id,
            "trade_id": fill.trade_id,
            "role": fill.role,
            "quantity": fill.quantity,
            "price": fill.price,
            "executed_at": fill.executed_at,
        }
        for position in positions
        if (position_id := new_ids.get((position.open_trade_id, position.close_trade_id)))
        for fill in position.fills
    ]

    if fill_rows:
        await session.execute(
            pg_insert(PositionFill).values(fill_rows).on_conflict_do_nothing()
        )

    return len(inserted)
