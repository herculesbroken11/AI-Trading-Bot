"""Auth endpoint must not expose OAuth tokens."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from backend.config.settings import reset_settings_cache


@pytest.fixture
def auth_app(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setenv("TASTYTRADE_ENV", "sandbox")
    reset_settings_cache()
    from backend.app_factory import create_app

    app = create_app(skip_db_init=True, defer_heavy_services=True)
    mock_auth = MagicMock()
    mock_auth.exchange_code_for_token.return_value = {"access_token": "super-secret-token"}
    app.config["AUTH"] = mock_auth
    return app


def test_auth_does_not_return_access_token(auth_app):
    client = auth_app.test_client()
    response = client.post("/auth/tastytrade", json={"code": "test-code"})
    assert response.status_code == 200
    body = response.get_json()
    assert body["success"] is True
    assert body["authenticated"] is True
    assert "access_token" not in body
    assert body.get("access_token_present") is True
    serialized = str(body)
    assert "super-secret-token" not in serialized
