"""TastytradeSandboxAdapter unit tests (mocked httpx)."""

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
    assert called_url.startswith(SANDBOX_BASE_URL)
    assert "api.tastyworks.com" not in called_url or "cert.tastyworks" in called_url


@patch("backend.adapters.broker.tastytrade_sandbox.httpx.Client")
def test_submit_order_422_message(mock_client_cls):
    accounts_response = MagicMock()
    accounts_response.status_code = 200
    accounts_response.json.return_value = {"data": {"items": [{"account-number": "5WT00001"}]}}

    order_response = MagicMock()
    order_response.status_code = 422
    order_response.text = '{"error":{"message":"instrumentation lag"}}'
    order_response.json.return_value = {"error": {"message": "instrumentation lag"}}

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.request.side_effect = [accounts_response, order_response]
    mock_client_cls.return_value = mock_client

    auth = MagicMock(spec=SandboxOAuthClient)
    auth.request_headers.return_value = {
        "Authorization": "Bearer x",
        "User-Agent": "AI-Trading-Bot/0.1",
    }
    adapter = TastytradeSandboxAdapter(_settings(), auth=auth)

    with pytest.raises(SandboxApiError) as exc:
        adapter.submit_equity_order(None, "TNA", "buy", 1)
    assert exc.value.status_code == 422
    assert exc.value.step_diagnostics is not None
    assert exc.value.step_diagnostics.step == "submit_order"


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


def test_execute_order_rejects_sell():
    auth = MagicMock(spec=SandboxOAuthClient)
    adapter = TastytradeSandboxAdapter(_settings(), auth=auth)
    intent = OrderIntent(
        symbol="TNA", side="buy", quantity=1, trading_mode="sandbox", current_price=50.0
    )
    # force sell at submit level
    with pytest.raises(SandboxApiError):
        adapter.submit_equity_order(None, "TNA", "sell", 1)
