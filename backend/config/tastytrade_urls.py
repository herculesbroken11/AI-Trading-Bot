"""Tastytrade API URL constants (Phase 2 — sandbox only for order paths)."""

from __future__ import annotations

SANDBOX_BASE_URL = "https://api.cert.tastyworks.com"
PRODUCTION_BASE_URL = "https://api.tastyworks.com"
SANDBOX_STREAMER_HOST = "streamer.cert.tastyworks.com"

# Hostnames that must never be used for Phase 2 broker HTTP.
BLOCKED_PRODUCTION_HOSTS = frozenset(
    {
        "api.tastytrade.com",
        "api.tastyworks.com",
        "api.cert.tastytrade.com",  # deprecated typo — use cert.tastyworks.com
    }
)

USER_AGENT = "AI-Trading-Bot/0.1"
SANDBOX_MAX_ORDER_QUANTITY = 1
ALLOWED_SANDBOX_SYMBOLS = frozenset({"TNA", "TZA"})


class BrokerUrlBlockedError(ValueError):
    """Raised when a non-sandbox broker URL or environment is requested."""


def resolve_broker_base_url(tastytrade_env: str) -> str:
    """Return sandbox base URL only. Production is blocked in Phase 2."""
    env = (tastytrade_env or "").strip().lower()
    if env == "sandbox":
        return SANDBOX_BASE_URL
    raise BrokerUrlBlockedError(
        f"TASTYTRADE_ENV={tastytrade_env!r} is blocked in Phase 2. "
        f"Only sandbox ({SANDBOX_BASE_URL}) is permitted."
    )


def assert_sandbox_base_url(url: str) -> None:
    """Ensure URL points at cert sandbox, not production or deprecated hosts."""
    normalized = (url or "").strip().rstrip("/").lower()
    if normalized != SANDBOX_BASE_URL.lower():
        raise BrokerUrlBlockedError(
            f"Broker URL {url!r} is not permitted. Phase 2 requires {SANDBOX_BASE_URL}."
        )
    for host in BLOCKED_PRODUCTION_HOSTS:
        if host in normalized:
            raise BrokerUrlBlockedError(f"Blocked broker host detected: {host}")


def is_production_url(url: str) -> bool:
    normalized = (url or "").lower()
    return PRODUCTION_BASE_URL.lower() in normalized or "api.tastytrade.com" in normalized
