"""Tests for sandbox refresh-token persistence and rotation."""

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

from backend.adapters.broker.sandbox_auth import SandboxOAuthClient
from backend.adapters.broker.sandbox_token_store import (
    REFRESH_TOKEN_ENV_KEY,
    persist_sandbox_refresh_token,
)
from backend.config.settings import Settings


def _settings(**overrides) -> Settings:
    base = {
        "live_trading_enabled": False,
        "trading_mode": "sandbox",
        "tastytrade_env": "sandbox",
        "tastytrade_client_id": "cid",
        "tastytrade_client_secret": "secret-value",
        "tastytrade_refresh_token": "old-refresh-token",
        "tastytrade_oauth_scopes": "read trade openid",
    }
    base.update(overrides)
    return Settings(**base)


def test_persist_refresh_token_updates_existing_env_line(tmp_path: Path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "TRADING_MODE=sandbox\nTASTYTRADE_REFRESH_TOKEN=old-token\nTASTYTRADE_ENV=sandbox\n",
        encoding="utf-8",
    )
    monkeypatch.delenv(REFRESH_TOKEN_ENV_KEY, raising=False)

    wrote = persist_sandbox_refresh_token(
        "new-rotated-token",
        env_path=env_file,
        previous_refresh_token="old-token",
    )
    assert wrote is True
    text = env_file.read_text(encoding="utf-8")
    assert "TASTYTRADE_REFRESH_TOKEN=new-rotated-token" in text
    assert "old-token" not in text
    assert "TRADING_MODE=sandbox" in text
    import os

    assert os.environ[REFRESH_TOKEN_ENV_KEY] == "new-rotated-token"


def test_persist_refresh_token_skips_when_unchanged(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text("TASTYTRADE_REFRESH_TOKEN=same-token\n", encoding="utf-8")
    wrote = persist_sandbox_refresh_token(
        "same-token",
        env_path=env_file,
        previous_refresh_token="same-token",
    )
    assert wrote is False


def test_persist_never_logs_token_value(tmp_path: Path, caplog):
    env_file = tmp_path / ".env"
    env_file.write_text("TASTYTRADE_REFRESH_TOKEN=old\n", encoding="utf-8")
    secret = "super-secret-rotated-refresh-token-value"
    with caplog.at_level(logging.INFO):
        persist_sandbox_refresh_token(
            secret,
            env_path=env_file,
            previous_refresh_token="old",
        )
    joined = "\n".join(r.message for r in caplog.records)
    assert secret not in joined
    assert "persisted" in joined.lower()


@patch("backend.adapters.broker.sandbox_auth.persist_sandbox_refresh_token")
@patch("backend.adapters.broker.sandbox_auth.httpx.Client")
def test_oauth_success_persists_rotated_refresh_token(mock_client_cls, mock_persist):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "access_token": "access-abc",
        "refresh_token": "rotated-refresh-xyz",
    }

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.post.return_value = mock_response
    mock_client_cls.return_value = mock_client

    client = SandboxOAuthClient(_settings())
    client.ensure_authenticated()

    mock_persist.assert_called_once()
    args, kwargs = mock_persist.call_args
    assert args[0] == "rotated-refresh-xyz"
    assert kwargs["previous_refresh_token"] == "old-refresh-token"
    assert client._refresh_token == "rotated-refresh-xyz"


@patch("backend.adapters.broker.sandbox_auth.persist_sandbox_refresh_token")
@patch("backend.adapters.broker.sandbox_auth.httpx.Client")
def test_oauth_success_without_new_refresh_does_not_persist(mock_client_cls, mock_persist):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"access_token": "access-abc"}

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.post.return_value = mock_response
    mock_client_cls.return_value = mock_client

    client = SandboxOAuthClient(_settings())
    client.ensure_authenticated()
    mock_persist.assert_not_called()
    assert client._refresh_token == "old-refresh-token"
