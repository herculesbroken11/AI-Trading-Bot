"""Sandbox equity order payload and endpoint tests."""

import json

import pytest

from backend.adapters.broker.tastytrade_sandbox import (
    EQUITY_BUY_ACTION,
    SandboxApiError,
    TastytradeSandboxAdapter,
    _coerce_amount,
    build_equity_order_payload,
)


def test_market_payload_uses_dashed_keys_and_buy_to_open():
    payload = build_equity_order_payload(symbol="TNA", quantity=1, order_type="Market")
    assert payload["time-in-force"] == "Day"
    assert payload["order-type"] == "Market"
    assert "price" not in payload
    assert "price-effect" not in payload
    leg = payload["legs"][0]
    assert leg["instrument-type"] == "Equity"
    assert leg["symbol"] == "TNA"
    assert leg["quantity"] == 1
    assert leg["action"] == EQUITY_BUY_ACTION
    assert leg["action"] == "Buy to Open"


def test_limit_payload_includes_price_string():
    payload = build_equity_order_payload(
        symbol="TZA", quantity=1, order_type="Limit", limit_price=2.0
    )
    assert payload["order-type"] == "Limit"
    assert payload["price"] == "2.00"
    assert payload["legs"][0]["symbol"] == "TZA"


def test_limit_requires_price():
    with pytest.raises(SandboxApiError):
        build_equity_order_payload(symbol="TNA", quantity=1, order_type="Limit")


def test_payload_does_not_use_plain_buy_action():
    payload = build_equity_order_payload(symbol="TNA", quantity=1)
    assert payload["legs"][0]["action"] != "Buy"


def test_coerce_amount_parses_string_numbers():
    assert _coerce_amount("100000.0") == 100000.0
    assert _coerce_amount("0") == 0.0
    assert _coerce_amount(None) is None
