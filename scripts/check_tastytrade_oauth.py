#!/usr/bin/env python3
"""Standalone Tastytrade sandbox OAuth diagnostic (never prints secrets)."""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.adapters.broker.env_file_diagnostics import (
    find_duplicate_tastytrade_env_keys,
    safe_fingerprint,
)
from backend.adapters.broker.oauth_diagnostics import oauth_next_step_hint
from backend.adapters.broker.sandbox_auth import SandboxAuthError, SandboxOAuthClient
from backend.adapters.broker.sandbox_cooldown import RATE_LIMITED_EXIT_CODE, print_cooldown_advice
from backend.adapters.broker.sandbox_env import (
    format_sandbox_env_report,
    sandbox_env_flags,
)
from backend.adapters.broker.sandbox_rate_limiter import rate_limit_info_from
from backend.adapters.broker.tastytrade_sandbox import SandboxApiError, TastytradeSandboxAdapter
from backend.config.settings import ConfigurationError, load_settings, reset_settings_cache
from backend.config.tastytrade_urls import SANDBOX_BASE_URL, assert_sandbox_base_url

CHECK_COMMAND = "py -3.11 scripts/check_tastytrade_oauth.py"

REQUIRED_KEYS = (
    "TASTYTRADE_CLIENT_ID",
    "TASTYTRADE_CLIENT_SECRET",
    "TASTYTRADE_REFRESH_TOKEN",
    "TASTYTRADE_ENV",
)


def _print_failure(
    *,
    step: str,
    authenticated: bool = False,
    account_count: int = 0,
    selected_account: str | None = None,
    status_code: int | None = None,
    provider_message: str | None = None,
    failure_reason: str | None = None,
    next_step: str | None = None,
) -> int:
    print("authenticated: false" if not authenticated else "authenticated: true")
    print(f"account_count: {account_count}")
    print(f"selected_account: {selected_account or 'none'}")
    print(f"error_step: {step}")
    if failure_reason:
        print(f"failure_reason: {failure_reason}")
    if status_code is not None:
        print(f"provider_status_code: {status_code}")
    if provider_message:
        print(f"provider_message: {provider_message}")
    if next_step:
        print(next_step)
    return 1


def main() -> int:
    env_path = _REPO_ROOT / ".env"
    print("--- tastytrade sandbox oauth check ---")
    print(f"env_path: {env_path}")

    if not env_path.is_file():
        return _print_failure(
            step="load_env",
            provider_message=".env file not found",
            next_step="Next step: create .env with sandbox OAuth credentials.",
        )

    duplicate_report = find_duplicate_tastytrade_env_keys(env_path)
    print(duplicate_report.format_safe())
    if duplicate_report.has_duplicates:
        print(
            "warning: duplicate TASTYTRADE_ keys detected; "
            "last definition wins and can cause secret/token mismatch."
        )

    reset_settings_cache()
    try:
        settings = load_settings(env_path=env_path, override=True)
    except ConfigurationError as exc:
        return _print_failure(
            step="load_settings",
            provider_message=str(exc),
            next_step="Next step: fix ConfigurationError values in .env.",
        )

    # Hard safety gates
    if settings.tastytrade_env.strip().lower() != "sandbox":
        return _print_failure(
            step="env_gate",
            failure_reason="production_sandbox_mismatch",
            provider_message=f"TASTYTRADE_ENV={settings.tastytrade_env!r} (must be sandbox)",
            next_step="Next step: set TASTYTRADE_ENV=sandbox.",
        )
    if settings.trading_mode.strip().lower() != "sandbox":
        return _print_failure(
            step="env_gate",
            failure_reason="production_sandbox_mismatch",
            provider_message=f"TRADING_MODE={settings.trading_mode!r} (must be sandbox)",
            next_step="Next step: set TRADING_MODE=sandbox.",
        )
    if settings.live_trading_enabled:
        return _print_failure(
            step="env_gate",
            failure_reason="live_trading_blocked",
            provider_message="LIVE_TRADING_ENABLED must be false",
            next_step="Next step: set LIVE_TRADING_ENABLED=false.",
        )

    try:
        assert_sandbox_base_url(SANDBOX_BASE_URL)
    except Exception as exc:
        return _print_failure(
            step="env_gate",
            failure_reason="production_sandbox_mismatch",
            provider_message=str(exc),
        )

    flags = sandbox_env_flags(settings)
    print(format_sandbox_env_report(flags))

    missing = []
    if not settings.tastytrade_client_id.strip():
        missing.append("TASTYTRADE_CLIENT_ID")
    if not settings.tastytrade_client_secret.strip():
        missing.append("TASTYTRADE_CLIENT_SECRET")
    if not settings.tastytrade_refresh_token.strip():
        missing.append("TASTYTRADE_REFRESH_TOKEN")
    if not settings.tastytrade_env.strip():
        missing.append("TASTYTRADE_ENV")
    if missing:
        return _print_failure(
            step="env_required_keys",
            provider_message=f"missing: {', '.join(missing)}",
            next_step="Next step: set required TASTYTRADE_* values in .env.",
        )

    # Fingerprints only — never full secrets
    print(safe_fingerprint(settings.tastytrade_client_id, label="TASTYTRADE_CLIENT_ID"))
    print(safe_fingerprint(settings.tastytrade_client_secret, label="TASTYTRADE_CLIENT_SECRET"))
    print(safe_fingerprint(settings.tastytrade_refresh_token, label="TASTYTRADE_REFRESH_TOKEN"))
    print(f"TASTYTRADE_ENV: {settings.tastytrade_env}")
    print(f"TRADING_MODE: {settings.trading_mode}")
    print(f"LIVE_TRADING_ENABLED: {str(settings.live_trading_enabled).lower()}")
    print(f"sandbox_base_url: {SANDBOX_BASE_URL}")

    auth = SandboxOAuthClient(settings)
    try:
        auth.ensure_authenticated()
    except SandboxAuthError as exc:
        info = rate_limit_info_from(exc)
        if info:
            _print_failure(
                step="oauth_token",
                status_code=429,
                provider_message="rate limited",
                failure_reason=info.failure_reason,
            )
            print_cooldown_advice(info, next_command=CHECK_COMMAND, stream=sys.stdout)
            return RATE_LIMITED_EXIT_CODE
        status = exc.diagnostics.status_code if exc.diagnostics else None
        reason = exc.diagnostics.failure_reason if exc.diagnostics else None
        provider_message = None
        if exc.diagnostics and exc.diagnostics.error_description:
            provider_message = exc.diagnostics.error_description
        elif exc.diagnostics and exc.diagnostics.error_code:
            provider_message = exc.diagnostics.error_code
        else:
            provider_message = str(exc)
        next_step = oauth_next_step_hint(exc.diagnostics) if exc.diagnostics else None
        if exc.diagnostics:
            print(exc.diagnostics.format_safe())
        return _print_failure(
            step="oauth_token",
            status_code=status,
            provider_message=provider_message,
            failure_reason=reason,
            next_step=next_step,
        )

    print("authenticated: true")
    # Share the authenticated client so the adapter does not request a second token.
    adapter = TastytradeSandboxAdapter(settings, auth=auth)
    try:
        accounts = adapter.get_accounts()
        account_count = len(accounts)
        selected = adapter.get_selected_account() if accounts else None
    except (SandboxAuthError, SandboxApiError) as exc:
        return _account_read_failure(exc)

    print(f"account_count: {account_count}")
    print(f"selected_account: {selected or 'none'}")
    print("error_step: none")
    print("oauth_check: passed")
    return 0


def _account_read_failure(exc: BaseException) -> int:
    info = rate_limit_info_from(exc)
    if info:
        _print_failure(
            step="get_accounts",
            authenticated=True,
            status_code=429,
            provider_message="rate limited",
            failure_reason=info.failure_reason,
        )
        print_cooldown_advice(info, next_command=CHECK_COMMAND, stream=sys.stdout)
        return RATE_LIMITED_EXIT_CODE
    if isinstance(exc, SandboxAuthError):
        status = exc.diagnostics.status_code if exc.diagnostics else None
        reason = exc.diagnostics.failure_reason if exc.diagnostics else "oauth_unhealthy"
        next_step = oauth_next_step_hint(exc.diagnostics) if exc.diagnostics else None
        return _print_failure(
            step="oauth_token",
            authenticated=True,
            status_code=status,
            provider_message=str(exc),
            failure_reason=reason,
            next_step=next_step,
        )
    if isinstance(exc, SandboxApiError):
        provider_message = (
            exc.step_diagnostics.provider_message
            if exc.step_diagnostics and exc.step_diagnostics.provider_message
            else exc.message
        )
        return _print_failure(
            step=exc.step_diagnostics.step if exc.step_diagnostics else "account_read",
            authenticated=True,
            status_code=exc.status_code,
            provider_message=provider_message,
            failure_reason="account_unavailable",
            next_step="Next step: confirm sandbox account exists for this grant.",
        )
    return _print_failure(step="account_read", authenticated=True, provider_message=type(exc).__name__)


if __name__ == "__main__":
    raise SystemExit(main())
