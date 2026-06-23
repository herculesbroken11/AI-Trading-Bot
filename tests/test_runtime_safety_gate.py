"""Tests for Phase 2 runtime safety gates on legacy endpoints."""

from pathlib import Path

import pytest

from backend.config.settings import ConfigurationError, load_settings, reset_settings_cache


@pytest.fixture
def test_app(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setenv("TASTYTRADE_ENV", "sandbox")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    monkeypatch.delenv("EMERGENCY_HALT", raising=False)
    reset_settings_cache()
    from backend.app_factory import create_app

    return create_app(skip_db_init=True, defer_heavy_services=True)


def test_startup_rejects_live_trading_mode(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "live")
    reset_settings_cache()
    with pytest.raises(ConfigurationError, match="blocked in Phase 2"):
        load_settings(env_path=Path("/nonexistent/.env"))


def test_startup_rejects_production_tastytrade_env(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setenv("TASTYTRADE_ENV", "production")
    reset_settings_cache()
    settings = load_settings(env_path=Path("/nonexistent/.env"))
    with pytest.raises(ConfigurationError, match="sandbox"):
        settings.validate_startup()


def test_startup_rejects_live_trading_enabled(monkeypatch):
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    reset_settings_cache()
    with pytest.raises(ConfigurationError, match="LIVE_TRADING_ENABLED"):
        load_settings(env_path=Path("/nonexistent/.env"))


def test_trade_execute_blocked(test_app):
    client = test_app.test_client()
    response = client.post("/trade/execute", json={"symbol": "TNA"})
    assert response.status_code == 423
    body = response.get_json()
    assert body["success"] is False
    assert body["status"] == "blocked"


def test_bot_start_blocked(test_app):
    client = test_app.test_client()
    response = client.post("/bot/start")
    assert response.status_code == 423
    body = response.get_json()
    assert body["status"] == "blocked"


def test_trade_close_blocked(test_app):
    client = test_app.test_client()
    response = client.post("/trade/close/1", json={"reason": "test"})
    assert response.status_code == 423
    body = response.get_json()
    assert body["status"] == "blocked"


def test_read_only_root_works(test_app):
    client = test_app.test_client()
    response = client.get("/")
    assert response.status_code == 200
    body = response.get_json()
    assert "settings" in body
