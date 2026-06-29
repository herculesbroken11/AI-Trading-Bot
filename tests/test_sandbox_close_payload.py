"""Sandbox close payload tests."""

from backend.adapters.broker.tastytrade_sandbox import (
    EQUITY_SELL_CLOSE_ACTION,
    build_equity_close_payload,
)


def test_close_payload_uses_sell_to_close_and_credit():
    payload = build_equity_close_payload(symbol="TNA", quantity=1, order_type="Market")
    assert payload["order-type"] == "Market"
    assert payload["price-effect"] == "Credit"
    assert payload["legs"][0]["action"] == EQUITY_SELL_CLOSE_ACTION
    assert payload["legs"][0]["instrument-type"] == "Equity"
    assert "price" not in payload
    assert "price_effect" not in payload


def test_close_limit_payload_includes_price():
    payload = build_equity_close_payload(
        symbol="TZA", quantity=1, order_type="Limit", limit_price=2.0
    )
    assert payload["price"] == "2.00"
    assert payload["price-effect"] == "Credit"
