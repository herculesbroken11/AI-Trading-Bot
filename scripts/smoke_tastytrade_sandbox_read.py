#!/usr/bin/env python3
"""Read-only Tastytrade sandbox smoke test (no orders)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.adapters.broker.tastytrade_sandbox import TastytradeSandboxAdapter
from backend.config.settings import ConfigurationError, load_settings, reset_settings_cache
from backend.config.tastytrade_urls import SANDBOX_BASE_URL, assert_sandbox_base_url


def _validate_sandbox_env():
    reset_settings_cache()
    settings = load_settings()
    if settings.trading_mode.strip().lower() != "sandbox":
        raise ConfigurationError("smoke_tastytrade_sandbox_read requires TRADING_MODE=sandbox")
    if settings.tastytrade_env.strip().lower() != "sandbox":
        raise ConfigurationError("smoke_tastytrade_sandbox_read requires TASTYTRADE_ENV=sandbox")
    if settings.live_trading_enabled:
        raise ConfigurationError("LIVE_TRADING_ENABLED must be false")
    assert_sandbox_base_url(SANDBOX_BASE_URL)
    return settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tastytrade sandbox read-only smoke test")
    parser.parse_args(argv)

    try:
        settings = _validate_sandbox_env()
    except ConfigurationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    adapter = TastytradeSandboxAdapter(settings)
    try:
        adapter._auth.ensure_authenticated()
        accounts = adapter.get_accounts()
        account_count = len(accounts)
        balance = adapter.get_balance()
        positions = adapter.get_positions(balance.get("account_number"))
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(f"authenticated: {adapter._auth.is_authenticated}")
    print(f"sandbox_base_url: {SANDBOX_BASE_URL}")
    print(f"account_count: {account_count}")
    print(f"selected_account: {balance.get('account_number')}")
    print(f"buying_power: {balance.get('buying_power')}")
    print(f"cash_balance: {balance.get('cash_balance')}")
    print(f"positions_count: {len(positions)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
