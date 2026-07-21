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

# A type="TCF" query emits <TradeConfirm> -- singular, and distinct from
# <TradeConfirmation>. Missing this tag parsed a real statement to zero fills
# and reported the sync as successful. No commission attribute at all here,
# which is how IBKR renders this layout unless the query asks for it.
TCF_XML = """<FlexQueryResponse queryName="Dashboard_Trades_Sync" type="TCF">
<FlexStatements count="1"><FlexStatement accountId="U***11111" period="Today">
<TradeConfirms>
 <TradeConfirm dateTime="20260720;093927" execID="000a.000b.01.01" tradeID="900001"
               price="100" quantity="3" currency="USD" symbol="ZZTEST"
               transactionType="ExchTrade" tradeDate="20260720" buySell="BUY"/>
 <TradeConfirm dateTime="20260720;094925" execID="000c.000d.01.01" tradeID="900002"
               price="95.5" quantity="-2" currency="USD" symbol="ZZTEST"
               transactionType="ExchTrade" tradeDate="20260720" buySell="SELL"/>
</TradeConfirms>
</FlexStatement></FlexStatements></FlexQueryResponse>"""


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


class TestTradeConfirmNodes:
    """type="TCF" layout -- <TradeConfirm>, not <TradeConfirmation>."""

    @pytest.fixture
    def executions(self):
        return parse_statement(ET.fromstring(TCF_XML))

    def test_fills_are_found(self, executions):
        """The regression: this parsed to 0 and the sync claimed success."""
        assert len(executions) == 2

    def test_falls_back_to_tradeid_for_identity(self, executions):
        assert sorted(e.transaction_id for e in executions) == ["900001", "900002"]

    def test_sign_and_side(self, executions):
        buy, sell = executions[0], executions[1]
        assert (buy.quantity, buy.side) == (3, "BUY")
        assert (sell.quantity, sell.side) == (-2, "SELL")

    def test_absent_commission_is_null_not_zero(self, executions):
        """None means unknown; 0 would assert the trade was free."""
        assert executions[0].commission is None

    def test_price_and_time(self, executions):
        assert executions[0].price == Decimal("100")
        assert str(executions[0].execution_time) == "2026-07-20 09:39:27-04:00"

    def test_container_node_is_not_mistaken_for_a_fill(self, executions):
        """<TradeConfirms> wraps <TradeConfirm>; only the children are fills."""
        assert len(executions) == 2


class TestLevelOfDetail:
    """A broad Flex query reports each fill at several levels of detail."""

    MULTI_LEVEL = """<FlexQueryResponse><FlexStatements><FlexStatement><Trades>
     <Trade tradeID="88001" symbol="ZZLVL" quantity="3" tradePrice="890"
            dateTime="20260720;093927" buySell="BUY" levelOfDetail="EXECUTION"/>
     <Trade tradeID="88999" symbol="ZZLVL" quantity="3" tradePrice="890"
            dateTime="20260720;093927" buySell="BUY" levelOfDetail="ORDER"/>
     <Trade tradeID="88500" symbol="ZZLVL" quantity="3" tradePrice="890"
            dateTime="20260720;093927" buySell="BUY" levelOfDetail="CLOSED_LOT"/>
    </Trades></FlexStatement></FlexStatements></FlexQueryResponse>"""

    def test_roll_up_rows_do_not_multiply_the_position(self):
        """Each level carries its own id, so dedup by id cannot catch this."""
        executions = parse_statement(ET.fromstring(self.MULTI_LEVEL))
        assert len(executions) == 1
        assert sum(e.quantity for e in executions) == 3, "3 shares traded, not 9"
        assert executions[0].transaction_id == "88001"

    def test_layout_without_the_attribute_is_untouched(self):
        """The TCF statements this account actually gets carry no level."""
        assert len(parse_statement(ET.fromstring(TCF_XML))) == 2

    def test_order_level_only_still_parses(self):
        """Filtering must not empty a query configured for order detail."""
        xml = """<FlexQueryResponse><Trades>
          <Trade tradeID="1" symbol="AAA" quantity="5" tradePrice="10"
                 dateTime="20260720;100000" buySell="BUY" levelOfDetail="ORDER"/>
        </Trades></FlexQueryResponse>"""
        assert len(parse_statement(ET.fromstring(xml))) == 1


class TestUnrecognizedLayout:
    def test_unknown_trade_node_is_logged_loudly(self, caplog):
        """A layout we do not handle must not fail silently."""
        xml = """<FlexQueryResponse><TradeSomethingElse tradeID="X1" symbol="AAA"
                 quantity="5" price="10" dateTime="20260720;100000"/>
              </FlexQueryResponse>"""
        with caplog.at_level(logging.WARNING, logger="services.ibkr_parser"):
            assert parse_statement(ET.fromstring(xml)) == []
        assert "TradeSomethingElse" in caplog.text
        assert "ingested nothing" in caplog.text

    def test_genuinely_empty_statement_stays_quiet(self, caplog):
        """No trades in the period is normal; do not cry wolf."""
        xml = """<FlexQueryResponse><FlexStatements><FlexStatement>
                 <TradeConfirms/></FlexStatement></FlexStatements></FlexQueryResponse>"""
        with caplog.at_level(logging.WARNING, logger="services.ibkr_parser"):
            assert parse_statement(ET.fromstring(xml)) == []
        assert "ingested nothing" not in caplog.text


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
