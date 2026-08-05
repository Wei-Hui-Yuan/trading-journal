"""Turning broker fills into long-term-book transactions.

A SEPARATE pipeline from the trading journal's ingest, not a branch inside
it. `ingest_ibkr` is the most repaired function in this codebase -- undated
fills, unpriced fills, suppressed fills, resumable staging, FIFO rematching --
and every one of those repairs is load-bearing for a ledger with 337 rows
behind it. Adding an "is this an investment?" fork inside it would put all of
that at risk to serve a book that wants none of it.

WHY THIS CANNOT BE A FILTER ON THE EXISTING FEED. Seven of fourteen holdings
in the long-term book are ALSO swing-traded in the journal: AMZN, GOOGL,
META, MSFT, NVDA, PANW and UNH. When a GOOGL BUY arrives, nothing in the fill
says which book it belongs to -- not the ticker, not the size, not the time of
day. Any rule that guessed would misroute silently, and a misrouted fill looks
like an ordinary transaction on whichever side it lands. The separation has to
come from the broker: a distinct account, read through its own Flex query.

TRADES ONLY. Dividends are entered by hand. The Flex queries emit no
`CashTransaction` nodes and the parser reads none, so there is nothing here to
map them from; pretending otherwise would mean inventing the rows.

This module holds no database session and reaches no network -- it takes
parsed executions and returns dictionaries. The endpoint that calls it decides
what to persist, which is what keeps a broker-format surprise from reaching
either book's tables.
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

# Namespaced so a synced row can never collide with a hand-entered one, and so
# the origin stays readable in the table. `investment_transactions.external_id`
# is nullable and UNIQUE (migration 026): hand-entered rows leave it NULL and
# Postgres treats NULLs as distinct, so they never collide with each other,
# while a broker id can only ever land once.
EXTERNAL_ID_PREFIX = "IBKR-"

# What the ledger models. TRANSFER exists in the schema for a holding that
# arrived from another broker with its basis intact, but IBKR reports those as
# position transfers rather than trades, so nothing here emits one.
BUY = "BUY"
SELL = "SELL"


def external_id_for(transaction_id: str) -> str:
    return f"{EXTERNAL_ID_PREFIX}{transaction_id}"


def _signed_total(side: str, quantity: Decimal, price: Decimal,
                  fees: Decimal) -> Decimal:
    """Cash movement from the account's point of view, fees included.

    Deliberately identical to `TransactionCreate.resolved_total` on the manual
    path -- negative when money left to buy something, positive when it
    arrived, and fees pushing both directions against the trader. A synced row
    and a hand-entered row describing the same purchase must produce the same
    number, or the derived average cost would depend on how the row got there.
    """
    gross = quantity * price
    if side == SELL:
        return gross - fees
    return -(gross + fees)


def to_investment_transaction(execution: Any) -> Optional[dict]:
    """One parsed fill as an `investment_transactions` row, or None to skip.

    None is returned for a fill that cannot be placed in a ledger honestly:

      * no execution time -- there is no correct position for it in a
        chronological ledger, and average cost is computed by walking that
        ledger in order. Inventing `now()` would file a real purchase under
        whenever the sync happened to run.
      * no price -- the cost basis would be a fabrication, and every figure
        derived from it after that.

    Both are logged rather than silently dropped, because either one is a
    data-quality problem in what the broker sent, not a routine skip. The
    same two conditions the trading ingest refuses to promote on.
    """
    execution_time: Optional[datetime] = execution.execution_time
    price: Optional[Decimal] = execution.price

    if execution_time is None:
        logger.error("Investment sync: %s (%s) has no execution time; skipped.",
                     execution.transaction_id, execution.symbol)
        return None
    if price is None:
        logger.error("Investment sync: %s (%s) has no price; skipped.",
                     execution.transaction_id, execution.symbol)
        return None

    side = execution.side
    quantity = execution.abs_quantity
    # Already normalised by the parser into a COST -- positive is paid, and a
    # tiered-pricing rebate stays negative rather than being flipped into a
    # charge. Taken as-is for that reason.
    fees = execution.commission_cost

    return {
        "ticker": execution.symbol,
        "transaction_type": side,
        "quantity": quantity,
        "price": price,
        "total_amount": _signed_total(side, quantity, price, fees),
        "fees": fees,
        "transaction_date": execution_time,
        "listed_currency": "USD",
        "exchange_rate": Decimal("1"),
        "source": "IBKR",
        "external_id": external_id_for(execution.transaction_id),
        "note": None,
    }


def to_investment_transactions(executions: Iterable[Any]) -> list[dict]:
    """Map a whole statement, dropping what cannot be placed.

    Deduplicated on `external_id` within the batch: one Flex response can
    describe the same fill twice when two queries overlap, and a batch insert
    that conflicted with itself would fail the whole sync rather than the one
    row.
    """
    rows: list[dict] = []
    seen: set[str] = set()

    for execution in executions:
        row = to_investment_transaction(execution)
        if row is None:
            continue
        if row["external_id"] in seen:
            continue
        seen.add(row["external_id"])
        rows.append(row)

    return rows
