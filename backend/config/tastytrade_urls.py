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


# Production market data (Checkpoint 2.9): read-only quotes. Only these
# (method, path) pairs may ever reach the production host.
MARKET_DATA_BASE_URL = PRODUCTION_BASE_URL
MARKET_DATA_OAUTH_PATH = "/oauth/token"
MARKET_DATA_QUOTES_PATH = "/market-data/by-type"
MARKET_DATA_QUOTE_TOKEN_PATH = "/api-quote-tokens"
MARKET_DATA_ALLOWED_REQUESTS = frozenset(
    {
        ("POST", MARKET_DATA_OAUTH_PATH),
        ("GET", MARKET_DATA_QUOTES_PATH),
        ("GET", MARKET_DATA_QUOTE_TOKEN_PATH),
    }
)
# DXLink streamer hosts returned by /api-quote-tokens (wss only).
DXLINK_ALLOWED_HOST_SUFFIXES = (".dxfeed.com",)


class MarketDataUrlBlockedError(BrokerUrlBlockedError):
    """Raised when a market-data request targets anything other than quote/auth endpoints."""


def assert_dxlink_stream_url(url: str) -> None:
    """DXLink streaming URL must be wss:// on a dxfeed host; anything else fails closed."""
    from urllib.parse import urlsplit

    parts = urlsplit((url or "").strip())
    host = (parts.hostname or "").lower()
    if parts.scheme.lower() != "wss":
        raise MarketDataUrlBlockedError(
            f"DXLink URL scheme {parts.scheme!r} is not permitted; only wss:// is allowed."
        )
    if not host or not any(host.endswith(suffix) for suffix in DXLINK_ALLOWED_HOST_SUFFIXES):
        raise MarketDataUrlBlockedError(
            f"DXLink host {host!r} is not permitted; expected a *.dxfeed.com streamer."
        )


class MarketDataCredentialMisuseError(BrokerUrlBlockedError):
    """Raised when a production market-data component is handed to the execution path."""


def assert_execution_component(component: object, *, role: str) -> None:
    """
    Fail closed if an execution-path component is a market-data-only object
    (production credentials) or points at a production host.
    """
    if component is None:
        return
    if getattr(component, "IS_MARKET_DATA_ONLY", False) is True:
        raise MarketDataCredentialMisuseError(
            f"{role}: production market-data credentials/clients cannot be used for "
            "order execution. Execution is sandbox-only."
        )
    base_url = getattr(component, "base_url", None)
    if isinstance(base_url, str) and is_production_url(base_url):
        raise MarketDataCredentialMisuseError(
            f"{role}: production host {base_url!r} is blocked for order execution."
        )


def assert_market_data_request(method: str, url: str) -> None:
    """
    Allow only production OAuth token refresh and quote snapshots.

    Any account, order, position or balance path on production fails closed.
    """
    from urllib.parse import urlsplit

    verb = (method or "").strip().upper()
    parts = urlsplit((url or "").strip())
    origin = f"{parts.scheme}://{parts.netloc}".lower()
    if origin != MARKET_DATA_BASE_URL.lower():
        raise MarketDataUrlBlockedError(
            f"Market data host {parts.netloc!r} is not permitted; "
            f"only {MARKET_DATA_BASE_URL} is used for read-only quotes."
        )
    path = (parts.path or "/").rstrip("/") or "/"
    if (verb, path) not in MARKET_DATA_ALLOWED_REQUESTS:
        raise MarketDataUrlBlockedError(
            f"Production request {verb} {path} is blocked. Production is market-data "
            "only; orders/accounts/positions are never sent to production."
        )
