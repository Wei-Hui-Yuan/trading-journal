"""IBKR Flex statement parsing and normalization."""

import logging
import xml.etree.ElementTree as ET
from decimal import Decimal

import pytest

from services.ibkr_parser import (
    parse_execution_datetime,
    parse_statement,
    to_staging_row,
)

TRADES_XML = """<FlexQueryResponse><FlexStatements><FlexStatement><Trades>
 <Trade transactionID="T1001" symbol="AAPL" quantity="100" tradePrice="185.2500"
        ibCommission="-1.0500" dateTime="20250311;103045" currency="USD"/>
 <Trade transactionID="T1002" symbol="tsla" quantity="-50" tradePrice="242.1000"
        ibCommission="-0.7500" dateTime="20250311;143012"/>
</Trades></FlexStatement></FlexStatements></FlexQueryResponse>"""

# The shape the live account's saved query actually returns.
CONFIRMS_XML = """<FlexQueryResponse><FlexStatements><FlexStatement><TradeConfirms>
 <TradeConfirmation tradeID="C2001" symbol="NVDA" quantity="25" price="880.5000"
                    commission="-1.2500" buySell="BUY" dateTime="20250311;093100"/>
 <TradeConfirmation tradeID="C2002" symbol="MSFT" quantity="10" price="410.0000"
                    commission="-0.9000" buySell="SELL" dateTime="20250311;155900"/>
</TradeConfirms></FlexStatement></FlexStatements></FlexQueryResponse>"""


class TestDateTimeParsing:
    def test_semicolon_format_localized_to_market_tz(self):
        parsed = parse_execution_datetime("20250311;103045")
        assert str(parsed) == "2025-03-11 10:30:45-04:00"
        assert parsed.tzinfo.key == "America/New_York"

    def test_wall_clock_is_not_shifted(self):
        """IBKR's reported time IS market time; converting would corrupt it."""
        parsed = parse_execution_datetime("20250311;103045")
        assert (parsed.hour, parsed.minute, parsed.second) == (10, 30, 45)

    def test_dst_handled(self):
        winter = parse_execution_datetime("20250115;103045")
        summer = parse_execution_datetime("20250711;103045")
        assert winter.utcoffset().total_seconds() / 3600 == -5.0  # EST
        assert summer.utcoffset().total_seconds() / 3600 == -4.0  # EDT

    def test_date_only_anchors_to_midnight(self):
        assert str(parse_execution_datetime("20250311")) == "2025-03-11 00:00:00-04:00"

    @pytest.mark.parametrize("raw", ["not-a-date", "", None])
    def test_unparseable_returns_none(self, raw):
        assert parse_execution_datetime(raw) is None


class TestTradeNodes:
    @pytest.fixture
    def executions(self):
        return parse_statement(ET.fromstring(TRADES_XML))

    def test_extracts_both(self, executions):
        assert len(executions) == 2

    def test_symbol_uppercased(self, executions):
        assert executions[1].symbol == "TSLA"

    def test_sign_carries_the_side(self, executions):
        assert executions[0].quantity == 100
        assert executions[0].side == "BUY"
        assert executions[1].quantity == -50
        assert executions[1].side == "SELL"
        assert executions[1].abs_quantity == 50

    def test_price_and_commission(self, executions):
        assert executions[0].price == Decimal("185.2500")
        assert executions[0].commission == Decimal("-1.0500")


class TestTradeConfirmationNodes:
    """The alternate node type, with different attribute names."""

    @pytest.fixture
    def executions(self):
        return parse_statement(ET.fromstring(CONFIRMS_XML))

    def test_alternate_attribute_names(self, executions):
        assert executions[0].price == Decimal("880.5000")
        assert executions[0].commission == Decimal("-1.2500")

    def test_buysell_overrides_unsigned_quantity(self, executions):
        assert executions[0].quantity == 25  # BUY stays positive
        assert executions[1].quantity == -10  # SELL forced negative
        assert executions[1].side == "SELL"


class TestFractionalShares:
    def test_rounded_and_flagged(self, caplog):
        xml = """<FlexQueryResponse><Trade transactionID="F1" symbol="VOO"
          quantity="2.6" tradePrice="500.00" dateTime="20250311;120000"/>
        </FlexQueryResponse>"""
        with caplog.at_level(logging.WARNING, logger="services.ibkr_parser"):
            executions = parse_statement(ET.fromstring(xml))

        assert len(executions) == 1, "a fractional fill is a real trade, not a skip"
        assert executions[0].quantity == 3
        assert executions[0].quantity_was_rounded is True
        assert "Fractional share" in caplog.text
        assert "F1" in caplog.text

    def test_whole_shares_not_flagged(self):
        executions = parse_statement(ET.fromstring(TRADES_XML))
        assert executions[0].quantity_was_rounded is False


class TestResilience:
    def test_malformed_rows_skipped_not_fatal(self):
        xml = """<FlexQueryResponse>
          <Trade transactionID="" symbol="AAA" quantity="1" tradePrice="1"/>
          <Trade transactionID="G1" quantity="1" tradePrice="1"/>
          <Trade transactionID="G2" symbol="BBB" quantity="0" tradePrice="1"/>
          <Trade transactionID="G3" symbol="CCC" quantity="5" tradePrice="9.99"
                 dateTime="20250311;100000"/>
        </FlexQueryResponse>"""
        executions = parse_statement(ET.fromstring(xml))
        assert len(executions) == 1
        assert executions[0].transaction_id == "G3"

    def test_duplicate_ids_collapsed_within_payload(self):
        xml = """<FlexQueryResponse>
          <Trade transactionID="D1" symbol="AAPL" quantity="10" tradePrice="100"/>
          <Trade transactionID="D1" symbol="AAPL" quantity="10" tradePrice="100"/>
          <Trade transactionID="D2" symbol="AAPL" quantity="5" tradePrice="101"/>
        </FlexQueryResponse>"""
        executions = parse_statement(ET.fromstring(xml))
        assert sorted(e.transaction_id for e in executions) == ["D1", "D2"]

    def test_empty_statement(self):
        assert parse_statement(ET.fromstring("<FlexQueryResponse/>")) == []


class TestStagingRow:
    def test_shape_matches_table(self):
        executions = parse_statement(ET.fromstring(TRADES_XML))
        row = to_staging_row(executions[1])
        assert row["quantity"] == -50, "sign must survive into staging"
        assert row["processed"] is False
        assert sorted(row.keys()) == [
            "commission",
            "execution_time",
            "price",
            "processed",
            "quantity",
            "symbol",
            "transaction_id",
        ]
