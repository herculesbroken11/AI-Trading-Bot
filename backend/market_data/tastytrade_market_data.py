"""
Tastytrade PRODUCTION market data — read-only quote snapshots (Checkpoint 2.9).

Production is used only for:
  POST /oauth/token            (read-scope token refresh)
  GET  /market-data/by-type    (quote snapshots)

This module never builds order/account/position requests, never imports the
execution router, order executor or sandbox broker adapter, and every
production request passes assert_market_data_request() which fails closed.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

import httpx

from backend.adapters.broker.oauth_diagnostics import parse_oauth_error_body
from backend.adapters.broker.sandbox_http_retry import (
    is_transient_transport_error,
    should_retry_http_status,
)
from backend.adapters.broker.sandbox_rate_limiter import (
    MIN_ENV_INTERVAL_SECONDS,
    RateLimitCooldownActive,
    RateLimitInfo,
    SandboxRateLimiter,
)
from backend.adapters.broker.sandbox_token_store import persist_refresh_token
from backend.config.tastytrade_urls import (
    MARKET_DATA_BASE_URL,
    MARKET_DATA_OAUTH_PATH,
    MARKET_DATA_QUOTES_PATH,
    USER_AGENT,
    assert_market_data_request,
)
from backend.market_data.config import MarketDataConfig
from backend.market_data.models import (
    TASTYTRADE_PRODUCTION_REST_SOURCE,
    MarketDataResult,
    normalize_tastytrade_quote,
)

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 20.0
DEFAULT_ACCESS_TOKEN_TTL_SECONDS = 900.0
ACCESS_TOKEN_EXPIRY_MARGIN_SECONDS = 60.0
MAX_TRANSIENT_ATTEMPTS = 3
TRANSIENT_BACKOFF_SECONDS = (0.25, 0.5, 1.0)
MAX_SYMBOLS_PER_REQUEST = 100

REFRESH_TOKEN_ENV_KEY = "TASTYTRADE_MARKET_DATA_REFRESH_TOKEN"

DEFAULT_EQUITY_SYMBOLS: Sequence[str] = ("TNA", "TZA", "IWM", "SPY", "QQQ")
DEFAULT_INDEX_SYMBOLS: Sequence[str] = ("VIX",)

# Separate rate-limit groups/state from the sandbox so production throttling
# never blocks sandbox scripts (and vice versa).
MARKET_DATA_OAUTH_GROUP = "market_data_oauth"
MARKET_DATA_QUOTES_GROUP = "market_data_quotes"
MARKET_DATA_QUOTE_TOKEN_GROUP = "market_data_quote_token"
MARKET_DATA_STREAM_GROUP = "market_data_stream"
MARKET_DATA_GROUPS = (
    MARKET_DATA_OAUTH_GROUP,
    MARKET_DATA_QUOTES_GROUP,
    MARKET_DATA_QUOTE_TOKEN_GROUP,
    MARKET_DATA_STREAM_GROUP,
)
DEFAULT_MARKET_DATA_MIN_INTERVALS: Dict[str, float] = {
    MARKET_DATA_OAUTH_GROUP: 10.0,
    MARKET_DATA_QUOTES_GROUP: 3.0,
    MARKET_DATA_QUOTE_TOKEN_GROUP: 10.0,
    MARKET_DATA_STREAM_GROUP: 5.0,
}
DEFAULT_MARKET_DATA_429_COOLDOWN_SECONDS = 60.0
MARKET_DATA_INTERVAL_ENV_PREFIX = "MARKET_DATA_MIN_INTERVAL_"
MARKET_DATA_COOLDOWN_ENV = "MARKET_DATA_429_COOLDOWN_SECONDS"
MARKET_DATA_STATE_PATH_ENV = "MARKET_DATA_RATE_STATE_PATH"
_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MARKET_DATA_STATE_PATH = _REPO_ROOT / ".market_data_rate_state.json"


class MarketDataError(RuntimeError):
    """Base error. Messages never include secrets or tokens."""

    def __init__(
        self,
        message: str,
        *,
        step: str,
        reason: str,
        status_code: Optional[int] = None,
        provider_message: Optional[str] = None,
        next_step: str = "",
        rate_limit: Optional[RateLimitInfo] = None,
    ) -> None:
        super().__init__(message)
        self.step = step
        self.reason = reason
        self.status_code = status_code
        self.provider_message = provider_message
        self.next_step = next_step
        self.rate_limit = rate_limit

    def format_safe(self) -> str:
        lines = [
            f"step: {self.step}",
            f"failure_reason: {self.reason}",
            f"status_code: {self.status_code if self.status_code is not None else 'n/a'}",
            f"provider_message: {self.provider_message or 'none'}",
            f"message: {self}",
        ]
        if self.next_step:
            lines.append(f"next_step: {self.next_step}")
        if self.rate_limit:
            lines.append(self.rate_limit.format_safe())
        return "\n".join(lines)


class MarketDataAuthError(MarketDataError):
    """Production market-data OAuth failed."""


class MarketDataApiError(MarketDataError):
    """Production quote request failed."""


# ---------------------------------------------------------------------------
# Rate limiter (market-data only)
# ---------------------------------------------------------------------------


def build_market_data_rate_limiter_from_env(
    env: Optional[Mapping[str, str]] = None,
) -> SandboxRateLimiter:
    source = env if env is not None else os.environ
    intervals = dict(DEFAULT_MARKET_DATA_MIN_INTERVALS)
    for group in MARKET_DATA_GROUPS:
        key = f"{MARKET_DATA_INTERVAL_ENV_PREFIX}{group.replace('market_data_', '').upper()}"
        raw = source.get(key)
        if raw is not None and str(raw).strip():
            try:
                intervals[group] = max(MIN_ENV_INTERVAL_SECONDS, float(raw))
            except ValueError:
                logger.warning("Ignoring invalid %s value (not a number)", key)

    cooldown = DEFAULT_MARKET_DATA_429_COOLDOWN_SECONDS
    raw_cooldown = source.get(MARKET_DATA_COOLDOWN_ENV)
    if raw_cooldown is not None and str(raw_cooldown).strip():
        try:
            cooldown = max(MIN_ENV_INTERVAL_SECONDS, float(raw_cooldown))
        except ValueError:
            logger.warning("Ignoring invalid %s value (not a number)", MARKET_DATA_COOLDOWN_ENV)

    raw_path = source.get(MARKET_DATA_STATE_PATH_ENV)
    if raw_path is None:
        state_path: Optional[Path] = DEFAULT_MARKET_DATA_STATE_PATH
    elif not raw_path.strip() or raw_path.strip().lower() in {"none", "off", "disabled"}:
        state_path = None
    else:
        state_path = Path(raw_path.strip())

    return SandboxRateLimiter(
        min_intervals=intervals,
        default_cooldown_seconds=cooldown,
        state_path=state_path,
    )


_market_data_limiter: Optional[SandboxRateLimiter] = None


def get_market_data_rate_limiter() -> SandboxRateLimiter:
    global _market_data_limiter
    if _market_data_limiter is None:
        _market_data_limiter = build_market_data_rate_limiter_from_env()
    return _market_data_limiter


def set_market_data_rate_limiter(limiter: Optional[SandboxRateLimiter]) -> None:
    global _market_data_limiter
    _market_data_limiter = limiter


# ---------------------------------------------------------------------------
# OAuth failure classification (production, read-only app)
# ---------------------------------------------------------------------------


def classify_market_data_oauth_failure(
    status_code: int,
    error_code: Optional[str],
    error_description: Optional[str],
) -> tuple:
    """Return (reason, next_step). No secret values involved."""
    code = (error_code or "").lower()
    text = f"{code} {(error_description or '').lower()}"
    if status_code == 429:
        return "rate_limited", "wait for the cooldown before retrying"
    if status_code >= 500:
        return "provider_unavailable", "Tastytrade production auth is unavailable; retry later"
    if "not a tastytrade customer" in text or "not a customer" in text:
        return (
            "not_production_customer",
            "the grant must come from a funded production Tastytrade account",
        )
    if "invalid_client" in code or "client secret" in text or "client_secret" in text:
        return (
            "secret_token_mismatch",
            "TASTYTRADE_MARKET_DATA_CLIENT_SECRET does not match the production OAuth app "
            "that issued the refresh token",
        )
    if "scope" in text:
        return (
            "invalid_scope",
            "the production grant must include the 'read' scope; set "
            "TASTYTRADE_MARKET_DATA_SCOPES=read",
        )
    if "revoked" in text or "expired" in text or "invalid_grant" in code:
        return (
            "invalid_refresh_token",
            "create a new personal grant in the production OAuth app and update "
            "TASTYTRADE_MARKET_DATA_REFRESH_TOKEN",
        )
    if status_code == 401:
        return "unauthorized", "check production market-data client secret and refresh token"
    if status_code == 403:
        return (
            "forbidden",
            "production account/app is not permitted (funded account and read scope required)",
        )
    if status_code == 400:
        return (
            "bad_request",
            "check that client secret and refresh token come from the same production OAuth app",
        )
    return "unknown_provider_error", "inspect provider_message; credentials were not printed"


def classify_market_data_api_failure(status_code: int) -> tuple:
    if status_code == 429:
        return "rate_limited", "wait for the cooldown before retrying"
    if status_code == 401:
        return "unauthorized", "access token rejected; re-run to refresh or create a new grant"
    if status_code == 403:
        return (
            "market_data_not_permitted",
            "REST quotes require a funded production account and a 'read' scoped token",
        )
    if status_code == 404:
        return "symbol_or_endpoint_not_found", "check symbols and instrument type"
    if status_code >= 500:
        return "provider_unavailable", "Tastytrade production market data unavailable; retry later"
    return "unknown_provider_error", "inspect provider_message"


# ---------------------------------------------------------------------------
# OAuth client (production, read-only)
# ---------------------------------------------------------------------------


class MarketDataOAuthClient:
    """Refresh-token OAuth against production for market data only (read scope)."""

    IS_MARKET_DATA_ONLY = True

    def __init__(
        self,
        config: MarketDataConfig,
        *,
        rate_limiter: Optional[SandboxRateLimiter] = None,
        clock: Callable[[], float] = time.time,
        refresh_token_persister: Optional[Callable[[str, str], bool]] = None,
    ) -> None:
        if config.read_only is not True:
            raise MarketDataAuthError(
                "Market-data OAuth requires MARKET_DATA_READ_ONLY=true",
                step="config",
                reason="read_only_required",
            )
        self._config = config
        self._rate_limiter = rate_limiter
        self._clock = clock
        self._refresh_token = config.refresh_token
        self._access_token: Optional[str] = None
        self._expires_at = 0.0
        self._failed = False
        self._token_request_count = 0
        self._granted_scope: Optional[str] = None
        self._persister = refresh_token_persister or _default_refresh_token_persister

    @property
    def base_url(self) -> str:
        return MARKET_DATA_BASE_URL

    @property
    def rate_limiter(self) -> SandboxRateLimiter:
        return self._rate_limiter or get_market_data_rate_limiter()

    @property
    def token_request_count(self) -> int:
        return self._token_request_count

    @property
    def granted_scope(self) -> Optional[str]:
        return self._granted_scope

    @property
    def is_authenticated(self) -> bool:
        return bool(self._access_token) and (
            self._clock() < self._expires_at - ACCESS_TOKEN_EXPIRY_MARGIN_SECONDS
        )

    def ensure_authenticated(self) -> None:
        if self.is_authenticated:
            return
        if self._failed:
            raise MarketDataAuthError(
                "Market-data OAuth already failed in this session; fix credentials and restart.",
                step="oauth_token",
                reason="oauth_previously_failed",
            )
        self._refresh()

    def force_refresh(self) -> None:
        self._access_token = None
        self.ensure_authenticated()

    def request_headers(self) -> Dict[str, str]:
        self.ensure_authenticated()
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }

    def _refresh(self) -> None:
        url = f"{MARKET_DATA_BASE_URL}{MARKET_DATA_OAUTH_PATH}"
        assert_market_data_request("POST", url)
        try:
            self.rate_limiter.acquire(MARKET_DATA_OAUTH_GROUP, step="market_data_oauth")
        except RateLimitCooldownActive as exc:
            raise MarketDataAuthError(
                "Market-data OAuth not attempted: local cooldown active after a prior 429.",
                step="oauth_token",
                reason="rate_limited",
                status_code=429,
                next_step="wait for the cooldown before retrying",
                rate_limit=exc.info,
            ) from exc

        self._token_request_count += 1
        payload = {
            "grant_type": "refresh_token",
            "client_secret": self._config.client_secret,
            "refresh_token": self._refresh_token,
            "scope": self._config.scopes,
        }
        try:
            with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
                response = client.post(
                    url,
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                        "User-Agent": USER_AGENT,
                    },
                    json=payload,
                )
        except httpx.HTTPError as exc:
            raise MarketDataAuthError(
                f"Market-data OAuth request failed: {type(exc).__name__}",
                step="oauth_token",
                reason="network_error",
                next_step="check network connectivity and retry",
            ) from exc

        status = response.status_code
        if status == 429:
            info = self.rate_limiter.record_rate_limited(
                MARKET_DATA_OAUTH_GROUP,
                step="market_data_oauth",
                retry_after=response.headers.get("Retry-After"),
            )
            raise MarketDataAuthError(
                "Market-data OAuth rate limited (429).",
                step="oauth_token",
                reason="rate_limited",
                status_code=429,
                next_step="wait for the cooldown before retrying",
                rate_limit=info,
            )
        if status >= 400:
            if status < 500:
                self._failed = True
            error_code, description = parse_oauth_error_body(response.text)
            reason, next_step = classify_market_data_oauth_failure(status, error_code, description)
            raise MarketDataAuthError(
                f"Market-data OAuth failed ({status}): {reason}",
                step="oauth_token",
                reason=reason,
                status_code=status,
                provider_message=description or error_code,
                next_step=next_step,
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise MarketDataAuthError(
                "Market-data OAuth returned a non-JSON response.",
                step="oauth_token",
                reason="invalid_response",
                status_code=status,
            ) from exc

        token = data.get("access_token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            raise MarketDataAuthError(
                "Market-data OAuth response missing access_token.",
                step="oauth_token",
                reason="invalid_response",
                status_code=status,
            )
        self._access_token = token
        self._expires_at = self._clock() + _token_ttl(data.get("expires_in"))
        scope = data.get("scope")
        self._granted_scope = scope if isinstance(scope, str) else None
        if self._granted_scope and "trade" in self._granted_scope.split():
            logger.warning(
                "market-data token reports 'trade' scope; it is still never used for orders. "
                "Prefer a read-only grant."
            )

        rotated = data.get("refresh_token")
        if isinstance(rotated, str) and rotated.strip() and rotated.strip() != self._refresh_token:
            previous = self._refresh_token
            self._refresh_token = rotated.strip()
            self._persister(self._refresh_token, previous)


def _default_refresh_token_persister(new_token: str, previous: str) -> bool:
    return persist_refresh_token(
        new_token,
        env_key=REFRESH_TOKEN_ENV_KEY,
        previous_refresh_token=previous,
        label="market-data",
    )


def _token_ttl(raw: Any) -> float:
    try:
        ttl = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_ACCESS_TOKEN_TTL_SECONDS
    return ttl if ttl > 0 else DEFAULT_ACCESS_TOKEN_TTL_SECONDS


# ---------------------------------------------------------------------------
# Quote client (production, read-only)
# ---------------------------------------------------------------------------


def _normalize_symbols(symbols: Iterable[str]) -> List[str]:
    seen: List[str] = []
    for raw in symbols:
        symbol = (raw or "").strip().upper()
        if symbol and symbol not in seen:
            seen.append(symbol)
    return seen


class TastytradeMarketDataClient:
    """
    Read-only quote snapshots from Tastytrade production.

    Deliberately exposes no order/account/position methods.
    """

    IS_MARKET_DATA_ONLY = True

    def __init__(
        self,
        config: MarketDataConfig,
        *,
        auth: Optional[MarketDataOAuthClient] = None,
        rate_limiter: Optional[SandboxRateLimiter] = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if config.read_only is not True:
            raise MarketDataApiError(
                "Market-data client requires MARKET_DATA_READ_ONLY=true",
                step="config",
                reason="read_only_required",
            )
        self._config = config
        self._rate_limiter = rate_limiter
        self._auth = auth or MarketDataOAuthClient(config, rate_limiter=rate_limiter)
        self._sleep = sleep
        self._quote_request_count = 0

    @property
    def base_url(self) -> str:
        return MARKET_DATA_BASE_URL

    @property
    def auth(self) -> MarketDataOAuthClient:
        return self._auth

    @property
    def rate_limiter(self) -> SandboxRateLimiter:
        return self._rate_limiter or get_market_data_rate_limiter()

    @property
    def quote_request_count(self) -> int:
        return self._quote_request_count

    def get_quotes(
        self,
        equities: Iterable[str] = DEFAULT_EQUITY_SYMBOLS,
        *,
        indices: Iterable[str] = (),
    ) -> MarketDataResult:
        """
        Fetch quote snapshots. Equities are required; indices (e.g. VIX) are optional
        and reported as unsupported instead of failing when not served.
        """
        equity_symbols = _normalize_symbols(equities)
        index_symbols = _normalize_symbols(indices)
        total = len(equity_symbols) + len(index_symbols)
        if total == 0:
            raise ValueError("at least one symbol is required")
        if total > MAX_SYMBOLS_PER_REQUEST:
            raise ValueError(f"at most {MAX_SYMBOLS_PER_REQUEST} symbols per request")

        result = MarketDataResult(requested=equity_symbols + index_symbols)

        if equity_symbols:
            items = self._fetch_items({"equity": ",".join(equity_symbols)}, step="quotes_equity")
            self._collect(items, equity_symbols, result)
            for symbol in equity_symbols:
                if symbol not in result.quotes:
                    result.missing.append(symbol)

        if index_symbols:
            try:
                items = self._fetch_items({"index": ",".join(index_symbols)}, step="quotes_index")
            except MarketDataApiError as exc:
                if exc.reason == "rate_limited" or (exc.status_code or 0) >= 500:
                    raise
                for symbol in index_symbols:
                    result.unsupported[symbol] = (
                        f"index quotes not available via REST ({exc.reason}); skipped"
                    )
            else:
                self._collect(items, index_symbols, result)
                for symbol in index_symbols:
                    if symbol not in result.quotes:
                        result.unsupported[symbol] = (
                            "index quote not returned by Tastytrade REST; skipped"
                        )

        result.fetched_at = datetime.now(timezone.utc)
        return result

    @staticmethod
    def _collect(
        items: List[Dict[str, Any]],
        wanted: List[str],
        result: MarketDataResult,
    ) -> None:
        for item in items:
            try:
                quote = normalize_tastytrade_quote(item, source=TASTYTRADE_PRODUCTION_REST_SOURCE)
            except ValueError:
                continue
            if quote.symbol in wanted:
                result.quotes[quote.symbol] = quote

    def _fetch_items(self, params: Dict[str, str], *, step: str) -> List[Dict[str, Any]]:
        response = self._get(MARKET_DATA_QUOTES_PATH, params=params, step=step)
        try:
            body = response.json()
        except ValueError as exc:
            raise MarketDataApiError(
                "Quote response was not JSON.",
                step=step,
                reason="invalid_response",
                status_code=response.status_code,
            ) from exc
        data = body.get("data") if isinstance(body, dict) else None
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return []
        return [item for item in items if isinstance(item, dict)]

    def _acquire(self, step: str) -> None:
        try:
            self.rate_limiter.acquire(MARKET_DATA_QUOTES_GROUP, step=step)
        except RateLimitCooldownActive as exc:
            raise MarketDataApiError(
                "Quote request not sent: local cooldown active after a prior 429.",
                step=step,
                reason="rate_limited",
                status_code=429,
                next_step="wait for the cooldown before retrying",
                rate_limit=exc.info,
            ) from exc

    def _get(self, path: str, *, params: Dict[str, str], step: str) -> httpx.Response:
        url = f"{MARKET_DATA_BASE_URL}{path}"
        assert_market_data_request("GET", url)
        headers = self._auth.request_headers()

        refreshed_after_401 = False
        last_status: Optional[int] = None
        for attempt in range(MAX_TRANSIENT_ATTEMPTS):
            is_last = attempt >= MAX_TRANSIENT_ATTEMPTS - 1
            self._acquire(step)
            self._quote_request_count += 1
            try:
                with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
                    response = client.get(url, headers=headers, params=params)
            except Exception as exc:
                if is_transient_transport_error(exc) and not is_last:
                    self._sleep(TRANSIENT_BACKOFF_SECONDS[attempt])
                    continue
                raise MarketDataApiError(
                    f"Quote request transport error: {type(exc).__name__}",
                    step=step,
                    reason="network_error",
                    next_step="check network connectivity and retry",
                ) from exc

            status = response.status_code
            last_status = status
            if status < 400:
                return response
            if status == 429:
                info = self.rate_limiter.record_rate_limited(
                    MARKET_DATA_QUOTES_GROUP,
                    step=step,
                    retry_after=response.headers.get("Retry-After"),
                )
                raise self._error(response, step=step, rate_limit=info)
            if status == 401 and not refreshed_after_401 and not is_last:
                refreshed_after_401 = True
                self._auth.force_refresh()
                headers = self._auth.request_headers()
                continue
            if should_retry_http_status(status) and not is_last:
                self._sleep(TRANSIENT_BACKOFF_SECONDS[attempt])
                continue
            raise self._error(response, step=step)

        raise MarketDataApiError(
            "Quote request failed after retries.",
            step=step,
            reason="provider_unavailable",
            status_code=last_status,
        )

    @staticmethod
    def _error(
        response: httpx.Response,
        *,
        step: str,
        rate_limit: Optional[RateLimitInfo] = None,
    ) -> MarketDataApiError:
        status = response.status_code
        error_code, description = parse_oauth_error_body(response.text)
        reason, next_step = classify_market_data_api_failure(status)
        return MarketDataApiError(
            f"Quote request failed ({status}): {reason}",
            step=step,
            reason=reason,
            status_code=status,
            provider_message=description or error_code,
            next_step=next_step,
            rate_limit=rate_limit,
        )
