"""Local sandbox .env shape checks (no Tastytrade HTTP, no secret values)."""

from __future__ import annotations

from typing import Dict

from backend.config.settings import Settings


def sandbox_env_flags(settings: Settings) -> Dict[str, bool]:
    """Return boolean flags for required sandbox OAuth environment shape."""
    return {
        "TRADING_MODE is sandbox": settings.trading_mode.strip().lower() == "sandbox",
        "TASTYTRADE_ENV is sandbox": settings.tastytrade_env.strip().lower() == "sandbox",
        "LIVE_TRADING_ENABLED is false": not settings.live_trading_enabled,
        "client_id configured": bool(settings.tastytrade_client_id.strip()),
        "client_secret configured": bool(settings.tastytrade_client_secret.strip()),
        "refresh_token configured": bool(settings.tastytrade_refresh_token.strip()),
        "redirect_uri configured": bool(settings.tastytrade_redirect_uri.strip()),
        "scopes configured": bool(settings.tastytrade_oauth_scopes.strip()),
    }


def format_sandbox_env_report(flags: Dict[str, bool]) -> str:
    lines = [f"{key}: {str(value).lower()}" for key, value in flags.items()]
    return "\n".join(lines)


def sandbox_env_ready_for_read(flags: Dict[str, bool]) -> bool:
    """True when env shape is sufficient to attempt read-only sandbox smoke."""
    required = [
        "TRADING_MODE is sandbox",
        "TASTYTRADE_ENV is sandbox",
        "LIVE_TRADING_ENABLED is false",
        "client_secret configured",
        "refresh_token configured",
        "scopes configured",
    ]
    return all(flags.get(key) for key in required)
