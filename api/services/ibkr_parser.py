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
import re
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

# The one level of detail that is an actual fill. ORDER and CLOSED_LOT rows
# describe the same shares again from another angle.
LEVEL_EXECUTION = "EXECUTION"

# Attribute fallbacks, most specific first.
_ID_KEYS = ("transactionID", "tradeID", "ibExecID", "execID")
_SYMBOL_KEYS = ("symbol", "underlyingSymbol")
_QUANTITY_KEYS = ("quantity", "shares")
_PRICE_KEYS = ("tradePrice", "price")
_COMMISSION_KEYS = ("ibCommission", "commission")
_DATETIME_KEYS = ("dateTime", "tradeDate", "reportDate")
_ASSET_CLASS_KEYS = ("assetCategory", "assetClass")

# IBKR asset categories that are not tradeable positions in this journal.
# CASH is a currency conversion; the rest of an equity account's categories
# (STK, ETF, OPT, FUT) are deliberately left to pass through, so starting to
# trade options does not silently drop every fill.
SKIP_ASSET_CATEGORIES = frozenset({"CASH"})

# Currency pairs are rendered `USD.SGD`. Used only when assetCategory is
# missing, since no equity ticker takes this shape.
_CURRENCY_PAIR = re.compile(r"^[A-Z]{3}\.[A-Z]{3}$")


@dataclass(frozen=True)
class ParsedExecution:
    """One normalized fill, ready for the staging ledger."""

    transaction_id: str
    symbol: str
    # Signed: positive = bought, negative = sold. The sign is what carries the
    # side through the staging table, which has no explicit side column.
    # Decimal, not int: fractional fills are the norm on this account, and
    # rounding them either destroyed the fill or resized it (migration 010).
    quantity: Decimal
    price: Optional[Decimal]
    commission: Optional[Decimal]
    execution_time: Optional[datetime]

    @property
    def side(self) -> str:
        return "BUY" if self.quantity >= 0 else "SELL"

    @property
    def abs_quantity(self) -> Decimal:
        return abs(self.quantity)


def _is_non_tradeable(attrs: dict[str, str], symbol: str) -> bool:
    """Reject rows that are not positions in an instrument.

    A statement covering a multi-currency account is mostly funding: buying
    USD with SGD to settle a purchase appears as a `USD.SGD` "trade" with a
    zero price. Left in, each conversion becomes a position in the journal --
    on this account they outnumbered the real fills, 54 rows to 55.

    Two checks, because the two query layouts carry different evidence.
    `assetCategory` is authoritative but only present when the Flex query
    includes Asset Class; the symbol shape is the fallback for when it does
    not. Currency pairs are `AAA.BBB`, which no equity ticker resembles.
    """
    category = (_first(attrs, _ASSET_CLASS_KEYS) or "").strip().upper()
    if category and category in SKIP_ASSET_CATEGORIES:
        logger.debug("Skipping %s: asset category %s", symbol, category)
        return True

    # Only trusted when the authoritative field is absent -- an instrument
    # genuinely named like a pair should not be dropped on a hunch.
    if not category and _CURRENCY_PAIR.match(symbol.strip().upper()):
        logger.debug("Skipping %s: looks like a currency conversion", symbol)
        return True

    return False


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


def _to_signed_quantity(raw: Optional[str], transaction_id: str) -> Optional[Decimal]:
    """Parse a reported quantity, preserving fractional size exactly.

    Kept as Decimal rather than coerced to int: fractional fills are ordinary
    here, and rounding them to whole shares silently destroyed sub-half-share
    positions and inflated the rest (see migration 010).
    """
    if raw in (None, ""):
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError):
        logger.warning("Unparseable quantity %r on execution %s", raw, transaction_id)
        return None


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

    if _is_non_tradeable(attrs, symbol):
        return None

    quantity = _to_signed_quantity(_first(attrs, _QUANTITY_KEYS), transaction_id)
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
    )


def parse_statement(root: ET.Element) -> list[ParsedExecution]:
    """Extract every fill from a Flex statement, de-duplicated by id.

    Duplicates within a single payload are dropped here so the batch insert
    cannot conflict with itself.
    """
    nodes: list[ET.Element] = []
    for node_name in TRADE_NODES:
        nodes.extend(root.findall(f".//{node_name}"))

    nodes = _single_detail_level(nodes)

    executions: list[ParsedExecution] = []
    seen: set[str] = set()

    for node in nodes:
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


def count_non_tradeable(root: ET.Element) -> int:
    """How many rows were dropped for not being positions in an instrument.

    Reported by the ingest endpoint so a statement that is mostly funding
    activity says so, rather than looking like a sync that quietly lost rows.
    """
    return sum(
        1
        for node_name in TRADE_NODES
        for node in root.findall(f".//{node_name}")
        if (symbol := _first(dict(node.attrib), _SYMBOL_KEYS))
        and _is_non_tradeable(dict(node.attrib), symbol)
    )


def _single_detail_level(nodes: list[ET.Element]) -> list[ET.Element]:
    """Keep one row per fill when IBKR reports several levels of detail.

    A broadly-configured Flex query returns the same trade more than once --
    as EXECUTION, again as ORDER, again as CLOSED_LOT -- each carrying its own
    id. Deduplication by id cannot catch that, so every position would be
    inflated by the number of levels enabled.

    Execution level is the ground truth; the others are roll-ups of it. When no
    row is marked EXECUTION the list is passed through untouched, so both a
    statement with no levelOfDetail attribute at all (the TCF layout) and one
    configured purely for order-level detail still parse.
    """
    execution_level = [
        node
        for node in nodes
        if (node.get("levelOfDetail") or "").strip().upper() == LEVEL_EXECUTION
    ]

    if execution_level and len(execution_level) != len(nodes):
        logger.info(
            "IBKR statement reports %d fill rows across multiple levels of "
            "detail; keeping the %d EXECUTION rows and discarding the "
            "order/lot roll-ups that would otherwise double-count.",
            len(nodes),
            len(execution_level),
        )
        return execution_level

    return nodes


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
