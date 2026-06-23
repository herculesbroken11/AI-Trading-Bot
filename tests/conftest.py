"""Pytest configuration and shared fixtures."""

import os

import pytest

from backend.config.settings import reset_settings_cache


@pytest.fixture(autouse=True)
def _isolate_settings_env(monkeypatch):
    """Prevent local .env from affecting unit tests."""
    monkeypatch.delenv("LIVE_TRADING_ENABLED", raising=False)
    monkeypatch.delenv("TRADING_MODE", raising=False)
    monkeypatch.delenv("TASTYTRADE_ENV", raising=False)
    monkeypatch.delenv("EMERGENCY_HALT", raising=False)
    reset_settings_cache()
