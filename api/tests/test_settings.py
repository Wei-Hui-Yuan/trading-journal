"""Trader-level defaults, and the risk figures a sized trade carries.

Two asymmetries drive most of these tests, and both are easy to "tidy" back
into bugs:

  * `account_size` is nullable and clearing it is a real intent; `risk_percent`
    is NOT NULL and an explicit null must be refused at the edge rather than
    reaching Postgres as an integrity error the client cannot interpret.
  * Omitting `risk_percent` on a manual trade does NOT leave it NULL --
    `trades.risk_percent` carries `default=1.00`, which SQLAlchemy applies
    whenever the value is None. So it cannot distinguish "risked 1%" from
    "never sized", and only `risk_amount` can.
"""

import os

import pytest
from pydantic import ValidationError

os.environ.setdefault("CORS_ALLOW_ORIGINS", "https://trading-journal-test.vercel.app")

import main  # noqa: E402


# ---------------------------------------------------------------------------
# SettingsUpdate — partial semantics
# ---------------------------------------------------------------------------


def test_omitted_keys_are_not_in_fields_set():
    """The update loop writes `model_fields_set`, so absence must mean absence.

    Truthiness cannot stand in for it: an account size of 0 is falsy but is a
    value the user asked for.
    """
    params = main.SettingsUpdate(risk_percent=2)
    assert params.model_fields_set == {"risk_percent"}
    assert "account_size" not in params.model_fields_set


def test_explicit_null_account_size_is_a_recorded_intent():
    """Clearing the remembered account size is distinct from not mentioning it."""
    params = main.SettingsUpdate(account_size=None)
    assert params.model_fields_set == {"account_size"}
    assert params.account_size is None


def test_zero_account_size_is_accepted_not_confused_with_absent():
    params = main.SettingsUpdate(account_size=0)
    assert params.account_size == 0
    assert "account_size" in params.model_fields_set


def test_negative_values_rejected():
    with pytest.raises(ValidationError):
        main.SettingsUpdate(account_size=-1)
    with pytest.raises(ValidationError):
        main.SettingsUpdate(risk_percent=-0.5)


def test_settings_out_tolerates_unset_account_size():
    out = main.SettingsOut(account_size=None, risk_percent=1.0, updated_at=None)
    assert out.account_size is None
    assert out.risk_percent == 1.0


def test_singleton_id_is_pinned():
    """The schema's CHECK (id = 1) is what makes GET/PUT total."""
    assert main.AppSetting.SINGLETON_ID == 1


# ---------------------------------------------------------------------------
# Manual trade — risk capture
# ---------------------------------------------------------------------------


def test_manual_trade_accepts_risk_figures():
    params = main.ManualTradeCreate(
        symbol="aapl",
        side="buy",
        quantity=6,
        price=150.25,
        risk_percent=1.0,
        risk_amount=25.0,
    )
    assert params.risk_percent == 1.0
    assert params.risk_amount == 25.0


def test_manual_trade_risk_fields_are_optional():
    """A trade logged without the calculator must still be loggable."""
    params = main.ManualTradeCreate(symbol="AAPL", side="BUY", quantity=1, price=10)
    assert params.risk_percent is None
    assert params.risk_amount is None


def test_risk_percent_bound_matches_the_widened_column():
    """Migration 015 widened trades.risk_percent to NUMERIC(6,2).

    The bound is validated here rather than at the database, so an oversized
    figure returns a 422 naming the field instead of an opaque 500. A margined
    position genuinely can exceed 100%, which is why the old (4,2) cap had to
    go rather than being clamped to.
    """
    ok = main.ManualTradeCreate(
        symbol="AAPL", side="BUY", quantity=100, price=10, risk_percent=140.5
    )
    assert ok.risk_percent == 140.5

    with pytest.raises(ValidationError):
        main.ManualTradeCreate(
            symbol="AAPL", side="BUY", quantity=1, price=10, risk_percent=10_000
        )


def test_negative_risk_rejected():
    with pytest.raises(ValidationError):
        main.ManualTradeCreate(
            symbol="AAPL", side="BUY", quantity=1, price=10, risk_amount=-5
        )


def test_column_types_match_the_validation_bounds():
    """Guards the pairing that keeps oversized risk a 422 rather than a 500."""
    assert main.Trade.__table__.c.risk_percent.type.precision == 6
    assert main.Trade.__table__.c.risk_percent.type.scale == 2
    assert main.AppSetting.__table__.c.risk_percent.nullable is False
    assert main.AppSetting.__table__.c.account_size.nullable is True
