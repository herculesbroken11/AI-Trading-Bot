"""Shared helpers for Tastytrade sandbox smoke scripts."""

from __future__ import annotations

import sys
from typing import Optional

from backend.adapters.broker.sandbox_auth import SandboxAuthError
from backend.adapters.broker.sandbox_cooldown import (
    RATE_LIMITED_EXIT_CODE,
    print_cooldown_advice,
)
from backend.adapters.broker.sandbox_rate_limiter import rate_limit_info_from
from backend.adapters.broker.sandbox_env import (
    format_sandbox_env_report,
    sandbox_env_flags,
    sandbox_env_ready_for_read,
)
from backend.adapters.broker.sandbox_order_verification import (
    broker_order_id_from_execution,
    format_order_status_summary,
    verify_buy_position_present,
    verify_position_closed,
)
from backend.adapters.broker.tastytrade_sandbox import SandboxApiError, TastytradeSandboxAdapter
from backend.config.settings import ConfigurationError, load_settings, reset_settings_cache
from backend.config.tastytrade_urls import SANDBOX_BASE_URL, assert_sandbox_base_url
from backend.repositories.order_repository import OrderRepository


def validate_sandbox_env(*, script_name: str):
    reset_settings_cache()
    settings = load_settings()
    if settings.trading_mode.strip().lower() != "sandbox":
        raise ConfigurationError(f"{script_name} requires TRADING_MODE=sandbox")
    if settings.tastytrade_env.strip().lower() != "sandbox":
        raise ConfigurationError(f"{script_name} requires TASTYTRADE_ENV=sandbox")
    if settings.live_trading_enabled:
        raise ConfigurationError("LIVE_TRADING_ENABLED must be false")
    if settings.emergency_halt:
        raise ConfigurationError("EMERGENCY_HALT must be false")
    assert_sandbox_base_url(SANDBOX_BASE_URL)
    return settings


def print_env_check(settings) -> bool:
    flags = sandbox_env_flags(settings)
    print("--- sandbox env check ---")
    print(format_sandbox_env_report(flags))
    return sandbox_env_ready_for_read(flags)


def print_api_error(exc: SandboxApiError, *, next_command: Optional[str] = None) -> None:
    print("error: sandbox step failed", file=sys.stderr)
    if exc.step_diagnostics:
        print(exc.step_diagnostics.format_safe(), file=sys.stderr)
    else:
        print(exc.message, file=sys.stderr)
    info = rate_limit_info_from(exc)
    if info:
        print_cooldown_advice(info, next_command=next_command)


def print_auth_error(exc: SandboxAuthError, *, next_command: Optional[str] = None) -> None:
    from backend.adapters.broker.oauth_diagnostics import oauth_next_step_hint

    print("error: authentication failed", file=sys.stderr)
    info = rate_limit_info_from(exc)
    if info:
        print(str(exc), file=sys.stderr)
        print_cooldown_advice(info, next_command=next_command)
        return
    if exc.diagnostics:
        print(exc.diagnostics.format_safe(), file=sys.stderr)
        print(oauth_next_step_hint(exc.diagnostics), file=sys.stderr)
    elif exc.step_diagnostics:
        print(exc.step_diagnostics.format_safe(), file=sys.stderr)
    else:
        print(str(exc), file=sys.stderr)


def print_sandbox_error(exc: BaseException, *, next_command: Optional[str] = None) -> int:
    """Print a sandbox API/auth error; return RATE_LIMITED_EXIT_CODE on 429, else 1."""
    if isinstance(exc, SandboxApiError):
        print_api_error(exc, next_command=next_command)
    elif isinstance(exc, SandboxAuthError):
        print_auth_error(exc, next_command=next_command)
    else:
        print(f"error: {type(exc).__name__}", file=sys.stderr)
    return RATE_LIMITED_EXIT_CODE if rate_limit_info_from(exc) else 1


def fetch_account_state(adapter: TastytradeSandboxAdapter) -> tuple[dict, list]:
    adapter._auth.ensure_authenticated()
    accounts = adapter.get_accounts()
    if not accounts:
        raise SandboxApiError(404, "No sandbox accounts returned")
    balance = adapter.get_balance()
    positions = adapter.get_positions(balance.get("account_number"))
    return balance, positions


def verify_order_after_submit(
    adapter: TastytradeSandboxAdapter,
    account_number: Optional[str],
    *,
    order_id: Optional[str],
    raw: Optional[dict],
) -> int:
    broker_order_id = broker_order_id_from_execution(order_id, raw)
    if not broker_order_id:
        print("warning: broker order id unavailable for get_order verification", file=sys.stderr)
        return 0
    try:
        summary = adapter.fetch_order_status_summary(account_number, broker_order_id)
        print(format_order_status_summary(summary))
        return 0
    except (SandboxApiError, SandboxAuthError) as exc:
        if isinstance(exc, SandboxApiError):
            print_api_error(exc)
        else:
            print_auth_error(exc)
        return 1


def verify_position_after_buy(
    adapter: TastytradeSandboxAdapter,
    account_number: Optional[str],
    symbol: str,
) -> None:
    try:
        positions = adapter.get_positions(account_number)
    except (SandboxApiError, SandboxAuthError) as exc:
        if isinstance(exc, SandboxApiError):
            print_api_error(exc)
        else:
            print_auth_error(exc)
        return
    ok, message = verify_buy_position_present(positions, symbol)
    stream = sys.stdout if ok else sys.stderr
    print(message, file=stream)


def verify_position_after_close(
    adapter: TastytradeSandboxAdapter,
    account_number: Optional[str],
    symbol: str,
) -> None:
    try:
        positions = adapter.get_positions(account_number)
    except (SandboxApiError, SandboxAuthError) as exc:
        if isinstance(exc, SandboxApiError):
            print_api_error(exc)
        else:
            print_auth_error(exc)
        return
    ok, message = verify_position_closed(positions, symbol)
    stream = sys.stdout if ok else sys.stderr
    print(message, file=stream)


def print_execution_summary(
    result,
    *,
    limit_price: Optional[float] = None,
    order_type: Optional[str] = None,
) -> None:
    raw = result.raw or {}
    broker_status = raw.get("broker_status")
    resolved_limit = raw.get("limit_price", limit_price)
    print(f"status: {result.status}")
    print(f"success: {result.success}")
    print(f"order_id: {result.order_id}")
    print(f"broker_status: {broker_status}")
    print(f"symbol: {result.symbol}")
    print(f"side: {result.side}")
    print(f"quantity: {result.quantity}")
    print(f"order_type: {order_type or raw.get('order_type')}")
    print(f"limit_price: {resolved_limit}")
    if broker_status and str(broker_status).lower() == "live":
        print("fill_price: None")
    else:
        print(f"fill_price: {result.fill_price}")
    print(f"trading_mode: {result.trading_mode}")
    print(f"message: {result.message}")


def print_db_order_summary(repo: OrderRepository, order_id: str) -> None:
    record = repo.get_by_order_id(order_id)
    if not record:
        recent = repo.get_recent_orders(limit=1)
        record = recent[0] if recent else None
    if not record:
        print("warning: no DB order record found for summary", file=sys.stderr)
        return
    print("--- db order record ---")
    print(f"order_id: {record.order_id}")
    print(f"broker_order_id: {record.broker_order_id}")
    print(f"status: {record.status}")
    print(f"symbol: {record.symbol}")
    print(f"side: {record.side}")
    print(f"quantity: {record.quantity}")
    print(f"order_type: {record.order_type}")
    print(f"limit_price: {record.limit_price}")
    print(f"fill_price: {record.fill_price}")
    print(f"mode: {record.mode}")
