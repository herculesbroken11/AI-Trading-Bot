"""Sandbox order status parsing and position verification tests."""

from backend.adapters.broker.sandbox_order_verification import (
    broker_order_id_from_execution,
    extract_broker_order_id,
    format_order_status_summary,
    map_broker_status_to_record_status,
    summarize_order_response,
    verify_buy_position_present,
    verify_position_closed,
)


def test_extract_broker_order_id_from_submit_response():
    response = {"data": {"order": {"id": 1159360, "status": "Filled"}}}
    assert extract_broker_order_id(response) == "1159360"


def test_summarize_order_response_safe_fields():
    response = {
        "data": {
            "id": 1159360,
            "status": "Filled",
            "order-type": "Limit",
            "price": "2.00",
            "legs": [
                {
                    "symbol": "TNA",
                    "quantity": 1,
                    "action": "Buy to Open",
                }
            ],
        }
    }
    summary = summarize_order_response(response)
    assert summary["broker_order_id"] == "1159360"
    assert summary["broker_status"] == "Filled"
    assert summary["status"] == "filled"
    assert summary["symbol"] == "TNA"
    assert summary["side"] == "buy"
    assert summary["order_type"] == "Limit"
    assert summary["limit_price"] == 2.0
    assert summary["trading_mode"] == "sandbox"


def test_broker_order_id_from_execution_strips_sandbox_prefix():
    assert broker_order_id_from_execution("SANDBOX-1159360", None) == "1159360"
    assert broker_order_id_from_execution("SANDBOX-1159360", {"broker_order_id": "999"}) == "999"


def test_format_order_status_summary_redacts_nothing_sensitive():
    summary = {
        "broker_order_id": "1159360",
        "broker_status": "Filled",
        "status": "filled",
        "symbol": "TNA",
        "side": "buy",
        "quantity": 1,
        "order_type": "Limit",
        "limit_price": 2.0,
        "fill_price": 2.0,
        "trading_mode": "sandbox",
    }
    text = format_order_status_summary(summary)
    assert "broker_order_id: 1159360" in text
    assert "access_token" not in text
    assert "Bearer" not in text


def test_map_broker_status_live_to_submitted():
    assert map_broker_status_to_record_status("Live") == "submitted"


def test_verify_buy_position_lag_warning():
    ok, message = verify_buy_position_present([], "TNA")
    assert ok is False
    assert "warning" in message
    assert "lag" in message


def test_verify_position_closed_when_missing():
    ok, message = verify_position_closed([], "TNA")
    assert ok is True
    assert "position_closed" in message


def test_verify_position_close_lag_warning():
    positions = [{"symbol": "TNA", "quantity": 1}]
    ok, message = verify_position_closed(positions, "TNA")
    assert ok is False
    assert "warning" in message
    assert "lag" in message
