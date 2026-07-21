"""Parse and normalize IBKR Flex statement XML into execution records.

Flex queries emit fills under different node types depending on how the saved
query was configured:

  * `<Trade>`             -- from a "Trades" query
  * `<TradeConfirmation>` -- from a "Trade Confirmation" query
  * `<TradeConfirm>`      -- from a type="TCF" query, nested in <TradeConfirms>

All are handled. Missing one is silent and expensive: an unmatched node type
parses to zero fills, so the sync reports success while ingesting nothing.

Attribute names differ between the layouts (tradePrice vs price, ibCommission
vs commission, transactionID vs tradeID), so each field is read through a
fallback list rather than a single key. Commission is absent entirely from
some TCF layouts and stays null.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Optional
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# IBKR reports fill times in the account's configured timezone without an
# offset. They are localized to US market time so bucketing lines up with the
# analytics session grid.
MARKET_TZ = ZoneInfo("America/New_York")

# Node types that represent a fill. Tag matching is exact, so a layout absent
# from this tuple yields no executions at all rather than an error.
TRADE_NODES = ("Trade", "TradeConfirmation", "TradeConfirm")

# Attribute fallbacks, most specific first.
_ID_KEYS = ("transactionID", "tradeID", "ibExecID", "execID")
_SYMBOL_KEYS = ("symbol", "underlyingSymbol")
_QUANTITY_KEYS = ("quantity", "shares")
_PRICE_KEYS = ("tradePrice", "price")
_COMMISSION_KEYS = ("ibCommission", "commission")
_DATETIME_KEYS = ("dateTime", "tradeDate", "reportDate")


@dataclass(frozen=True)
class ParsedExecution:
    """One normalized fill, ready for the staging ledger."""

    transaction_id: str
    symbol: str
    # Signed: positive = bought, negative = sold. The sign is what carries the
    # side through the staging table, which has no explicit side column.
    quantity: int
    price: Optional[Decimal]
    commission: Optional[Decimal]
    execution_time: Optional[datetime]
    # True when IBKR reported a fractional size that had to be rounded to fit
    # the INTEGER ledger column. Surfaced so callers can report it, not just
    # bury it in a log line.
    quantity_was_rounded: bool = False

    @property
    def side(self) -> str:
        return "BUY" if self.quantity >= 0 else "SELL"

    @property
    def abs_quantity(self) -> int:
        return abs(self.quantity)


def _first(attrs: dict[str, str], keys: tuple[str, ...]) -> Optional[str]:
    """First non-empty attribute among `keys`."""
    for key in keys:
        value = attrs.get(key)
        if value not in (None, ""):
            return value
    return None


def parse_execution_datetime(raw: Optional[str]) -> Optional[datetime]:
    """Convert IBKR's `YYYYMMDD;HHMMSS` into an America/New_York datetime.

    The separator and precision vary by query configuration, so several shapes
    are attempted. The result is always timezone-aware; a date-only value is
    anchored to midnight market time.
    """
    if not raw:
        return None

    cleaned = " ".join(raw.strip().replace(";", " ").replace(",", " ").split())

    for fmt in (
        "%Y%m%d %H%M%S",
        "%Y%m%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H%M%S",
        "%Y%m%d",
        "%Y-%m-%d",
    ):
        try:
            naive = datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
        # tzinfo is attached rather than converted: the wall-clock time IBKR
        # reports IS market time, so it must not be shifted.
        return naive.replace(tzinfo=MARKET_TZ)

    logger.warning("Unparseable IBKR dateTime %r; execution_time left null", raw)
    return None


def _to_decimal(raw: Optional[str]) -> Optional[Decimal]:
    if raw in (None, ""):
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError):
        logger.warning("Unparseable IBKR numeric value %r", raw)
        return None


def _to_signed_int_quantity(
    raw: Optional[str], transaction_id: str
) -> tuple[Optional[int], bool]:
    """Coerce a reported quantity to a signed whole number of shares.

    `trades.quantity` is an INTEGER column, so a fractional fill cannot be
    stored faithfully. Rather than dropping the execution (which would silently
    lose a real trade), it is rounded and the discrepancy logged loudly.
    """
    if raw in (None, ""):
        return None, False
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        logger.warning("Unparseable quantity %r on execution %s", raw, transaction_id)
        return None, False

    rounded = int(value.to_integral_value(rounding="ROUND_HALF_UP"))
    was_rounded = value != rounded
    if was_rounded:
        logger.warning(
            "Fractional share quantity %s on IBKR execution %s rounded to %d "
            "(trades.quantity is an INTEGER column)",
            value,
            transaction_id,
            rounded,
        )
    return rounded, was_rounded


def parse_execution_node(node: ET.Element) -> Optional[ParsedExecution]:
    """Normalize one <Trade>/<TradeConfirmation> node.

    Returns None when the record lacks the identity or size needed to be
    meaningful, so one malformed row cannot abort a whole sync.
    """
    attrs: dict[str, str] = dict(node.attrib)

    transaction_id = _first(attrs, _ID_KEYS)
    symbol = _first(attrs, _SYMBOL_KEYS)
    if not transaction_id or not symbol:
        logger.warning("Skipping IBKR execution with no id/symbol: %s", attrs)
        return None

    quantity, was_rounded = _to_signed_int_quantity(
        _first(attrs, _QUANTITY_KEYS), transaction_id
    )
    if quantity is None or quantity == 0:
        logger.warning(
            "Skipping IBKR execution %s: quantity missing or zero", transaction_id
        )
        return None

    # A `buySell` attribute, when present, is authoritative over the sign --
    # some query layouts report an unsigned quantity alongside it.
    side = (attrs.get("buySell") or "").strip().upper()
    if side == "SELL" and quantity > 0:
        quantity = -quantity
    elif side == "BUY" and quantity < 0:
        quantity = abs(quantity)

    return ParsedExecution(
        transaction_id=transaction_id.strip(),
        symbol=symbol.strip().upper(),
        quantity=quantity,
        price=_to_decimal(_first(attrs, _PRICE_KEYS)),
        commission=_to_decimal(_first(attrs, _COMMISSION_KEYS)),
        execution_time=parse_execution_datetime(_first(attrs, _DATETIME_KEYS)),
        quantity_was_rounded=was_rounded,
    )


def parse_statement(root: ET.Element) -> list[ParsedExecution]:
    """Extract every fill from a Flex statement, de-duplicated by id.

    Duplicates within a single payload are dropped here so the batch insert
    cannot conflict with itself.
    """
    executions: list[ParsedExecution] = []
    seen: set[str] = set()

    for node_name in TRADE_NODES:
        for node in root.findall(f".//{node_name}"):
            parsed = parse_execution_node(node)
            if parsed is None:
                continue
            if parsed.transaction_id in seen:
                continue
            seen.add(parsed.transaction_id)
            executions.append(parsed)

    if not executions:
        _warn_if_fills_were_missed(root)

    return executions


def _warn_if_fills_were_missed(root: ET.Element) -> None:
    """Flag fill-shaped nodes that no known layout matched.

    An empty result is ambiguous: it means either "no trades in this period" or
    "IBKR used a node name we do not recognize". Those look identical to the
    caller, and the second silently reports a successful sync that ingested
    nothing. Naming the unmatched tags turns that into something greppable.
    """
    containers = {f"{name}s" for name in TRADE_NODES}
    unmatched = {
        el.tag
        for el in root.iter()
        if "trade" in el.tag.lower()
        and el.tag not in TRADE_NODES
        and el.tag not in containers
        and el.attrib  # containers carry no attributes; fills do
    }
    if unmatched:
        logger.warning(
            "IBKR statement contained no recognized fills, but has trade-like "
            "nodes this parser does not handle: %s. Known layouts: %s. "
            "The sync will report success having ingested nothing.",
            ", ".join(sorted(unmatched)),
            ", ".join(TRADE_NODES),
        )


def to_staging_row(execution: ParsedExecution) -> dict[str, Any]:
    """Shape a parsed execution for the ibkr_executions table."""
    return {
        "transaction_id": execution.transaction_id,
        "symbol": execution.symbol,
        "quantity": execution.quantity,  # signed
        "price": execution.price,
        "commission": execution.commission,
        "execution_time": execution.execution_time,
        "processed": False,
    }
