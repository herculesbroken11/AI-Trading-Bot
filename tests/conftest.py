"""Pytest configuration and shared fixtures."""

import os

import pytest

from backend.adapters.broker.sandbox_rate_limiter import (
    ENDPOINT_GROUPS,
    SandboxRateLimiter,
    set_sandbox_rate_limiter,
)
from backend.config.settings import reset_settings_cache
from backend.market_data.tastytrade_market_data import (
    MARKET_DATA_GROUPS,
    set_market_data_rate_limiter,
)


@pytest.fixture(autouse=True)
def _isolate_settings_env(monkeypatch):
    """Prevent local .env from affecting unit tests."""
    monkeypatch.delenv("LIVE_TRADING_ENABLED", raising=False)
    monkeypatch.delenv("TRADING_MODE", raising=False)
    monkeypatch.delenv("TASTYTRADE_ENV", raising=False)
    monkeypatch.delenv("EMERGENCY_HALT", raising=False)
    reset_settings_cache()


@pytest.fixture(autouse=True)
def _isolate_sandbox_rate_limiter(monkeypatch):
    """Zero-delay, in-memory limiter so unit tests never sleep or write the state file."""
    monkeypatch.setenv("TASTYTRADE_SANDBOX_RATE_STATE_PATH", "none")
    limiter = SandboxRateLimiter(
        min_intervals={group: 0.0 for group in ENDPOINT_GROUPS},
        state_path=None,
        sleep=lambda _seconds: None,
    )
    set_sandbox_rate_limiter(limiter)
    yield limiter
    set_sandbox_rate_limiter(None)


@pytest.fixture(autouse=True)
def _isolate_market_data_rate_limiter(monkeypatch):
    """Same isolation for the separate production market-data limiter."""
    monkeypatch.setenv("MARKET_DATA_RATE_STATE_PATH", "none")
    limiter = SandboxRateLimiter(
        min_intervals={group: 0.0 for group in MARKET_DATA_GROUPS},
        state_path=None,
        sleep=lambda _seconds: None,
    )
    set_market_data_rate_limiter(limiter)
    yield limiter
    set_market_data_rate_limiter(None)
