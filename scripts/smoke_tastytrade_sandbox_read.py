#!/usr/bin/env python3
"""Read-only Tastytrade sandbox smoke test (no orders)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.adapters.broker.sandbox_auth import SandboxAuthError
from backend.adapters.broker.sandbox_env import (
    format_sandbox_env_report,
    sandbox_env_flags,
    sandbox_env_ready_for_read,
)
from backend.adapters.broker.tastytrade_sandbox import SandboxApiError, TastytradeSandboxAdapter
from backend.config.settings import ConfigurationError, load_settings, reset_settings_cache
from backend.config.tastytrade_urls import SANDBOX_BASE_URL, assert_sandbox_base_url


def _print_env_check(settings) -> bool:
    flags = sandbox_env_flags(settings)
    print("--- sandbox env check ---")
    print(format_sandbox_env_report(flags))
    return sandbox_env_ready_for_read(flags)


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


def _print_auth_error(exc: SandboxAuthError) -> None:
    print("error: authentication failed", file=sys.stderr)
    if exc.diagnostics:
        print(exc.diagnostics.format_safe(), file=sys.stderr)
        from backend.adapters.broker.oauth_diagnostics import oauth_next_step_hint

        print(oauth_next_step_hint(exc.diagnostics), file=sys.stderr)
    else:
        print(str(exc), file=sys.stderr)


def _print_api_error(exc: SandboxApiError) -> None:
    print(f"error: sandbox API failed (status {exc.status_code})", file=sys.stderr)
    print(exc.message, file=sys.stderr)
    if exc.status_code == 422:
        print(
            "Next step: valid symbols may lag in sandbox instrumentation — retry later.",
            file=sys.stderr,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tastytrade sandbox read-only smoke test")
    parser.parse_args(argv)

    try:
        settings = _validate_sandbox_env()
    except ConfigurationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    env_ok = _print_env_check(settings)
    if not env_ok:
        print(
            "error: sandbox env check failed — fix .env before retrying read smoke",
            file=sys.stderr,
        )
        return 2

    adapter = TastytradeSandboxAdapter(settings)

    try:
        adapter._auth.ensure_authenticated()
    except SandboxAuthError as exc:
        _print_auth_error(exc)
        return 1

    try:
        accounts = adapter.get_accounts()
    except SandboxApiError as exc:
        _print_api_error(exc)
        return 1
    except SandboxAuthError as exc:
        _print_auth_error(exc)
        return 1

    account_count = len(accounts)
    if account_count == 0:
        print("error: no sandbox accounts returned", file=sys.stderr)
        return 1

    try:
        balance = adapter.get_balance()
    except (SandboxApiError, SandboxAuthError) as exc:
        if isinstance(exc, SandboxApiError):
            _print_api_error(exc)
        else:
            _print_auth_error(exc)
        return 1

    try:
        positions = adapter.get_positions(balance.get("account_number"))
    except (SandboxApiError, SandboxAuthError) as exc:
        if isinstance(exc, SandboxApiError):
            _print_api_error(exc)
        else:
            _print_auth_error(exc)
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
