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

import logging
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable, Optional

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

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

# positions.commission is NUMERIC(12, 4). Apportioning one fill's commission
# across the legs it covers divides, so the result must be quantized somewhere;
# doing it once per round trip keeps gross - commission = net exact.
MONEY_PRECISION = Decimal("0.0001")


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
    # What this fill COST to execute: positive is paid, negative is a rebate
    # (see ParsedExecution.commission_cost). Defaults to zero so a caller that
    # does not know about commissions -- a backtest, an older test -- gets the
    # gross arithmetic it always got rather than a null.
    commission: Decimal = Decimal("0")
    # What IBKR says this fill realised, net of commission AND of exchange,
    # clearing and regulatory charges. None when unknown, which is the normal
    # state for a hand-added repair fill and for anything the Flex window no
    # longer reaches. See _reconcile_to_broker.
    broker_realized_pnl: Optional[Decimal] = None


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
    # NET of ALL costs -- what this slice actually put in the account.
    realized_pnl: Decimal
    # The same figure before costs, and the costs themselves. Kept per leg
    # rather than derived later because commission is apportioned by the
    # fraction of each fill a leg consumed, and that fraction is only known
    # here.
    gross_pnl: Decimal = Decimal("0")
    # ALL-IN cost: commission plus exchange, clearing and regulatory charges.
    # Equal to ib_commission until _reconcile_to_broker replaces it with
    # gross - broker_realized_pnl, which is the only figure that ties to the
    # statement without re-implementing IBKR's fee schedule.
    commission: Decimal = Decimal("0")
    # The apportioned raw ibCommission, kept for provenance.
    ib_commission: Decimal = Decimal("0")
    # This leg's share of the broker's own realised figure for its closing
    # fill. None when the fill carried none.
    broker_realized_pnl: Optional[Decimal] = None


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
    # NET: gross_pnl - commission. This is the figure every downstream
    # statistic reads, so net is what it has to be -- a win rate computed on
    # gross counts a trade that made $0.40 and paid $0.36 as a full win.
    realized_pnl: Decimal
    style: str
    fills: list[PositionFill] = field(default_factory=list)
    # The FIFO pairings this round trip was folded from. Kept so they can be
    # persisted to `realized_legs`, which is where money is bucketed by period
    # -- a round trip that scaled out across a month boundary realised its P&L
    # on two different dates, and only the legs know that.
    legs: list["_Leg"] = field(default_factory=list)
    # Reported beside the net figure rather than instead of it. Cost is a
    # thing the trader controls -- through size, through how often they trade
    # -- and it cannot be managed while it is folded invisibly into P&L.
    gross_pnl: Decimal = Decimal("0")
    commission: Decimal = Decimal("0")

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


@dataclass(frozen=True)
class OpenExposure:
    """What is still held on a ticker, and what it cost.

    Open exposure has no `positions` row -- by definition nothing has closed --
    so it is reconstructed from the fills FIFO never paired off.
    """

    # Signed: positive is long, negative is short.
    net_quantity: Decimal
    # Cost of the shares that REMAIN, on the same FIFO basis the broker uses,
    # inclusive of the commission paid to acquire them.
    average_cost: Decimal


@dataclass
class _BasisLot:
    """An unsold parcel of shares and what it actually cost to acquire.

    `commission_per_share` is capitalised into the basis rather than expensed,
    because that is what the broker does -- IBKR's reported cost basis for an
    open position includes the commission paid to buy it. Held per share so a
    partial sale takes the right fraction of it away with the shares.
    """

    remaining: Decimal
    price: Decimal
    commission_per_share: Decimal = Decimal("0")

    @property
    def unit_cost(self) -> Decimal:
        # Commission is a COST (positive paid, negative rebate), so it adds.
        # A rebated fill therefore sits marginally BELOW its trade price, which
        # is exactly what the statement shows: MSFT's remaining lot reads
        # 407.298903 against a 407.30 fill.
        return self.price + self.commission_per_share


def replay_open_exposure(
    fills: Iterable[
        tuple[str, Decimal, Decimal] | tuple[str, Decimal, Decimal, Decimal]
    ],
) -> OpenExposure:
    """Net position and cost of the remainder, replayed fill by fill in order.

    FIFO, matching the broker. This used to use average-cost, on the stated
    grounds that average-cost "is what the broker reports" -- and the claim was
    never checked against a statement. It is wrong. IBKR reports a FIFO basis,
    and the difference is not academic: on MSFT, FIFO says the 2 remaining
    shares cost 407.30 and average-cost said 409.40, against a statement
    reading 407.298903. Two dollars a share, on the figure the journal invites
    you to reconcile.

    Its predecessor was worse still -- it averaged every same-direction fill,
    counting shares already sold as though they were still held, and reported
    415.45 for that same position. This is the second correction to the same
    number, so the statement figure is now pinned in a test.

    Commission is CAPITALISED into the basis, again because the broker does it:
    a fill's commission is part of what the shares cost, and it travels with
    them as they are consumed. Only the acquiring commission counts -- the
    commission on a sale is a cost of exiting, already inside that sale's
    realised P&L, and adding it here would charge it twice.

    Reducing fills consume the oldest parcels first and leave the survivors'
    cost untouched: selling half a position does not change what the other half
    cost. A fill that overshoots flat flips the position and opens a fresh
    parcel at its own price, carrying none of the old basis across.
    """
    position = Decimal("0")
    lots: deque[_BasisLot] = deque()

    for fill in fills:
        direction, quantity, price = fill[0], fill[1], fill[2]
        # Optional so a caller with no commission data -- a backtest, a preview
        # -- gets the price-only basis it always got rather than a crash.
        commission = fill[3] if len(fill) > 3 else Decimal("0")
        if quantity is None or price is None:
            continue
        signed = quantity if (direction or BUY).upper() == BUY else -quantity
        if signed == 0:
            continue

        per_share = (commission or Decimal("0")) / abs(signed)

        opening = position == 0 or (position > 0) == (signed > 0)
        if opening:
            lots.append(_BasisLot(abs(signed), price, per_share))
            position += signed
            continue

        # Reducing. Retire the oldest parcels first; whatever is left keeps the
        # cost it always had.
        closing = min(abs(signed), abs(position))
        left = closing
        while left > 0 and lots:
            lot = lots[0]
            taken = min(left, lot.remaining)
            lot.remaining -= taken
            left -= taken
            if lot.remaining == 0:
                lots.popleft()
        position += signed

        overshoot = abs(signed) - closing
        if overshoot > 0:
            # Crossed through flat: the excess opens the other way, and none of
            # the old basis survives into a position of the opposite sign.
            lots.clear()
            lots.append(_BasisLot(overshoot, price, per_share))

    if position == 0:
        return OpenExposure(Decimal("0"), Decimal("0"))

    held = sum((lot.remaining for lot in lots), Decimal("0"))
    if held == 0:
        # Defensive: a net position with no parcels behind it would be a bug in
        # the loop above, and reporting a division error is worse than
        # reporting the position with no basis.
        return OpenExposure(net_quantity=position, average_cost=Decimal("0"))

    cost = sum((lot.remaining * lot.unit_cost for lot in lots), Decimal("0"))
    return OpenExposure(
        net_quantity=position,
        average_cost=(cost / held).quantize(
            PRICE_PRECISION, rounding=ROUND_HALF_UP
        ),
    )


def _reconcile_to_broker(
    legs: list[_Leg], executions: dict[uuid.UUID, Execution]
) -> None:
    """Replace each leg's cost with the all-in figure the broker implies.

    WHY THIS EXISTS. Our FIFO agrees with IBKR's lot matching exactly --
    compared fill by fill across a year, every closing fill lands within $0.50
    and the whole ledger within $9.14. What differs is COST. `ibCommission` is
    the commission alone, while IBKR's realised P&L also nets exchange,
    clearing and regulatory charges, which the Flex feed folds into cost basis
    instead of reporting separately. That is about 5.3c per closing fill.

    Rather than re-implement SEC Section 31 and FINRA TAF schedules and keep
    them current forever, take the broker's own answer and work backwards:

        all_in_cost = our_gross_pnl - broker_realized_pnl

    Applied per CLOSING fill, because that is the grain IBKR reports on. The
    figure already contains the buy-side charges too -- they are inside the
    cost basis IBKR relieved -- so this REPLACES the apportioned commission
    rather than adding to it. Adding would double-count the commission.

    Be clear about what the result is: a plug. It is everything between our
    gross and the broker's net, which is overwhelmingly fees but also absorbs
    any small cost-basis difference. That is the point -- net P&L then matches
    the statement to the cent by construction -- and it is why the figure is
    labelled all-in rather than commission, and why `ib_commission` is kept
    beside it untouched.

    A fill with no broker figure keeps its apportioned ibCommission. Reported
    rather than hidden: `broker_realized_pnl` stays None on those legs, and the
    coverage is surfaced so a journal cannot quietly under-charge fees on rows
    it could not verify.
    """
    by_close: dict[uuid.UUID, list[_Leg]] = {}
    for leg in legs:
        by_close.setdefault(leg.close_trade_id, []).append(leg)

    for close_trade_id, fill_legs in by_close.items():
        execution = executions.get(close_trade_id)
        if execution is None or execution.broker_realized_pnl is None:
            continue

        gross = sum((leg.gross_pnl for leg in fill_legs), Decimal("0"))
        all_in = gross - execution.broker_realized_pnl

        # Split across the legs this fill closed, by quantity. Allocated so the
        # parts sum to the whole exactly: every leg but the last is rounded,
        # and the last takes the remainder. Distributing the rounding error
        # instead would leave the period total a cent away from the statement,
        # which is the entire thing this function exists to prevent.
        total_qty = sum((leg.quantity for leg in fill_legs), Decimal("0"))
        allocated = Decimal("0")
        for index, leg in enumerate(fill_legs):
            if index == len(fill_legs) - 1 or total_qty == 0:
                share = all_in - allocated
                broker_share = execution.broker_realized_pnl - sum(
                    (l.broker_realized_pnl or Decimal("0")) for l in fill_legs[:index]
                )
            else:
                weight = leg.quantity / total_qty
                share = (all_in * weight).quantize(
                    MONEY_PRECISION, rounding=ROUND_HALF_UP
                )
                broker_share = (execution.broker_realized_pnl * weight).quantize(
                    MONEY_PRECISION, rounding=ROUND_HALF_UP
                )
            allocated += share
            leg.commission = share
            leg.broker_realized_pnl = broker_share
            leg.realized_pnl = leg.gross_pnl - share


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

    # Summed from the legs, deliberately: deriving these from the weighted
    # averages above would fold their rounding into reported P&L.
    #
    # Quantized here rather than left to Postgres. All three money figures land
    # in NUMERIC(12,4) columns and are rendered side by side, so the identity
    # gross - commission = net has to hold in what is STORED, not just in what
    # was computed. Rounding each of the three independently on the way into
    # the database does not guarantee that -- a fractional fill puts real
    # digits below the fourth place, and a penny of disagreement between three
    # numbers on screen reads as a bug in the journal.
    #
    # So gross and commission are each rounded once, and net is derived from
    # the rounded pair. Exact by construction, at a cost of at most 0.0001.
    gross_pnl = sum((leg.gross_pnl for leg in legs), Decimal("0")).quantize(
        MONEY_PRECISION, rounding=ROUND_HALF_UP
    )
    commission = sum((leg.commission for leg in legs), Decimal("0")).quantize(
        MONEY_PRECISION, rounding=ROUND_HALF_UP
    )

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
        gross_pnl=gross_pnl,
        commission=commission,
        realized_pnl=gross_pnl - commission,
        style=classify_style(entry_date, exit_date),
        fills=sorted(
            [*opens.values(), *closes.values()],
            key=lambda f: (f.executed_at, f.role, str(f.trade_id)),
        ),
        legs=list(legs),
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
    # would count a half-finished idea as a completed trade.
    #
    # This figure used to be the ONLY record of that money, and nothing read it,
    # so every dollar realised on the way out of a position still held was
    # missing from the journal entirely (migration 022). The legs below are now
    # persisted; this stays as the convenient total.
    open_round_trip_realized_pnl: Decimal = Decimal("0")
    # Those same legs, individually. Real realised money with no completed round
    # trip to hang it on, so they are stored with position_id NULL.
    open_legs: list["_Leg"] = field(default_factory=list)
    # Round trips that were in `positions` before this run and are not in it
    # now, because a backdated fill re-partitioned the FIFO queue. Reported
    # rather than swallowed: they carried P&L into every headline figure, and
    # their disappearance is a real event the caller should be able to explain.
    positions_removed: int = 0
    # How many of those had been reviewed. Deleting a round trip takes its
    # grade, notes, mistakes and discipline answers with it, and that is the
    # part the user cannot reconstruct.
    reviews_discarded: int = 0

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


def _gross_pnl(
    direction: str, entry_price: Decimal, exit_price: Decimal, quantity: Decimal
) -> Decimal:
    """P&L for `quantity` shares of a closed position, BEFORE costs.

    Longs profit when price rises; shorts profit when it falls.

    Named for what it is. This used to be `_realized_pnl` and was stored
    unchanged as the realised figure, which is the whole of C3: the price move
    is not what reached the account.
    """
    if direction == LONG:
        move = exit_price - entry_price
    else:
        move = entry_price - exit_price
    return move * quantity


def _apportion_commission(execution: Execution, matched_quantity: Decimal) -> Decimal:
    """The share of this fill's commission that belongs to `matched_quantity`.

    One execution can be consumed by several legs -- a 20-share buy closed by
    two 10-share sells, or an oversell that closes one round trip and opens the
    next -- so its commission is split by the fraction of the fill each leg
    took.

    Nothing is rounded here. Thirds and sevenths still leave a residue, since
    Decimal division keeps 28 significant digits rather than infinite ones, but
    it lands around 1e-28 and vanishes when the round trip quantizes to the
    4-decimal money column. Rounding per leg instead would put the error at the
    cent level, where it is visible.

    Charged in full to the round trips that consume the fill, which means a
    partially-filled position carries only the cost of the shares it has
    actually closed. The rest stays with the open remainder and is charged when
    it closes.
    """
    if execution.quantity <= 0 or not execution.commission:
        return Decimal("0")
    return execution.commission * matched_quantity / execution.quantity


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

        # Legs THIS execution closes. Collected separately from `legs`, which
        # accumulates the whole round trip, because the broker reports realised
        # P&L per execution -- so these, and only these, are what its figure
        # has to be reconciled against.
        closed_here: list[_Leg] = []

        # An execution closes existing lots only if it is on the opposite side.
        # A queue of BUY lots is closed by a SELL, and vice versa.
        while unallocated > 0 and open_lots and open_lots[0].execution.direction != execution.direction:
            lot = open_lots[0]

            # Consume the smaller of the two so neither side goes negative.
            matched_qty = min(unallocated, lot.remaining)

            # The lot direction determines whether this was a long or a short.
            position_direction = LONG if lot.execution.direction == BUY else SHORT

            gross = _gross_pnl(
                position_direction,
                lot.execution.price,
                execution.price,
                matched_qty,
            )
            # Both sides are charged: the trade paid to get in and to get out,
            # and only counting one halves the cost of every round trip.
            commission = _apportion_commission(
                lot.execution, matched_qty
            ) + _apportion_commission(execution, matched_qty)

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
                    gross_pnl=gross,
                    # Both start as the apportioned ibCommission.
                    # _reconcile_to_broker replaces `commission` with the
                    # all-in figure where the broker reported one, and leaves
                    # `ib_commission` alone as the provenance record.
                    commission=commission,
                    ib_commission=commission,
                    realized_pnl=gross - commission,
                )
            )

            closed_here.append(legs[-1])

            lot.remaining -= matched_qty
            unallocated -= matched_qty

            # Lot fully closed -> retire it so the next one becomes the head.
            if lot.remaining == 0:
                open_lots.popleft()

        # Swap the apportioned commission for the all-in cost the broker's own
        # realised figure implies. Done here, before the round trip is folded
        # up, so the aggregate is built from reconciled legs rather than having
        # to be corrected afterwards.
        if closed_here:
            _reconcile_to_broker(closed_here, {execution.trade_id: execution})

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
    # up as open_lots instead. Net, like everything else -- the commission on
    # the shares already scaled out has been paid whether or not the idea is
    # finished.
    result.open_round_trip_realized_pnl = sum(
        (leg.realized_pnl for leg in legs), Decimal("0")
    )
    # Persisted with position_id NULL. Real money, realised on a real date,
    # with no completed round trip to hang it on -- which is exactly why it
    # went missing from every figure before migration 022.
    result.open_legs = list(legs)
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
            # Already normalised to a cost on the way into `trades` (positive
            # is paid, negative is a rebate), so the engine only subtracts.
            commission=Decimal(str(row.commission or 0)),
            # The broker's own realised figure, when it sent one. None here is
            # meaningful and must not become zero: zero is a real P&L, absent
            # means "reconcile against the commission instead".
            broker_realized_pnl=(
                Decimal(str(row.broker_realized_pnl))
                if row.broker_realized_pnl is not None
                else None
            ),
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
    """Make `positions` match what FIFO says about this ticker, exactly.

    Rows in `trades` are never modified: they stay an immutable record of what
    the broker actually filled. Everything derived lands in `positions`.

    AUTHORITATIVE, NOT ADDITIVE. This used to only append, trusting
    ON CONFLICT against uq_positions_open_close to make a re-run a no-op. That
    holds only while re-running produces the same (open_trade_id,
    close_trade_id) pairs -- and a fill that arrives with an EARLIER timestamp
    than fills already stored re-partitions the whole FIFO queue, so it does
    not.

    Concretely: BUY 10@100 then SELL 10@110 stores one round trip worth +100.
    Add a backdated BUY 10@90 and FIFO now leaves 10 shares open and closes
    nothing, so the fresh result is empty, nothing conflicts, and that +100 row
    stays in the table forever -- counted in net P&L, win rate, trade count,
    expectancy and the equity curve, while the same shares are simultaneously
    reported as open exposure. A later sell instead produces a DIFFERENT pair,
    which also fails to conflict, and the P&L is double-counted outright.

    That is not a hypothetical ordering. Adding a fill IBKR dropped is
    backdated by definition, and the two Flex queries disagree on purpose --
    a TCF query reports today's fills while an Activity query lags a day or so,
    so an older fill routinely lands after a newer one has already matched.

    So the fresh result is treated as the truth and anything else for this
    ticker is deleted. Pairs that survive re-matching keep their row, and
    therefore their id, review, grade and discipline answers; only genuinely
    dissolved round trips are removed.

    Set `persist=False` to compute without writing (previews, backtests, and
    the reconciliation audit).
    """
    # Checked before any work: a subset match cannot be authoritative over the
    # whole ticker. With only_unclassified the fresh set is built from part of
    # the fills, so every position derived from a classified fill would look
    # stale and be deleted. No caller passes this today; the guard is here so
    # that adding one cannot quietly turn this function into a data-loss bug.
    if persist and only_unclassified:
        raise ValueError(
            "run_matching_for_ticker(persist=True) cannot be combined with "
            "only_unclassified=True: matching a subset of a ticker's fills "
            "cannot decide which of its positions are stale."
        )

    executions = await load_executions_for_ticker(
        session, ticker, only_unclassified=only_unclassified
    )
    result = match_executions(executions)
    result.ticker = ticker

    if not persist:
        return result

    # Deferred to avoid a circular import with the FastAPI app.
    from main import Position, ReviewStatus  # noqa: PLC0415

    fresh_pairs = {(p.open_trade_id, p.close_trade_id) for p in result.positions}
    existing = (
        await session.execute(
            select(
                Position.id,
                Position.open_trade_id,
                Position.close_trade_id,
                Position.review_status,
                Position.review_went_well,
                Position.review_went_wrong,
                Position.review_lessons,
                Position.notes,
            ).where(Position.symbol == ticker)
        )
    ).all()

    stale = [
        row for row in existing
        if (row.open_trade_id, row.close_trade_id) not in fresh_pairs
    ]

    result.positions_removed = len(stale)
    # Same test delete_trade and update_execution already use, so one number
    # means one thing across every surface that reports it.
    result.reviews_discarded = sum(
        1
        for row in stale
        if row.review_status == ReviewStatus.reviewed.value
        or any((row.review_went_well, row.review_went_wrong, row.review_lessons, row.notes))
    )

    # One transaction. A delete that committed without its replacement insert
    # would erase real round trips; an insert without the delete is the
    # double-count this exists to prevent. Neither is allowed to land alone.
    if stale:
        await session.execute(
            delete(Position).where(Position.id.in_([row.id for row in stale]))
        )
        # Loud on purpose. "Where did my review for that trade go?" needs an
        # answer, and a backdated fill re-partitioning the queue is an
        # explanation nobody would arrive at unaided.
        logger.warning(
            "Authoritative matching for %s removed %d stale position(s) and "
            "discarded %d review(s); a backdated fill re-partitioned the FIFO "
            "queue.",
            ticker,
            len(stale),
            result.reviews_discarded,
        )

    position_ids: dict[tuple[uuid.UUID, uuid.UUID], uuid.UUID] = {}
    if result.positions:
        position_ids = await insert_positions(session, result.positions)

    # Legs are replaced outright rather than reconciled. Every column is
    # derived from `trades` -- unlike `positions`, which carries the trader's
    # review -- so there is nothing here worth preserving across a rebuild, and
    # a delete-then-insert cannot leave a stale leg behind the way an
    # append-only write left stale positions (see the docstring above).
    await replace_realized_legs(session, ticker, result, position_ids)

    await session.commit()
    return result


async def insert_positions(
    session: AsyncSession, positions: list[MatchedPosition]
) -> dict[tuple[uuid.UUID, uuid.UUID], uuid.UUID]:
    """Upsert closed round trips, returning (open, close) -> position id.

    One statement for the whole batch rather than a write per position.

    `open_trade_id` / `close_trade_id` identify the round trip and must always
    be set: uq_positions_open_close is what a re-run conflicts against, and
    Postgres treats NULLs as distinct, so null keys would never collide. They
    have been NOT NULL since migration 019.

    On conflict the DERIVED columns are refreshed and the user's own work --
    grade, tags, strategy, notes, mistakes, the post-mortem, the ideal and
    revised levels -- is left exactly as it was. A round trip's numbers belong
    to its executions; its review belongs to the trader.

    `position_fills` is resynced the same way, so the drill-down cannot end up
    describing a different trade from the position above it. Returns the number
    of positions written.
    """
    if not positions:
        return 0

    # Deferred to avoid a circular import with the FastAPI app.
    from main import Position, PositionFill  # noqa: PLC0415

    rows = [
        {
            "id": uuid.uuid4(),
            "symbol": position.ticker,
            # Written verbatim. It was computed correctly here and discarded,
            # leaving every reader to reconstruct it from the opening execution
            # and guess "long" when that lookup failed (migration 022).
            "direction": position.direction,
            "style": position.style,
            "quantity": position.quantity,
            "entry_price": position.entry_price,
            "exit_price": position.exit_price,
            "entry_time": position.entry_date,
            "exit_time": position.exit_date,
            # Net. Stored under the name every consumer already reads, so
            # analytics, the equity curve and win rate become net without one
            # of them having to know commissions exist.
            "realized_pnl": position.realized_pnl,
            "gross_pnl": position.gross_pnl,
            "commission": position.commission,
            "open_trade_id": position.open_trade_id,
            "close_trade_id": position.close_trade_id,
            # New positions enter the Trade Inbox awaiting review. DO NOTHING
            # (rather than an upsert) is what preserves tags and grades a user
            # has already set when the engine is re-run over the same fills.
            "review_status": DEFAULT_REVIEW_STATUS,
        }
        for position in positions
    ]

    # DERIVED columns are refreshed on conflict; everything the user wrote is
    # never named here and so is never touched.
    #
    # This used to be DO NOTHING, which kept grades and tags safe but also made
    # a stored position permanently authoritative over the fills underneath it.
    # Re-matching could not correct a figure once written -- which is precisely
    # the state migration 020 had to repair, because every realized_pnl in the
    # table had been computed before commissions existed. A round trip is
    # derived data; it should follow its executions.
    refreshed = (
        "symbol", "direction", "style", "quantity", "entry_price", "exit_price",
        "entry_time", "exit_time", "realized_pnl", "gross_pnl", "commission",
    )
    stmt = (
        pg_insert(Position)
        .values(rows)
        .on_conflict_do_update(
            index_elements=["open_trade_id", "close_trade_id"],
            set_={column: getattr(pg_insert(Position).excluded, column)
                  for column in refreshed},
        )
        .returning(Position.id, Position.open_trade_id, Position.close_trade_id)
    )
    upserted = (await session.execute(stmt)).fetchall()
    position_ids = {(r.open_trade_id, r.close_trade_id): r.id for r in upserted}

    await _sync_position_fills(session, positions, position_ids)
    # The mapping, not a count: the caller needs it to stamp each realised leg
    # with the round trip it belongs to, and re-deriving it there would mean
    # querying back rows this statement already returned.
    return position_ids


async def _sync_position_fills(
    session: AsyncSession,
    positions: list[MatchedPosition],
    position_ids: dict[tuple[uuid.UUID, uuid.UUID], uuid.UUID],
) -> tuple[int, int]:
    """Make `position_fills` describe what FIFO actually produced.

    A round trip is identified by its FIRST open and LAST close, so a fill
    landing between them joins it without changing the pair. BUY 10 then
    SELL 15 stores a 10-share round trip (A,B); adding a backdated BUY 5 makes
    it a 15-share round trip -- still (A,B), so nothing is stale, and the
    position row is refreshed in place by the upsert above.

    Its fills were not. They were written once, when the position was created,
    and skipped ever after on the grounds that re-inserting them would conflict
    anyway. That is true of the rows that already existed and says nothing
    about the ones that should now exist: the repair fill got no row at all,
    and the closing fill kept a quantity of 10 under a position asserting 15.

    The damage is not in the money -- realized_pnl is correct -- but the
    drill-down contradicts its own position, `is_matched` reads false for the
    repair fill, and because open exposure is reconstructed from executions
    that have no fill row, it reappears as a phantom open trade. Which makes
    this the ordinary outcome of the repair feature's whole purpose: noticing a
    round trip is short a fill and adding it.

    Costs nothing when nothing changed. The stored rows were already being read
    to decide which positions to skip; they are now read in full and compared,
    and no write is issued unless the composition actually differs.
    """
    from main import PositionFill  # noqa: PLC0415 - avoids import cycle

    if not position_ids:
        return (0, 0)

    # Keyed the way uq_position_fills_position_trade_role is, so a comparison
    # here and a conflict in Postgres mean the same thing.
    fresh: dict[tuple, dict] = {}
    for position in positions:
        position_id = position_ids.get(
            (position.open_trade_id, position.close_trade_id)
        )
        if position_id is None:
            continue
        for fill in position.fills:
            fresh[(position_id, fill.trade_id, fill.role)] = {
                "position_id": position_id,
                "trade_id": fill.trade_id,
                "role": fill.role,
                "quantity": fill.quantity,
                "price": fill.price,
                "executed_at": fill.executed_at,
            }

    stored = (
        await session.execute(
            select(
                PositionFill.id,
                PositionFill.position_id,
                PositionFill.trade_id,
                PositionFill.role,
                PositionFill.quantity,
                PositionFill.price,
            ).where(PositionFill.position_id.in_(list(position_ids.values())))
        )
    ).all()
    stored_by_key = {(r.position_id, r.trade_id, r.role): r for r in stored}

    # `executed_at` is deliberately not compared. It comes from the same trades
    # row the fill was built from, so it cannot drift while the key holds --
    # and comparing timestamps across a driver boundary is the kind of thing
    # that reports a difference every run and turns a no-op into a write.
    changed = [
        values
        for key, values in fresh.items()
        if (row := stored_by_key.get(key)) is None
        or row.quantity != values["quantity"]
        or row.price != values["price"]
    ]
    # Fills that used to belong to this round trip and no longer do. Without
    # this the sum drifts the other way: a re-partition that moves a fill out
    # leaves it behind, and the drill-down over-counts.
    removed = [row.id for key, row in stored_by_key.items() if key not in fresh]

    if removed:
        await session.execute(
            delete(PositionFill).where(PositionFill.id.in_(removed))
        )

    if changed:
        await session.execute(
            pg_insert(PositionFill)
            .values([{"id": uuid.uuid4(), **values} for values in changed])
            .on_conflict_do_update(
                index_elements=["position_id", "trade_id", "role"],
                set_={
                    "quantity": pg_insert(PositionFill).excluded.quantity,
                    "price": pg_insert(PositionFill).excluded.price,
                    "executed_at": pg_insert(PositionFill).excluded.executed_at,
                },
            )
        )

    if changed or removed:
        logger.info(
            "position_fills resynced: %d row(s) written, %d removed across "
            "%d round trip(s).",
            len(changed), len(removed), len(position_ids),
        )

    return (len(changed), len(removed))


async def replace_realized_legs(
    session: AsyncSession,
    ticker: str,
    result: MatchingResult,
    position_ids: dict[tuple[uuid.UUID, uuid.UUID], uuid.UUID],
) -> int:
    """Make `realized_legs` describe exactly what FIFO realised on this ticker.

    Money is realised by a closing FILL, not by a round trip. `positions` gets
    a row only when a ticker returns to flat, so before migration 022 every
    dollar banked on the way out of a position still held was recorded nowhere
    -- MSFT alone had -63.34 of real, realised P&L that no figure in the app
    could see. Legs are where that money lives now.

    Replaced outright rather than reconciled. Every column here is derived from
    `trades`; unlike `positions`, nothing a user typed is stored, so there is
    nothing to preserve and a delete-then-insert cannot leave a stale row the
    way an append-only write once left stale positions.

    Legs from an unclosed run are written with `position_id = NULL`. That NULL
    is the point: it is real money, realised on a real date, that has no
    completed round trip to hang it on.
    """
    from main import RealizedLeg  # noqa: PLC0415 - deferred, avoids a cycle

    # Every leg FIFO produced for this ticker: those folded into completed
    # round trips, plus those left in the run that never went flat.
    pairs: list[tuple[_Leg, Optional[uuid.UUID]]] = [
        (leg, position_ids.get((position.open_trade_id, position.close_trade_id)))
        for position in result.positions
        for leg in position.legs
    ]
    pairs += [(leg, None) for leg in result.open_legs]

    await session.execute(delete(RealizedLeg).where(RealizedLeg.symbol == ticker))

    if not pairs:
        return 0

    await session.execute(
        pg_insert(RealizedLeg).values([
            {
                "id": uuid.uuid4(),
                "symbol": ticker,
                "direction": leg.direction,
                "quantity": leg.quantity,
                "open_trade_id": leg.open_trade_id,
                "close_trade_id": leg.close_trade_id,
                "entry_price": leg.entry_price,
                "exit_price": leg.exit_price,
                "entry_time": leg.entry_date,
                # The date every period figure buckets on.
                "exit_time": leg.exit_date,
                # Quantized on the way in for the same reason positions are:
                # gross - commission = net has to hold in what is STORED, not
                # only in what was computed, or three numbers rendered side by
                # side disagree by a hundredth and read as a bug.
                "gross_pnl": leg.gross_pnl.quantize(MONEY_PRECISION, rounding=ROUND_HALF_UP),
                # ALL-IN, so gross - commission = the broker's own net.
                "commission": leg.commission.quantize(MONEY_PRECISION, rounding=ROUND_HALF_UP),
                # The raw apportioned ibCommission, untouched, beside it.
                "ib_commission": leg.ib_commission.quantize(
                    MONEY_PRECISION, rounding=ROUND_HALF_UP
                ),
                "broker_realized_pnl": (
                    leg.broker_realized_pnl.quantize(MONEY_PRECISION, rounding=ROUND_HALF_UP)
                    if leg.broker_realized_pnl is not None
                    else None
                ),
                "realized_pnl": (
                    leg.gross_pnl.quantize(MONEY_PRECISION, rounding=ROUND_HALF_UP)
                    - leg.commission.quantize(MONEY_PRECISION, rounding=ROUND_HALF_UP)
                ),
                "position_id": position_id,
            }
            for leg, position_id in pairs
        ])
    )
    return len(pairs)
