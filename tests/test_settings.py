"""Tests for centralized runtime settings (Checkpoint 2.0)."""

from pathlib import Path

import pytest

from backend.config.settings import (
    ALLOWED_TRADING_MODES_PHASE_2,
    ConfigurationError,
    load_settings,
    reset_settings_cache,
)

_NO_ENV = Path("/nonexistent/.env")
_SECRET_SAMPLE = "super-secret-value-xyz-12345"


def _base_env(monkeypatch) -> None:
    monkeypatch.delenv("LIVE_TRADING_ENABLED", raising=False)
    monkeypatch.delenv("TRADING_MODE", raising=False)
    monkeypatch.delenv("TASTYTRADE_ENV", raising=False)
    monkeypatch.delenv("EMERGENCY_HALT", raising=False)
    reset_settings_cache()


def test_default_trading_mode_is_paper(monkeypatch):
    _base_env(monkeypatch)
    settings = load_settings(env_path=_NO_ENV)
    assert settings.trading_mode == "paper"


def test_default_live_trading_enabled_is_false(monkeypatch):
    _base_env(monkeypatch)
    settings = load_settings(env_path=_NO_ENV)
    assert settings.live_trading_enabled is False


def test_default_tastytrade_env_is_sandbox(monkeypatch):
    _base_env(monkeypatch)
    settings = load_settings(env_path=_NO_ENV)
    assert settings.tastytrade_env == "sandbox"


def test_trading_mode_live_rejected(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("TRADING_MODE", "live")
    with pytest.raises(ConfigurationError, match="blocked in Phase 2"):
        load_settings(env_path=_NO_ENV)


def test_live_trading_enabled_true_rejected(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    with pytest.raises(ConfigurationError, match="LIVE_TRADING_ENABLED"):
        load_settings(env_path=_NO_ENV)


def test_allowed_trading_modes_phase2():
    assert ALLOWED_TRADING_MODES_PHASE_2 == frozenset({"paper", "sandbox"})


def test_safe_summary_does_not_expose_secrets(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("ALPHAVANTAGE_API_KEY", _SECRET_SAMPLE)
    monkeypatch.setenv("OPENAI_API_KEY", _SECRET_SAMPLE)
    monkeypatch.setenv("TASTYTRADE_CLIENT_SECRET", _SECRET_SAMPLE)
    monkeypatch.setenv("TASTYTRADE_PASSWORD", _SECRET_SAMPLE)
    monkeypatch.setenv("API_ADMIN_KEY", _SECRET_SAMPLE)
    monkeypatch.setenv("DATABASE_URL", f"postgresql://user:{_SECRET_SAMPLE}@localhost/db")

    settings = load_settings(env_path=_NO_ENV)
    summary = settings.safe_summary()
    serialized = str(summary)

    assert _SECRET_SAMPLE not in serialized
    assert "super-secret" not in serialized
    for key in (
        "alphavantage_api_key",
        "openai_api_key",
        "tastytrade_client_secret",
        "tastytrade_password",
        "api_admin_key",
        "database_url",
    ):
        assert key not in summary

    assert summary["alphavantage_api_key_configured"] is True
    assert summary["openai_api_key_configured"] is True


def test_sandbox_mode_allowed(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("TRADING_MODE", "sandbox")
    settings = load_settings(env_path=_NO_ENV)
    assert settings.trading_mode == "sandbox"
