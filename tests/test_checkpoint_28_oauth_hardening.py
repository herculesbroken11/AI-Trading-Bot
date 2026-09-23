"""Checkpoint 2.8 — OAuth diagnostics, retries, env duplicate detection."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from backend.adapters.broker.env_file_diagnostics import (
    find_duplicate_tastytrade_env_keys,
    safe_fingerprint,
)
from backend.adapters.broker.oauth_diagnostics import (
    build_oauth_diagnostics,
    classify_oauth_failure,
)
from backend.adapters.broker.sandbox_auth import SandboxAuthError, SandboxOAuthClient
from backend.adapters.broker.sandbox_http_retry import should_retry_http_status
from backend.adapters.broker.tastytrade_sandbox import SandboxApiError, TastytradeSandboxAdapter
from backend.config.settings import Settings, reset_settings_cache

REPO_ROOT = Path(__file__).resolve().parents[1]
CANCEL_SCRIPT = REPO_ROOT / "scripts" / "smoke_tastytrade_sandbox_cancel.py"
OAUTH_CHECK_SCRIPT = REPO_ROOT / "scripts" / "check_tastytrade_oauth.py"


def _settings(**overrides) -> Settings:
    base = {
        "live_trading_enabled": False,
        "trading_mode": "sandbox",
        "tastytrade_env": "sandbox",
        "tastytrade_client_id": "cid",
        "tastytrade_client_secret": "secret-value",
        "tastytrade_refresh_token": "refresh-value",
        "tastytrade_oauth_scopes": "read trade openid",
    }
    base.update(overrides)
    return Settings(**base)


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_duplicate_env_key_detection(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "TASTYTRADE_CLIENT_ID=first",
                "TASTYTRADE_REFRESH_TOKEN=aaa",
                "TASTYTRADE_CLIENT_ID=second",
                "TRADING_MODE=sandbox",
            ]
        ),
        encoding="utf-8",
    )
    report = find_duplicate_tastytrade_env_keys(env_file)
    assert report.has_duplicates is True
    assert report.duplicate_keys["TASTYTRADE_CLIENT_ID"] == 2
    safe = report.format_safe()
    assert "TASTYTRADE_CLIENT_ID" in safe
    assert "first" not in safe
    assert "second" not in safe


def test_safe_fingerprint_never_prints_full_secret():
    secret = "super-secret-refresh-token-value-xyz"
    fp = safe_fingerprint(secret, label="TASTYTRADE_REFRESH_TOKEN")
    assert secret not in fp
    assert "sha256_8=" in fp


def test_classify_oauth_wrong_sandbox_customer():
    result = classify_oauth_failure(
        status_code=400,
        error_code="invalid_grant",
        error_description="User is not a TastyTrade customer",
    )
    assert result.reason == "wrong_sandbox_customer"
    assert "sandbox" in result.next_step.lower()


def test_classify_oauth_invalid_client_secret_mismatch():
    result = classify_oauth_failure(
        status_code=400,
        error_code="invalid_client",
        error_description="client authentication failed",
    )
    assert result.reason == "secret_token_mismatch"


def test_classify_oauth_provider_unavailable():
    result = classify_oauth_failure(status_code=503, error_code=None, error_description=None)
    assert result.reason == "provider_unavailable"


@patch("backend.adapters.broker.sandbox_auth.httpx.Client")
def test_oauth_failure_includes_clear_reason(mock_client_cls):
    mock_response = MagicMock()
    mock_response.status_code = 400
    mock_response.text = json.dumps(
        {
            "error": "invalid_grant",
            "error_description": "User is not a TastyTrade customer",
            "refresh_token": "leaked-refresh-token-should-not-appear",
        }
    )
    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.post.return_value = mock_response
    mock_client_cls.return_value = mock_client

    client = SandboxOAuthClient(_settings())
    with pytest.raises(SandboxAuthError) as exc:
        client.ensure_authenticated()

    message = str(exc.value)
    assert "wrong_sandbox_customer" in message
    assert "leaked-refresh-token-should-not-appear" not in message
    assert "secret-value" not in message
    assert exc.value.diagnostics is not None
    assert exc.value.diagnostics.failure_reason == "wrong_sandbox_customer"
    safe = exc.value.diagnostics.format_safe()
    assert "leaked-refresh" not in safe


def test_should_retry_only_transient_statuses():
    assert should_retry_http_status(502) is True
    assert should_retry_http_status(503) is True
    assert should_retry_http_status(504) is True
    assert should_retry_http_status(400) is False
    assert should_retry_http_status(401) is False
    assert should_retry_http_status(403) is False
    assert should_retry_http_status(422) is False
    assert should_retry_http_status(404) is False


@patch("backend.adapters.broker.tastytrade_sandbox.time.sleep", return_value=None)
@patch("backend.adapters.broker.tastytrade_sandbox.httpx.Client")
def test_adapter_retries_transient_502(mock_client_cls, _sleep):
    fail = MagicMock()
    fail.status_code = 502
    fail.text = '{"error":{"message":"bad gateway"}}'
    fail.json.return_value = {"error": {"message": "bad gateway"}}

    ok = MagicMock()
    ok.status_code = 200
    ok.json.return_value = {"data": {"items": [{"account-number": "5WM30541"}]}}

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.request.side_effect = [fail, ok]
    mock_client_cls.return_value = mock_client

    auth = MagicMock()
    auth.request_headers.return_value = {
        "Authorization": "Bearer x",
        "User-Agent": "AI-Trading-Bot/0.1",
    }
    adapter = TastytradeSandboxAdapter(_settings(), auth=auth)
    accounts = adapter.get_accounts()
    assert len(accounts) == 1
    assert mock_client.request.call_count == 2


@patch("backend.adapters.broker.tastytrade_sandbox.time.sleep", return_value=None)
@patch("backend.adapters.broker.tastytrade_sandbox.httpx.Client")
def test_adapter_does_not_retry_400(mock_client_cls, _sleep):
    fail = MagicMock()
    fail.status_code = 400
    fail.text = '{"error":{"code":"invalid_request","message":"bad request"}}'
    fail.json.return_value = {"error": {"code": "invalid_request", "message": "bad request"}}

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.request.return_value = fail
    mock_client_cls.return_value = mock_client

    auth = MagicMock()
    auth.request_headers.return_value = {
        "Authorization": "Bearer x",
        "User-Agent": "AI-Trading-Bot/0.1",
    }
    adapter = TastytradeSandboxAdapter(_settings(), auth=auth)
    with pytest.raises(SandboxApiError) as exc:
        adapter.get_accounts()
    assert mock_client.request.call_count == 1
    assert exc.value.status_code == 400


@pytest.mark.parametrize("status", [401, 403, 422])
@patch("backend.adapters.broker.tastytrade_sandbox.time.sleep", return_value=None)
@patch("backend.adapters.broker.tastytrade_sandbox.httpx.Client")
def test_adapter_does_not_retry_client_errors(mock_client_cls, _sleep, status):
    fail = MagicMock()
    fail.status_code = status
    fail.text = f'{{"error":{{"message":"status {status}"}}}}'
    fail.json.return_value = {"error": {"message": f"status {status}"}}

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.request.return_value = fail
    mock_client_cls.return_value = mock_client

    auth = MagicMock()
    auth.request_headers.return_value = {
        "Authorization": "Bearer x",
        "User-Agent": "AI-Trading-Bot/0.1",
    }
    auth.refresh_access_token = MagicMock()
    adapter = TastytradeSandboxAdapter(_settings(), auth=auth)
    with pytest.raises(SandboxApiError):
        adapter.get_accounts()
    if status == 401:
        assert mock_client.request.call_count == 2
    else:
        assert mock_client.request.call_count == 1


def test_cancel_script_stops_when_oauth_fails(monkeypatch, capsys):
    monkeypatch.setenv("TRADING_MODE", "sandbox")
    monkeypatch.setenv("TASTYTRADE_ENV", "sandbox")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    monkeypatch.setenv("TASTYTRADE_CLIENT_SECRET", "configured")
    monkeypatch.setenv("TASTYTRADE_REFRESH_TOKEN", "configured")
    monkeypatch.setenv("TASTYTRADE_OAUTH_SCOPES", "read trade")
    reset_settings_cache()

    mod = _load_module(CANCEL_SCRIPT, "smoke_cancel_oauth_fail")
    cancel_called = {"value": False}

    class FakeAuth:
        is_authenticated = False

        def ensure_authenticated(self):
            raise SandboxAuthError(
                "Sandbox OAuth failed (400): wrong_sandbox_customer — bad grant",
                diagnostics=build_oauth_diagnostics(
                    status_code=400,
                    response_text=json.dumps(
                        {
                            "error": "invalid_grant",
                            "error_description": "User is not a TastyTrade customer",
                        }
                    ),
                    grant_type="refresh_token",
                    client_id_configured=True,
                    client_secret_configured=True,
                    refresh_token_configured=True,
                    redirect_uri_configured=True,
                ),
            )

    class FakeAdapter:
        _auth = FakeAuth()

        def cancel_order(self, account_number, order_id):
            cancel_called["value"] = True
            return {}

        def list_live_orders(self, account_number):
            cancel_called["value"] = True
            return []

    monkeypatch.setattr(mod, "TastytradeSandboxAdapter", lambda settings: FakeAdapter())
    assert mod.main(["--order-id", "1225452", "--confirm-sandbox-cancel"]) == 1
    assert cancel_called["value"] is False
    err = capsys.readouterr().err
    assert "cancel aborted" in err.lower() or "oauth" in err.lower()


def test_cancel_script_requires_confirm_flag(monkeypatch, capsys):
    monkeypatch.setenv("TRADING_MODE", "sandbox")
    monkeypatch.setenv("TASTYTRADE_ENV", "sandbox")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    monkeypatch.setenv("TASTYTRADE_CLIENT_SECRET", "configured")
    monkeypatch.setenv("TASTYTRADE_REFRESH_TOKEN", "configured")
    monkeypatch.setenv("TASTYTRADE_OAUTH_SCOPES", "read trade")
    reset_settings_cache()

    mod = _load_module(CANCEL_SCRIPT, "smoke_cancel_needs_confirm_28")
    cancel_called = {"value": False}

    class FakeAuth:
        is_authenticated = True

        def ensure_authenticated(self):
            return None

    class FakeAdapter:
        _auth = FakeAuth()

        def get_customers_me(self):
            return {}

        def get_accounts(self):
            return [{"account-number": "5WM30541"}]

        def get_balance(self):
            return {"account_number": "5WM30541"}

        def list_live_orders(self, account_number):
            return [
                {
                    "id": 1225452,
                    "status": "Live",
                    "order-type": "Limit",
                    "price": "2.00",
                    "legs": [{"symbol": "TNA", "quantity": 1, "action": "Buy to Open"}],
                }
            ]

        def get_order(self, account_number, order_id):
            return {
                "data": {
                    "id": int(order_id),
                    "status": "Live",
                    "order-type": "Limit",
                    "price": "2.00",
                    "legs": [{"symbol": "TNA", "quantity": 1, "action": "Buy to Open"}],
                }
            }

        def cancel_order(self, account_number, order_id):
            cancel_called["value"] = True
            return {"data": {"cancelled": True}}

    monkeypatch.setattr(mod, "TastytradeSandboxAdapter", lambda settings: FakeAdapter())
    assert mod.main(["--order-id", "1225452"]) == 0
    assert cancel_called["value"] is False
    err = capsys.readouterr().err
    assert "confirm-sandbox-cancel" in err


def test_oauth_check_script_never_prints_secrets():
    text = OAUTH_CHECK_SCRIPT.read_text(encoding="utf-8")
    assert "safe_fingerprint" in text
    assert 'print(settings.tastytrade_client_secret)' not in text
    assert 'print(settings.tastytrade_refresh_token)' not in text


def test_cancel_script_never_submits_orders():
    text = CANCEL_SCRIPT.read_text(encoding="utf-8")
    assert "submit_equity_order" not in text
    assert "OrderExecutor" not in text
