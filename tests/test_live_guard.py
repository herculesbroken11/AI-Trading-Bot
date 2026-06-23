"""Tests for Phase 2 live-trading guard (Checkpoint 2.0)."""

from pathlib import Path

import pytest

from backend.config.settings import Settings, load_settings, reset_settings_cache
from backend.risk.live_guard import (
    BrokerEnvironmentBlockedError,
    LiveTradingBlockedError,
    assert_order_execution_allowed,
    is_paper_mode,
    is_sandbox_broker_mode,
)

_NO_ENV = Path("/nonexistent/.env")


def _paper_settings(**overrides) -> Settings:
    base = {
        "live_trading_enabled": False,
        "trading_mode": "paper",
        "tastytrade_env": "sandbox",
        "emergency_halt": False,
    }
    base.update(overrides)
    return Settings(**base)


def test_paper_mode_allowed_for_orders(monkeypatch):
    monkeypatch.delenv("TRADING_MODE", raising=False)
    reset_settings_cache()
    settings = _paper_settings()
    assert_order_execution_allowed(settings)
    assert is_paper_mode(settings) is True
    assert is_sandbox_broker_mode(settings) is False


def test_sandbox_mode_allowed_when_env_is_sandbox():
    settings = _paper_settings(trading_mode="sandbox")
    assert_order_execution_allowed(settings)
    assert is_sandbox_broker_mode(settings) is True


def test_production_tastytrade_env_rejected_for_orders():
    settings = _paper_settings(tastytrade_env="production")
    with pytest.raises(BrokerEnvironmentBlockedError, match="sandbox"):
        assert_order_execution_allowed(settings)


def test_live_trading_enabled_true_rejected_for_orders_even_if_mode_paper():
    settings = _paper_settings(live_trading_enabled=True)
    with pytest.raises(LiveTradingBlockedError, match="LIVE_TRADING_ENABLED"):
        assert_order_execution_allowed(settings)


def test_trading_mode_live_rejected_for_orders():
    settings = _paper_settings(trading_mode="live")
    with pytest.raises(LiveTradingBlockedError, match="TRADING_MODE=live"):
        assert_order_execution_allowed(settings)


def test_load_settings_production_env_still_loads_but_guard_blocks_orders(monkeypatch):
    monkeypatch.delenv("LIVE_TRADING_ENABLED", raising=False)
    monkeypatch.delenv("TRADING_MODE", raising=False)
    monkeypatch.setenv("TASTYTRADE_ENV", "production")
    reset_settings_cache()
    settings = load_settings(env_path=_NO_ENV)
    assert settings.tastytrade_env == "production"
    with pytest.raises(BrokerEnvironmentBlockedError):
        assert_order_execution_allowed(settings)
