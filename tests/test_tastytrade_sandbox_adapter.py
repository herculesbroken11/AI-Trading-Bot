"""TastytradeSandboxAdapter unit tests (mocked httpx)."""

import json
from unittest.mock import MagicMock, patch

import pytest

from backend.adapters.broker.sandbox_auth import SandboxAuthError, SandboxOAuthClient
from backend.adapters.broker.tastytrade_sandbox import SandboxApiError, TastytradeSandboxAdapter
from backend.config.settings import Settings
from backend.config.tastytrade_urls import SANDBOX_BASE_URL
from backend.risk.models import OrderIntent


def _settings(**overrides) -> Settings:
    base = {
        "live_trading_enabled": False,
        "trading_mode": "sandbox",
        "tastytrade_env": "sandbox",
        "emergency_halt": False,
        "tastytrade_client_id": "cid",
        "tastytrade_client_secret": "secret",
        "tastytrade_refresh_token": "refresh",
    }
    base.update(overrides)
    return Settings(**base)


def test_adapter_refuses_non_sandbox_env():
    with pytest.raises(SandboxAuthError):
        TastytradeSandboxAdapter(_settings(tastytrade_env="production"))


def test_adapter_base_url_is_sandbox_only():
    auth = MagicMock(spec=SandboxOAuthClient)
    auth.is_authenticated = True
    auth.request_headers.return_value = {"Authorization": "Bearer x", "User-Agent": "AI-Trading-Bot/0.1"}
    adapter = TastytradeSandboxAdapter(_settings(), auth=auth)
    assert adapter.base_url == SANDBOX_BASE_URL


@patch("backend.adapters.broker.sandbox_auth.httpx.Client")
def test_get_headers_never_logs_real_token(mock_client_cls):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"access_token": "super-secret-token"}

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.post.return_value = mock_response
    mock_client_cls.return_value = mock_client

    client = SandboxOAuthClient(_settings())
    client.ensure_authenticated()
    headers = client.get_headers()
    assert "super-secret-token" not in str(headers)
    assert headers["Authorization"] == "Bearer [REDACTED]"


@patch("backend.adapters.broker.tastytrade_sandbox.httpx.Client")
def test_get_accounts_parses_response(mock_client_cls):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "data": {"items": [{"account-number": "5WT00001"}]}
    }
    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.request.return_value = mock_response
    mock_client_cls.return_value = mock_client

    auth = MagicMock(spec=SandboxOAuthClient)
    auth.request_headers.return_value = {"Authorization": "Bearer x"}
    adapter = TastytradeSandboxAdapter(_settings(), auth=auth)

    accounts = adapter.get_accounts()
    assert len(accounts) == 1
    called_url = mock_client.request.call_args[0][1]
    assert called_url == f"{SANDBOX_BASE_URL}/customers/me/accounts"
    assert mock_client.request.call_count == 1


@patch("backend.adapters.broker.tastytrade_sandbox.httpx.Client")
def test_get_accounts_empty_does_not_call_top_level_accounts(mock_client_cls):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"data": {"items": []}}

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.request.return_value = mock_response
    mock_client_cls.return_value = mock_client

    auth = MagicMock(spec=SandboxOAuthClient)
    auth.request_headers.return_value = {"Authorization": "Bearer x", "User-Agent": "AI-Trading-Bot/0.1"}
    adapter = TastytradeSandboxAdapter(_settings(), auth=auth)

    accounts = adapter.get_accounts()
    assert accounts == []
    assert mock_client.request.call_count == 1
    called_url = mock_client.request.call_args[0][1]
    assert called_url == f"{SANDBOX_BASE_URL}/customers/me/accounts"


@patch("backend.adapters.broker.tastytrade_sandbox.httpx.Client")
def test_get_accounts_403_reports_customers_me_accounts_endpoint(mock_client_cls):
    response = MagicMock()
    response.status_code = 403
    response.text = '{"error":{"message":"User not permitted access"}}'
    response.json.return_value = {"error": {"message": "User not permitted access"}}

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.request.return_value = response
    mock_client_cls.return_value = mock_client

    auth = MagicMock(spec=SandboxOAuthClient)
    auth.request_headers.return_value = {"Authorization": "Bearer x", "User-Agent": "AI-Trading-Bot/0.1"}
    adapter = TastytradeSandboxAdapter(_settings(), auth=auth)

    with pytest.raises(SandboxApiError) as exc:
        adapter.get_accounts()

    assert exc.value.step_diagnostics is not None
    assert exc.value.step_diagnostics.endpoint_path == "/customers/me/accounts"
    assert exc.value.step_diagnostics.step == "get_accounts"


def test_get_accounts_does_not_use_top_level_accounts_listing():
    import inspect

    from backend.adapters.broker import tastytrade_sandbox as mod

    source = inspect.getsource(mod.TastytradeSandboxAdapter.get_accounts)
    assert "/customers/me/accounts" in source
    assert '"/accounts"' not in source
    assert "'/accounts'" not in source


@patch("backend.adapters.broker.tastytrade_sandbox.httpx.Client")
def test_submit_order_422_includes_redacted_response_json(mock_client_cls):
    accounts_response = MagicMock()
    accounts_response.status_code = 200
    accounts_response.json.return_value = {"data": {"items": [{"account-number": "5WT00001"}]}}

    order_response = MagicMock()
    order_response.status_code = 422
    order_response.text = json.dumps(
        {
            "error": {
                "code": "preflight_check_failure",
                "message": "One or more preflight checks failed",
            },
            "errors": [{"code": "buying_power", "message": "Insufficient buying power"}],
            "warnings": [{"code": "tif_next_valid_session", "message": "Next session"}],
        }
    )
    order_response.json.return_value = json.loads(order_response.text)

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.request.side_effect = [accounts_response, order_response]
    mock_client_cls.return_value = mock_client

    auth = MagicMock(spec=SandboxOAuthClient)
    auth.request_headers.return_value = {
        "Authorization": "Bearer secret-access-token-xyz",
        "User-Agent": "AI-Trading-Bot/0.1",
    }
    adapter = TastytradeSandboxAdapter(_settings(), auth=auth)

    with pytest.raises(SandboxApiError) as exc:
        adapter.submit_equity_order(None, "TNA", "buy", 1)

    diag = exc.value.step_diagnostics
    assert diag is not None
    assert diag.step == "submit_order"
    assert diag.endpoint_path == "/accounts/5WT00001/orders"
    assert diag.provider_error_code == "preflight_check_failure"
    assert diag.redacted_response_json is not None
    safe = diag.format_safe()
    assert "response_json:" in safe
    assert "preflight_check_failure" in safe
    assert "secret-access-token-xyz" not in safe


@patch("backend.adapters.broker.tastytrade_sandbox.httpx.Client")
def test_dry_run_uses_dry_run_endpoint(mock_client_cls):
    dry_response = MagicMock()
    dry_response.status_code = 200
    dry_response.json.return_value = {"data": {"buying-power-effect": {}}}

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.request.return_value = dry_response
    mock_client_cls.return_value = mock_client

    auth = MagicMock(spec=SandboxOAuthClient)
    auth.request_headers.return_value = {"Authorization": "Bearer x", "User-Agent": "AI-Trading-Bot/0.1"}
    adapter = TastytradeSandboxAdapter(_settings(), auth=auth)

    adapter.dry_run_equity_order("5WM30541", "TNA", "buy", 1)

    called_url = mock_client.request.call_args[0][1]
    assert called_url == f"{SANDBOX_BASE_URL}/accounts/5WM30541/orders/dry-run"
    payload = mock_client.request.call_args.kwargs["json"]
    assert payload["order-type"] == "Market"
    assert payload["price-effect"] == "Debit"
    assert payload["legs"][0]["action"] == "Buy to Open"
    assert "price" not in payload
    assert "price_effect" not in payload


@patch("backend.adapters.broker.tastytrade_sandbox.httpx.Client")
def test_submit_order_uses_submit_endpoint(mock_client_cls):
    submit_response = MagicMock()
    submit_response.status_code = 200
    submit_response.json.return_value = {"data": {"order": {"id": "123"}}}

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.request.return_value = submit_response
    mock_client_cls.return_value = mock_client

    auth = MagicMock(spec=SandboxOAuthClient)
    auth.request_headers.return_value = {"Authorization": "Bearer x", "User-Agent": "AI-Trading-Bot/0.1"}
    adapter = TastytradeSandboxAdapter(_settings(), auth=auth)

    adapter.submit_equity_order("5WM30541", "TZA", "buy", 1, order_type="Limit", limit_price=2.0)

    called_url = mock_client.request.call_args[0][1]
    assert called_url == f"{SANDBOX_BASE_URL}/accounts/5WM30541/orders"
    payload = mock_client.request.call_args.kwargs["json"]
    assert payload["price"] == "2.00"
    assert payload["price-effect"] == "Debit"
    assert payload["legs"][0]["action"] == "Buy to Open"
    assert "price_effect" not in payload


@patch("backend.adapters.broker.tastytrade_sandbox.httpx.Client")
def test_get_customers_me_403_includes_step_diagnostics(mock_client_cls):
    response = MagicMock()
    response.status_code = 403
    response.text = '{"error":{"message":"Forbidden resource"}}'
    response.json.return_value = {"error": {"message": "Forbidden resource"}}

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.request.return_value = response
    mock_client_cls.return_value = mock_client

    auth = MagicMock(spec=SandboxOAuthClient)
    auth.request_headers.return_value = {
        "Authorization": "Bearer secret-access-token-value",
        "User-Agent": "AI-Trading-Bot/0.1",
    }
    adapter = TastytradeSandboxAdapter(_settings(), auth=auth)

    with pytest.raises(SandboxApiError) as exc:
        adapter.get_customers_me()

    diag = exc.value.step_diagnostics
    assert diag is not None
    assert diag.step == "get_customers_me"
    assert diag.status_code == 403
    assert diag.endpoint_path == "/customers/me"
    assert diag.authorization_present is True
    assert diag.user_agent_present is True
    safe = diag.format_safe()
    assert "secret-access-token-value" not in safe
    assert "Bearer secret" not in safe
    assert "403 usually means" in safe


@patch("backend.adapters.broker.tastytrade_sandbox.httpx.Client")
def test_get_order_returns_summary(mock_client_cls):
    order_response = MagicMock()
    order_response.status_code = 200
    order_response.json.return_value = {
        "data": {
            "id": 1159360,
            "status": "Filled",
            "order-type": "Limit",
            "price": "2.00",
            "legs": [{"symbol": "TNA", "quantity": 1, "action": "Buy to Open"}],
        }
    }

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.request.return_value = order_response
    mock_client_cls.return_value = mock_client

    auth = MagicMock(spec=SandboxOAuthClient)
    auth.request_headers.return_value = {"Authorization": "Bearer x", "User-Agent": "AI-Trading-Bot/0.1"}
    adapter = TastytradeSandboxAdapter(_settings(), auth=auth)

    summary = adapter.fetch_order_status_summary("5WM30541", "1159360")
    assert summary["broker_order_id"] == "1159360"
    assert summary["status"] == "filled"
    called_url = mock_client.request.call_args[0][1]
    assert called_url == f"{SANDBOX_BASE_URL}/accounts/5WM30541/orders/1159360"
    assert "api.tastyworks.com" not in called_url or "cert.tastyworks" in called_url


@patch("backend.adapters.broker.tastytrade_sandbox.httpx.Client")
def test_dry_run_close_uses_dry_run_endpoint(mock_client_cls):
    dry_response = MagicMock()
    dry_response.status_code = 200
    dry_response.json.return_value = {"data": {}}

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.request.return_value = dry_response
    mock_client_cls.return_value = mock_client

    auth = MagicMock(spec=SandboxOAuthClient)
    auth.request_headers.return_value = {"Authorization": "Bearer x", "User-Agent": "AI-Trading-Bot/0.1"}
    adapter = TastytradeSandboxAdapter(_settings(), auth=auth)

    adapter.dry_run_equity_close("5WM30541", "TNA", 1)

    called_url = mock_client.request.call_args[0][1]
    assert called_url == f"{SANDBOX_BASE_URL}/accounts/5WM30541/orders/dry-run"
    payload = mock_client.request.call_args.kwargs["json"]
    assert payload["legs"][0]["action"] == "Sell to Close"
    assert payload["price-effect"] == "Credit"


@patch("backend.adapters.broker.tastytrade_sandbox.httpx.Client")
def test_list_live_orders_uses_live_endpoint(mock_client_cls):
    live_response = MagicMock()
    live_response.status_code = 200
    live_response.json.return_value = {
        "data": {
            "items": [
                {
                    "id": 115959,
                    "status": "Live",
                    "order-type": "Limit",
                    "price": "2.00",
                    "legs": [{"symbol": "TNA", "quantity": 1, "action": "Buy to Open"}],
                }
            ]
        }
    }

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.request.return_value = live_response
    mock_client_cls.return_value = mock_client

    auth = MagicMock(spec=SandboxOAuthClient)
    auth.request_headers.return_value = {"Authorization": "Bearer x", "User-Agent": "AI-Trading-Bot/0.1"}
    adapter = TastytradeSandboxAdapter(_settings(), auth=auth)

    items = adapter.list_live_orders("5WM30541")
    assert len(items) == 1
    called_url = mock_client.request.call_args[0][1]
    assert called_url == f"{SANDBOX_BASE_URL}/accounts/5WM30541/orders/live"
    assert "cert.tastyworks" in called_url


@patch("backend.adapters.broker.tastytrade_sandbox.httpx.Client")
def test_cancel_order_uses_delete_endpoint(mock_client_cls):
    cancel_response = MagicMock()
    cancel_response.status_code = 200
    cancel_response.content = b'{"data":{"cancelled":true}}'
    cancel_response.json.return_value = {"data": {"cancelled": True}}

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.request.return_value = cancel_response
    mock_client_cls.return_value = mock_client

    auth = MagicMock(spec=SandboxOAuthClient)
    auth.request_headers.return_value = {"Authorization": "Bearer x", "User-Agent": "AI-Trading-Bot/0.1"}
    adapter = TastytradeSandboxAdapter(_settings(), auth=auth)

    result = adapter.cancel_order("5WM30541", "115959")
    assert result["data"]["cancelled"] is True
    assert mock_client.request.call_args[0][0] == "DELETE"
    called_url = mock_client.request.call_args[0][1]
    assert called_url == f"{SANDBOX_BASE_URL}/accounts/5WM30541/orders/115959"


@patch("backend.adapters.broker.tastytrade_sandbox.httpx.Client")
def test_execute_order_live_status_has_no_fill_price(mock_client_cls):
    submit_response = MagicMock()
    submit_response.status_code = 200
    submit_response.json.return_value = {"data": {"order": {"id": "789", "status": "Live"}}}

    status_response = MagicMock()
    status_response.status_code = 200
    status_response.json.return_value = {
        "data": {
            "id": 789,
            "status": "Live",
            "order-type": "Limit",
            "price": "2.00",
            "legs": [{"symbol": "TNA", "quantity": 1, "action": "Buy to Open"}],
        }
    }

    accounts_response = MagicMock()
    accounts_response.status_code = 200
    accounts_response.json.return_value = {
        "data": {"items": [{"account-number": "5WM30541"}]}
    }

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.request.side_effect = [accounts_response, submit_response, status_response]
    mock_client_cls.return_value = mock_client

    auth = MagicMock(spec=SandboxOAuthClient)
    auth.request_headers.return_value = {"Authorization": "Bearer x", "User-Agent": "AI-Trading-Bot/0.1"}
    adapter = TastytradeSandboxAdapter(_settings(), auth=auth)

    intent = OrderIntent(
        symbol="TNA",
        side="buy",
        quantity=1,
        trading_mode="sandbox",
        order_type="Limit",
        limit_price=2.0,
        current_price=50.0,
    )
    result = adapter.execute_order(intent)

    assert result.success is True
    assert result.status == "submitted"
    assert result.fill_price is None
    assert result.raw["broker_status"] == "Live"
    assert result.raw["limit_price"] == 2.0


@patch("backend.adapters.broker.tastytrade_sandbox.httpx.Client")
def test_execute_order_passes_limit_price(mock_client_cls):
    submit_response = MagicMock()
    submit_response.status_code = 200
    submit_response.json.return_value = {"data": {"order": {"id": "456", "status": "Filled"}}}

    status_response = MagicMock()
    status_response.status_code = 200
    status_response.json.return_value = {
        "data": {
            "id": 456,
            "status": "Filled",
            "order-type": "Limit",
            "price": "2.00",
            "legs": [{"symbol": "TZA", "quantity": 1, "action": "Buy to Open"}],
        }
    }

    accounts_response = MagicMock()
    accounts_response.status_code = 200
    accounts_response.json.return_value = {
        "data": {"items": [{"account-number": "5WM30541"}]}
    }

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.request.side_effect = [accounts_response, submit_response, status_response]
    mock_client_cls.return_value = mock_client

    auth = MagicMock(spec=SandboxOAuthClient)
    auth.request_headers.return_value = {"Authorization": "Bearer x", "User-Agent": "AI-Trading-Bot/0.1"}
    adapter = TastytradeSandboxAdapter(_settings(), auth=auth)

    intent = OrderIntent(
        symbol="TZA",
        side="buy",
        quantity=1,
        trading_mode="sandbox",
        order_type="Limit",
        limit_price=2.0,
        current_price=50.0,
    )
    result = adapter.execute_order(intent)

    assert result.success is True
    submit_payload = mock_client.request.call_args_list[1].kwargs["json"]
    assert submit_payload["price"] == "2.00"
    assert submit_payload["price-effect"] == "Debit"
    assert submit_payload["order-type"] == "Limit"
    assert result.raw["broker_order_id"] == "456"


def test_execute_order_rejects_sell():
    auth = MagicMock(spec=SandboxOAuthClient)
    adapter = TastytradeSandboxAdapter(_settings(), auth=auth)
    intent = OrderIntent(
        symbol="TNA", side="buy", quantity=1, trading_mode="sandbox", current_price=50.0
    )
    # force sell at submit level
    with pytest.raises(SandboxApiError):
        adapter.submit_equity_order(None, "TNA", "sell", 1)
