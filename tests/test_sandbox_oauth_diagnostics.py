"""Sandbox OAuth diagnostics and request format tests."""

import json
import logging
from unittest.mock import MagicMock, patch

import httpx
import pytest

from backend.adapters.broker.oauth_diagnostics import (
    OAUTH_TOKEN_PATH,
    REFRESH_GRANT_TYPE,
    build_oauth_diagnostics,
    parse_oauth_error_body,
    redact_oauth_body,
)
from backend.adapters.broker.sandbox_auth import SandboxAuthError, SandboxOAuthClient
from backend.config.settings import Settings
from backend.config.tastytrade_urls import PRODUCTION_BASE_URL, SANDBOX_BASE_URL, USER_AGENT


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


@patch("backend.adapters.broker.sandbox_auth.httpx.Client")
def test_oauth_request_uses_sandbox_url(mock_client_cls):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"access_token": "new-access-token"}

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.post.return_value = mock_response
    mock_client_cls.return_value = mock_client

    client = SandboxOAuthClient(_settings())
    client.ensure_authenticated()

    call_kwargs = mock_client.post.call_args
    url = call_kwargs[0][0]
    assert url == f"{SANDBOX_BASE_URL}{OAUTH_TOKEN_PATH}"
    assert PRODUCTION_BASE_URL not in url


@patch("backend.adapters.broker.sandbox_auth.httpx.Client")
def test_oauth_request_includes_user_agent_and_json_body(mock_client_cls):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"access_token": "tok"}

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.post.return_value = mock_response
    mock_client_cls.return_value = mock_client

    client = SandboxOAuthClient(_settings())
    client.ensure_authenticated()

    kwargs = mock_client.post.call_args.kwargs
    headers = kwargs["headers"]
    assert headers["User-Agent"] == USER_AGENT
    assert headers["Content-Type"] == "application/json"
    assert "Authorization" not in headers

    body = kwargs["json"]
    assert body["grant_type"] == REFRESH_GRANT_TYPE
    assert body["client_secret"] == "secret-value"
    assert body["refresh_token"] == "refresh-value"
    assert body["scope"] == "read trade openid"
    assert "redirect_uri" not in body


@patch("backend.adapters.broker.sandbox_auth.httpx.Client")
def test_oauth_failure_is_redacted(mock_client_cls, caplog):
    mock_response = MagicMock()
    mock_response.status_code = 400
    mock_response.text = json.dumps(
        {
            "error": "invalid_grant",
            "error_description": "refresh token expired",
            "refresh_token": "leaked-refresh",
        }
    )

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.post.return_value = mock_response
    mock_client_cls.return_value = mock_client

    client = SandboxOAuthClient(_settings())
    with caplog.at_level(logging.WARNING):
        with pytest.raises(SandboxAuthError) as exc:
            client.ensure_authenticated()

    assert exc.value.step_diagnostics is not None
    assert exc.value.step_diagnostics.step == "oauth_token"
    assert "leaked-refresh" not in str(exc.value.step_diagnostics.format_safe())
    assert "secret-value" not in str(exc.value.step_diagnostics.format_safe())
    assert "refresh-value" not in str(exc.value.step_diagnostics.format_safe())
    assert exc.value.diagnostics is not None
    assert exc.value.diagnostics.error_code == "invalid_grant"


@patch("backend.adapters.broker.sandbox_auth.httpx.Client")
def test_oauth_success_stores_token_without_logging(mock_client_cls, caplog):
    secret_token = "super-secret-access-token-xyz"
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"access_token": secret_token}

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.post.return_value = mock_response
    mock_client_cls.return_value = mock_client

    client = SandboxOAuthClient(_settings())
    with caplog.at_level(logging.DEBUG):
        client.ensure_authenticated()

    assert client.is_authenticated
    assert secret_token not in caplog.text
    safe_headers = client.get_headers()
    assert secret_token not in str(safe_headers)


@patch("backend.adapters.broker.sandbox_auth.httpx.Client")
def test_oauth_does_not_retry_after_failure(mock_client_cls):
    mock_response = MagicMock()
    mock_response.status_code = 400
    mock_response.text = '{"error":"invalid_client"}'

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.post.return_value = mock_response
    mock_client_cls.return_value = mock_client

    client = SandboxOAuthClient(_settings())
    with pytest.raises(SandboxAuthError):
        client.ensure_authenticated()
    with pytest.raises(SandboxAuthError):
        client.ensure_authenticated()
    assert mock_client.post.call_count == 1


def test_parse_oauth_error_redacts_tokens():
    body = '{"error":"invalid_grant","refresh_token":"abc123xyz78901234567890"}'
    code, desc = parse_oauth_error_body(body)
    assert code == "invalid_grant"


def test_redact_oauth_body_dict():
    redacted = redact_oauth_body({"access_token": "secret", "error": "invalid_grant"})
    assert redacted["access_token"] == "[REDACTED]"
    assert redacted["error"] == "invalid_grant"


def test_build_oauth_diagnostics_safe_fields():
    diag = build_oauth_diagnostics(
        status_code=401,
        response_text='{"error":"unauthorized"}',
        grant_type=REFRESH_GRANT_TYPE,
        client_id_configured=True,
        client_secret_configured=True,
        refresh_token_configured=True,
        redirect_uri_configured=True,
    )
    safe = diag.format_safe()
    assert "status_code: 401" in safe
    assert OAUTH_TOKEN_PATH in safe
    assert "secret" not in safe.lower() or "configured" in safe
