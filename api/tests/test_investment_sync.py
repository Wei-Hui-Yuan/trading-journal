"""Mapping broker fills into the long-term book.

The risk this covers is not that a fill fails to import -- that is visible.
It is that a fill imports into the WRONG BOOK, or with the wrong sign, and
looks entirely ordinary afterwards. Seven of fourteen holdings here are also
swing-traded in the journal (AMZN, GOOGL, META, MSFT, NVDA, PANW, UNH), so
nothing in a fill identifies which book it belongs to; the separation comes
from reading a different IBKR ACCOUNT, and these tests pin the mapping that
sits behind that separation.

Nothing here touches a network or a database. The parser is exercised through
its real XML path rather than hand-built objects, so a change to how IBKR's
attributes are read shows up here too.
"""

import os
import xml.etree.ElementTree as ET
from decimal import Decimal

import pytest

os.environ.setdefault("CORS_ALLOW_ORIGINS", "https://trading-journal-test.vercel.app")

from services import ibkr_parser  # noqa: E402
from services import investment_sync as sync  # noqa: E402

# Shaped like query 1578306, the Activity Flex layout already in use -- the
# investment account's query mirrors it, so the parser needs no changes and
# these fixtures stay honest about what it will actually receive.
STATEMENT = """<FlexQueryResponse queryName="Investments" type="AF">
<FlexStatements count="1"><FlexStatement accountId="U***22222" period="LastYear">
<Trades>
 <Trade transactionID="500001" symbol="GOOGL" quantity="10" tradePrice="180.50"
        ibCommission="-1.05" dateTime="20260115;103000" buySell="BUY"
        assetCategory="STK" levelOfDetail="EXECUTION"/>
 <Trade transactionID="500002" symbol="VOO" quantity="4" tradePrice="520.25"
        ibCommission="-0.98" dateTime="20260212;140500" buySell="BUY"
        assetCategory="STK" levelOfDetail="EXECUTION"/>
 <Trade transactionID="500003" symbol="GOOGL" quantity="-3" tradePrice="205.00"
        ibCommission="-1.02" dateTime="20260410;093500" buySell="SELL"
        assetCategory="STK" levelOfDetail="EXECUTION"/>
</Trades>
</FlexStatement></FlexStatements></FlexQueryResponse>"""


def rows(xml: str = STATEMENT) -> list[dict]:
    executions = ibkr_parser.parse_statement(ET.fromstring(xml))
    return sync.to_investment_transactions(executions)


def by_id(external_id: str, source: str = STATEMENT) -> dict:
    return next(r for r in rows(source) if r["external_id"] == external_id)


# ---------------------------------------------------------------------------
# The sign convention, which decides every derived figure
# ---------------------------------------------------------------------------


def test_a_buy_is_money_leaving_including_fees():
    """10 x 180.50 = 1805.00 plus 1.05 commission. The derived cost basis --
    and therefore average cost, unrealised P&L and the discount to intrinsic
    value -- all rest on this one figure."""
    row = by_id("IBKR-500001")
    assert row["transaction_type"] == "BUY"
    assert row["quantity"] == Decimal("10")
    assert row["total_amount"] == pytest.approx(Decimal("-1806.05"))


def test_a_sell_is_money_arriving_less_fees():
    row = by_id("IBKR-500003")
    assert row["transaction_type"] == "SELL"
    assert row["quantity"] == Decimal("3")
    assert row["total_amount"] == pytest.approx(Decimal("613.98"))


def test_the_total_matches_what_the_manual_path_would_compute():
    """A synced row and a hand-entered row describing the same purchase must
    agree exactly. If they diverge, average cost depends on how the row got
    into the ledger, which is not a property anyone would expect."""
    import main

    row = by_id("IBKR-500001")
    manual = main.TransactionCreate(
        ticker="GOOGL", transaction_type="BUY", quantity=10, price=180.50,
        fees=1.05, transaction_date=row["transaction_date"],
    )
    assert float(row["total_amount"]) == pytest.approx(manual.resolved_total())


def test_a_sell_agrees_with_the_manual_path_too():
    import main

    row = by_id("IBKR-500003")
    manual = main.TransactionCreate(
        ticker="GOOGL", transaction_type="SELL", quantity=3, price=205.00,
        fees=1.02, transaction_date=row["transaction_date"],
    )
    assert float(row["total_amount"]) == pytest.approx(manual.resolved_total())


def test_quantity_is_a_magnitude_and_the_side_carries_direction():
    """IBKR signs quantity; this ledger has an explicit type column and a
    positive-quantity CHECK constraint (migration 026). A negative quantity
    would be rejected by the database rather than silently stored."""
    for row in rows():
        assert row["quantity"] > 0


def test_a_commission_rebate_stays_a_rebate():
    """Ten of 328 fills on the trading account report a POSITIVE ibCommission
    -- tiered pricing passing an exchange rebate through. That is money
    received, and booking it as a charge would get the sign wrong on the one
    figure fees exist to make honest."""
    rebate = STATEMENT.replace('ibCommission="-1.05"', 'ibCommission="0.40"')
    row = by_id("IBKR-500001", rebate)
    assert row["fees"] == Decimal("-0.40")
    # 1805.00 gross, less a 0.40 rebate received.
    assert row["total_amount"] == pytest.approx(Decimal("-1804.60"))


# ---------------------------------------------------------------------------
# Identity and idempotency
# ---------------------------------------------------------------------------


def test_the_external_id_is_namespaced():
    """`investment_transactions.external_id` is UNIQUE, and the prefix is what
    keeps a broker id from ever colliding with anything else written there."""
    assert by_id("IBKR-500001")["external_id"] == "IBKR-500001"
    assert sync.external_id_for("900") == "IBKR-900"


def test_every_row_is_marked_as_broker_sourced():
    """Distinguishable from a hand-entered row forever, which is what lets the
    ledger UI say where a transaction came from."""
    for row in rows():
        assert row["source"] == "IBKR"


def test_a_fill_repeated_within_one_response_is_mapped_once():
    """Two overlapping Flex queries can describe the same fill. A batch insert
    that conflicted with itself would fail the whole sync rather than the one
    row."""
    doubled = STATEMENT.replace(
        '</Trades>',
        ' <Trade transactionID="500001" symbol="GOOGL" quantity="10"'
        ' tradePrice="180.50" ibCommission="-1.05" dateTime="20260115;103000"'
        ' buySell="BUY" assetCategory="STK" levelOfDetail="EXECUTION"/>\n</Trades>'
    )
    assert len(rows(doubled)) == 3
    assert len({r["external_id"] for r in rows(doubled)}) == 3


# ---------------------------------------------------------------------------
# What must not be imported
# ---------------------------------------------------------------------------


def test_an_undated_fill_is_skipped_rather_than_dated_now():
    """Average cost is computed by walking the ledger in date order. Stamping
    a real purchase with whenever the sync ran would place it wrongly in that
    walk and quietly change the cost basis of everything after it."""
    undated = STATEMENT.replace(' dateTime="20260115;103000"', '')
    ids = {r["external_id"] for r in rows(undated)}
    assert "IBKR-500001" not in ids
    assert len(ids) == 2


def test_an_unpriced_fill_is_skipped_rather_than_priced_at_zero():
    """A zero-price purchase would read as free shares and understate the
    cost basis for as long as the holding is held."""
    unpriced = STATEMENT.replace(' tradePrice="180.50"', '')
    ids = {r["external_id"] for r in rows(unpriced)}
    assert "IBKR-500001" not in ids


def test_a_currency_conversion_is_not_a_holding():
    """The parser drops CASH rows; a multi-currency account is mostly funding
    activity, and importing a USD.SGD conversion as a position would invent a
    holding in a ticker that is not a company."""
    with_fx = STATEMENT.replace(
        '</Trades>',
        ' <Trade transactionID="500009" symbol="USD.SGD" quantity="1000"'
        ' tradePrice="1.34" dateTime="20260115;110000" buySell="BUY"'
        ' assetCategory="CASH" levelOfDetail="EXECUTION"/>\n</Trades>'
    )
    assert "IBKR-500009" not in {r["external_id"] for r in rows(with_fx)}


def test_an_empty_statement_maps_to_nothing_rather_than_failing():
    empty = """<FlexQueryResponse queryName="Investments" type="AF">
<FlexStatements count="1"><FlexStatement accountId="U***22222" period="LastYear">
<Trades></Trades></FlexStatement></FlexStatements></FlexQueryResponse>"""
    assert rows(empty) == []


# ---------------------------------------------------------------------------
# The guardrail
# ---------------------------------------------------------------------------


def test_the_module_reaches_no_database_and_no_network():
    """It maps and returns dictionaries. The endpoint decides what to persist,
    which is what stops a broker-format surprise from reaching either book's
    tables.

    Checked through the import graph rather than by searching the text: the
    module's own docstring says "holds no database session", and a substring
    search flags that sentence as a violation of the rule it is describing.
    """
    import ast
    import inspect

    imported: set[str] = set()
    for node in ast.walk(ast.parse(inspect.getsource(sync))):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    for forbidden in ("main", "sqlalchemy", "httpx", "asyncpg"):
        assert forbidden not in imported, (
            f"investment_sync must not import {forbidden} -- it maps and "
            f"returns, and the endpoint decides what to persist"
        )


def test_the_sync_never_writes_a_trading_table():
    """The whole point of a parallel pipeline. If this fails, an investment
    sync has grown the ability to modify the journal."""
    import ast
    import inspect

    import main

    source = inspect.getsource(main)
    tree = ast.parse(source)
    endpoint = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "sync_investment_transactions"
    )

    referenced = {
        n.id for n in ast.walk(endpoint) if isinstance(n, ast.Name)
    } | {
        n.attr for n in ast.walk(endpoint) if isinstance(n, ast.Attribute)
    }
    for forbidden in ("Trade", "Position", "PositionFill", "RealizedLeg",
                      "PlannedTrade", "IBKRExecution", "SuppressedExecution",
                      "ingest_ibkr", "run_matching_for_ticker"):
        assert forbidden not in referenced, (
            f"the investment sync must not reference {forbidden}"
        )


def test_the_trading_ingest_does_not_know_this_exists():
    """The other direction, and the one that would actually break the journal:
    ingest_ibkr must be unable to reach investment code."""
    import ast
    import inspect

    import main

    tree = ast.parse(inspect.getsource(main))
    ingest = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "ingest_ibkr"
    )

    referenced = {
        n.id for n in ast.walk(ingest) if isinstance(n, ast.Name)
    } | {
        n.attr for n in ast.walk(ingest) if isinstance(n, ast.Attribute)
    }
    for forbidden in ("InvestmentTransaction", "InvestmentHolding",
                      "investment_sync", "sync_investment_transactions"):
        assert forbidden not in referenced, (
            f"ingest_ibkr must not reference {forbidden}"
        )
