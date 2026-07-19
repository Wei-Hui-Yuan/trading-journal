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
from decimal import Decimal
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

LONG = "LONG"
SHORT = "SHORT"
BUY = "BUY"
SELL = "SELL"


@dataclass(frozen=True)
class Execution:
    """One broker fill, decoupled from the ORM row it came from."""

    trade_id: uuid.UUID
    ticker: str
    direction: str  # BUY or SELL
    quantity: int
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
    remaining: int


@dataclass
class MatchedPosition:
    """A completed round trip produced by pairing an opening and closing fill."""

    ticker: str
    direction: str  # LONG or SHORT
    quantity: int  # shares actually matched in this pairing
    open_trade_id: uuid.UUID
    close_trade_id: uuid.UUID
    entry_price: Decimal
    exit_price: Decimal
    entry_date: datetime
    exit_date: datetime
    realized_pnl: Decimal
    style: str

    @property
    def holding_period(self) -> timedelta:
        return self.exit_date - self.entry_date


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

    @property
    def total_realized_pnl(self) -> Decimal:
        return sum((p.realized_pnl for p in self.positions), Decimal("0"))

    @property
    def open_quantity(self) -> int:
        return sum(lot.remaining for lot in self.open_lots)


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
    direction: str, entry_price: Decimal, exit_price: Decimal, quantity: int
) -> Decimal:
    """P&L for `quantity` shares of a closed position.

    Longs profit when price rises; shorts profit when it falls.
    """
    if direction == LONG:
        move = exit_price - entry_price
    else:
        move = entry_price - exit_price
    return move * Decimal(quantity)


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

            result.positions.append(
                MatchedPosition(
                    ticker=execution.ticker,
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
                    style=classify_style(
                        lot.execution.executed_at, execution.executed_at
                    ),
                )
            )

            lot.remaining -= matched_qty
            unallocated -= matched_qty

            # Lot fully closed -> retire it so the next one becomes the head.
            if lot.remaining == 0:
                open_lots.popleft()

        # Leftover quantity opens a new lot. If the queue already held lots on
        # this side, this simply adds to the position; if the queue was drained
        # by this execution, it flips the position to the other side.
        if unallocated > 0:
            open_lots.append(_OpenLot(execution=execution, remaining=unallocated))

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
            quantity=int(row.quantity),
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

    from main import Position  # noqa: PLC0415 - deferred to avoid circular import

    rows = [
        {
            "id": uuid.uuid4(),
            "symbol": position.ticker,
            "style": position.style,
            "quantity": Decimal(position.quantity),
            "entry_price": position.entry_price,
            "exit_price": position.exit_price,
            "entry_time": position.entry_date,
            "exit_time": position.exit_date,
            "realized_pnl": position.realized_pnl,
            "open_trade_id": position.open_trade_id,
            "close_trade_id": position.close_trade_id,
        }
        for position in positions
    ]

    stmt = pg_insert(Position).values(rows).on_conflict_do_nothing()
    result = await session.execute(stmt)
    return result.rowcount or 0
