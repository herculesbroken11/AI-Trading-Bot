"""
Tastytrade DXLink streaming market data — READ-ONLY (Checkpoint 2.10).

Flow: GET /api-quote-tokens (production REST, read-scope OAuth token) ->
wss dxlink-url -> SETUP -> AUTH_STATE UNAUTHORIZED -> AUTH -> CHANNEL_REQUEST
(FEED) -> FEED_SETUP (COMPACT) -> FEED_SUBSCRIPTION (Quote/Trade/Summary).

This module never builds order/account requests and never imports the
execution router, order executor or sandbox broker adapter.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Protocol, Sequence

import httpx

from backend.adapters.broker.oauth_diagnostics import parse_oauth_error_body, redact_oauth_body
from backend.adapters.broker.sandbox_http_retry import is_transient_transport_error
from backend.adapters.broker.sandbox_rate_limiter import RateLimitCooldownActive, SandboxRateLimiter
from backend.config.tastytrade_urls import (
    MARKET_DATA_BASE_URL,
    MARKET_DATA_QUOTE_TOKEN_PATH,
    USER_AGENT,
    MarketDataUrlBlockedError,
    assert_dxlink_stream_url,
    assert_market_data_request,
)
from backend.market_data.config import MarketDataConfig, safe_fingerprint
from backend.market_data.models import parse_timestamp
from backend.market_data.stream_models import (
    DEFAULT_AGGREGATION_PERIOD_SECONDS,
    DEFAULT_EVENT_FIELDS,
    DEFAULT_EVENT_TYPES,
    DEFAULT_KEEPALIVE_INTERVAL_SECONDS,
    DEFAULT_KEEPALIVE_TIMEOUT_SECONDS,
    DEFAULT_STREAM_MAX_QUOTE_AGE_SECONDS,
    DEFAULT_STREAM_SYMBOLS,
    FEED_CHANNEL,
    StreamState,
    build_auth_frame,
    build_channel_request_frame,
    build_feed_setup_frame,
    build_feed_subscription_frame,
    build_keepalive_frame,
    build_setup_frame,
    parse_compact_feed_data,
)
from backend.market_data.tastytrade_market_data import (
    MARKET_DATA_QUOTE_TOKEN_GROUP,
    MARKET_DATA_STREAM_GROUP,
    MarketDataError,
    MarketDataOAuthClient,
    classify_market_data_api_failure,
    get_market_data_rate_limiter,
)

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 20.0
# Tokens are valid 24h; without expires-at assume a conservative lifetime.
DEFAULT_QUOTE_TOKEN_TTL_SECONDS = 12 * 3600.0
QUOTE_TOKEN_EXPIRY_MARGIN_SECONDS = 300.0
DEFAULT_HANDSHAKE_TIMEOUT_SECONDS = 10.0
DEFAULT_POLL_INTERVAL_SECONDS = 0.1
DEFAULT_SAMPLE_INTERVAL_SECONDS = 0.1
MIN_RECV_TIMEOUT_SECONDS = 0.01
DEFAULT_MAX_RECONNECTS = 2
RECONNECT_BACKOFF_SECONDS = (1.0, 2.0, 5.0, 10.0, 30.0)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class DXLinkError(MarketDataError):
    """DXLink quote-token / connection / auth / subscription failure (no secrets)."""


# ---------------------------------------------------------------------------
# Quote token (production REST, cached)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QuoteToken:
    token: str = field(repr=False)
    dxlink_url: str
    level: Optional[str]
    issued_at: Optional[datetime]
    expires_at: Optional[datetime]
    fetched_at: float

    @property
    def fingerprint(self) -> str:
        return safe_fingerprint(self.token)

    @property
    def is_realtime(self) -> bool:
        return "delay" not in (self.level or "").lower()

    def safe_summary(self) -> Dict[str, Any]:
        return {
            "dxlink_url": self.dxlink_url,
            "level": self.level,
            "issued_at": self.issued_at.isoformat() if self.issued_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "quote_token": self.fingerprint,
        }


class DXLinkQuoteTokenProvider:
    """
    Fetches and caches the DXLink API quote token (GET /api-quote-tokens).

    Cached in memory until near expires-at (or a safe TTL) so repeated
    connects/reconnects never re-request a token unnecessarily.
    """

    IS_MARKET_DATA_ONLY = True

    def __init__(
        self,
        config: MarketDataConfig,
        *,
        auth: Optional[MarketDataOAuthClient] = None,
        rate_limiter: Optional[SandboxRateLimiter] = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = _utc_now,
        default_ttl_seconds: float = DEFAULT_QUOTE_TOKEN_TTL_SECONDS,
    ) -> None:
        if config.read_only is not True:
            raise DXLinkError(
                "DXLink requires MARKET_DATA_READ_ONLY=true",
                step="config",
                reason="read_only_required",
            )
        self._config = config
        self._rate_limiter = rate_limiter
        self._auth = auth or MarketDataOAuthClient(config, rate_limiter=rate_limiter)
        self._clock = clock
        self._wall_clock = wall_clock
        self._default_ttl = default_ttl_seconds
        self._cached: Optional[QuoteToken] = None
        self._token_request_count = 0

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
    def token_request_count(self) -> int:
        return self._token_request_count

    def _is_fresh(self, token: QuoteToken) -> bool:
        if token.expires_at is not None:
            return self._wall_clock() < token.expires_at - timedelta(
                seconds=QUOTE_TOKEN_EXPIRY_MARGIN_SECONDS
            )
        return self._clock() < token.fetched_at + self._default_ttl - QUOTE_TOKEN_EXPIRY_MARGIN_SECONDS

    def get_token(self, *, force_refresh: bool = False) -> QuoteToken:
        if not force_refresh and self._cached is not None and self._is_fresh(self._cached):
            return self._cached
        self._cached = self._fetch()
        return self._cached

    def invalidate(self) -> None:
        self._cached = None

    def _fetch(self) -> QuoteToken:
        step = "quote_token"
        url = f"{MARKET_DATA_BASE_URL}{MARKET_DATA_QUOTE_TOKEN_PATH}"
        assert_market_data_request("GET", url)
        headers = self._auth.request_headers()
        refreshed_after_401 = False

        for attempt in range(2):
            try:
                self.rate_limiter.acquire(MARKET_DATA_QUOTE_TOKEN_GROUP, step=step)
            except RateLimitCooldownActive as exc:
                raise DXLinkError(
                    "Quote token not requested: local cooldown active after a prior 429.",
                    step=step,
                    reason="rate_limited",
                    status_code=429,
                    next_step="wait for the cooldown before retrying",
                    rate_limit=exc.info,
                ) from exc

            self._token_request_count += 1
            try:
                with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
                    response = client.get(url, headers=headers)
            except Exception as exc:
                reason = "network_error" if is_transient_transport_error(exc) else "request_failed"
                raise DXLinkError(
                    f"Quote token request failed: {type(exc).__name__}",
                    step=step,
                    reason=reason,
                    next_step="check network connectivity and retry",
                ) from exc

            status = response.status_code
            if status == 429:
                info = self.rate_limiter.record_rate_limited(
                    MARKET_DATA_QUOTE_TOKEN_GROUP,
                    step=step,
                    retry_after=response.headers.get("Retry-After"),
                )
                raise DXLinkError(
                    "Quote token request rate limited (429).",
                    step=step,
                    reason="rate_limited",
                    status_code=429,
                    next_step="wait for the cooldown before retrying",
                    rate_limit=info,
                )
            if status == 401 and not refreshed_after_401 and attempt == 0:
                refreshed_after_401 = True
                self._auth.force_refresh()
                headers = self._auth.request_headers()
                continue
            if status >= 400:
                error_code, description = parse_oauth_error_body(response.text)
                if status == 403:
                    reason, next_step = (
                        "quote_token_not_permitted",
                        "streaming requires a funded production account with market-data "
                        "access and a 'read' scoped token",
                    )
                else:
                    reason, next_step = classify_market_data_api_failure(status)
                raise DXLinkError(
                    f"Quote token request failed ({status}): {reason}",
                    step=step,
                    reason=reason,
                    status_code=status,
                    provider_message=description or error_code,
                    next_step=next_step,
                )
            return self._parse(response)

        raise DXLinkError(
            "Quote token request failed after token refresh.",
            step=step,
            reason="unauthorized",
            status_code=401,
        )

    def _parse(self, response: httpx.Response) -> QuoteToken:
        step = "quote_token"
        try:
            body = response.json()
        except ValueError as exc:
            raise DXLinkError(
                "Quote token response was not JSON.",
                step=step,
                reason="invalid_response",
                status_code=response.status_code,
            ) from exc
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            data = {}
        token = data.get("token")
        dxlink_url = data.get("dxlink-url") or data.get("dxlinkUrl")
        if not isinstance(token, str) or not token or not isinstance(dxlink_url, str):
            raise DXLinkError(
                "Quote token response missing token or dxlink-url.",
                step=step,
                reason="invalid_response",
                status_code=response.status_code,
            )
        try:
            assert_dxlink_stream_url(dxlink_url)
        except MarketDataUrlBlockedError as exc:
            raise DXLinkError(
                str(exc),
                step=step,
                reason="dxlink_url_blocked",
                next_step="unexpected streamer URL; refusing to connect",
            ) from exc
        level = data.get("level")
        return QuoteToken(
            token=token,
            dxlink_url=dxlink_url,
            level=level if isinstance(level, str) else None,
            issued_at=parse_timestamp(data.get("issued-at")),
            expires_at=parse_timestamp(data.get("expires-at")),
            fetched_at=self._clock(),
        )


# ---------------------------------------------------------------------------
# WebSocket connection abstraction
# ---------------------------------------------------------------------------


class StreamConnectionClosed(Exception):
    """The WebSocket closed (remote close or network drop)."""


class StreamConnection(Protocol):
    def send(self, message: str) -> None: ...

    def recv(self, timeout: float) -> str:
        """Return the next text frame or raise TimeoutError / StreamConnectionClosed."""
        ...

    def close(self) -> None: ...


ConnectFactory = Callable[[str, float], StreamConnection]


class _WebsocketsConnection:
    def __init__(self, ws: Any) -> None:
        from websockets.exceptions import ConnectionClosed

        self._ws = ws
        self._closed_exc = ConnectionClosed

    def send(self, message: str) -> None:
        try:
            self._ws.send(message)
        except self._closed_exc as exc:
            raise StreamConnectionClosed(type(exc).__name__) from exc

    def recv(self, timeout: float) -> str:
        try:
            message = self._ws.recv(timeout=timeout)
        except self._closed_exc as exc:
            raise StreamConnectionClosed(type(exc).__name__) from exc
        if isinstance(message, bytes):
            return message.decode("utf-8", errors="replace")
        return message

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:
            pass


def websockets_connect(url: str, open_timeout: float) -> StreamConnection:
    assert_dxlink_stream_url(url)
    from websockets.sync.client import connect

    ws = connect(
        url,
        open_timeout=open_timeout,
        close_timeout=2,
        user_agent_header=USER_AGENT,
        max_size=4 * 1024 * 1024,
    )
    return _WebsocketsConnection(ws)


# ---------------------------------------------------------------------------
# DXLink stream client (read-only)
# ---------------------------------------------------------------------------


@dataclass
class StreamRunSummary:
    connected: bool = False
    authorized: bool = False
    channel_opened: bool = False
    feed_configured: bool = False
    subscribed: bool = False
    data_format: Optional[str] = None
    messages_received: int = 0
    feed_data_messages: int = 0
    keepalives_sent: int = 0
    reconnects: int = 0
    duration_seconds: float = 0.0
    warnings: List[str] = field(default_factory=list)


class DXLinkStreamClient:
    """
    Read-only DXLink quote stream. Exposes no order/account methods.

    Never busy-loops: recv() blocks with a timeout, and reconnects use
    exponential backoff with a hard cap.
    """

    IS_MARKET_DATA_ONLY = True

    def __init__(
        self,
        token_provider: DXLinkQuoteTokenProvider,
        *,
        symbols: Iterable[str] = DEFAULT_STREAM_SYMBOLS,
        event_types: Sequence[str] = DEFAULT_EVENT_TYPES,
        event_fields: Optional[Mapping[str, Sequence[str]]] = None,
        connect_factory: Optional[ConnectFactory] = None,
        rate_limiter: Optional[SandboxRateLimiter] = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = _utc_now,
        sleep: Callable[[float], None] = time.sleep,
        keepalive_interval: float = DEFAULT_KEEPALIVE_INTERVAL_SECONDS,
        handshake_timeout: float = DEFAULT_HANDSHAKE_TIMEOUT_SECONDS,
        poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
        sample_interval: float = DEFAULT_SAMPLE_INTERVAL_SECONDS,
        max_reconnects: int = DEFAULT_MAX_RECONNECTS,
        aggregation_period: float = DEFAULT_AGGREGATION_PERIOD_SECONDS,
    ) -> None:
        self._token_provider = token_provider
        self._symbols = [s.strip().upper() for s in symbols if s and s.strip()]
        if not self._symbols:
            raise ValueError("at least one symbol is required")
        self._event_types = list(event_types)
        requested = event_fields or DEFAULT_EVENT_FIELDS
        self._requested_fields: Dict[str, List[str]] = {
            name: list(requested[name]) for name in self._event_types if name in requested
        }
        self._event_fields: Dict[str, List[str]] = dict(self._requested_fields)
        self._connect_factory = connect_factory or websockets_connect
        self._rate_limiter = rate_limiter
        self._clock = clock
        self._wall_clock = wall_clock
        self._sleep = sleep
        self._keepalive_interval = keepalive_interval
        self._handshake_timeout = handshake_timeout
        self._poll_interval = poll_interval
        self._sample_interval = sample_interval
        self._max_reconnects = max_reconnects
        self._aggregation_period = aggregation_period
        self._conn: Optional[StreamConnection] = None
        self._state = StreamState(self._symbols)
        self._summary = StreamRunSummary()

    @property
    def base_url(self) -> str:
        return MARKET_DATA_BASE_URL

    @property
    def symbols(self) -> List[str]:
        return list(self._symbols)

    @property
    def state(self) -> StreamState:
        return self._state

    @property
    def summary(self) -> StreamRunSummary:
        return self._summary

    def now(self) -> float:
        """Current reading of the clock used for quote-age calculations."""
        return self._clock()

    @property
    def event_fields(self) -> Dict[str, List[str]]:
        return {k: list(v) for k, v in self._event_fields.items()}

    @property
    def rate_limiter(self) -> SandboxRateLimiter:
        return self._rate_limiter or get_market_data_rate_limiter()

    def __enter__(self) -> "DXLinkStreamClient":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.disconnect()

    # -- public ------------------------------------------------------------

    def connect(self) -> None:
        """Handshake + subscribe. Retries AUTH once with a freshly fetched quote token."""
        try:
            self._handshake(force_new_token=False)
        except DXLinkError as exc:
            if exc.reason != "auth_failed":
                raise
            self._close_connection()
            self._token_provider.invalidate()
            self._handshake(force_new_token=True)

    def stream(
        self,
        duration_seconds: float,
        *,
        max_age_seconds: float = DEFAULT_STREAM_MAX_QUOTE_AGE_SECONDS,
    ) -> StreamRunSummary:
        """Consume events for duration_seconds, sampling quote age along the way."""
        if self._conn is None:
            self.connect()
        start = self._clock()
        end = start + max(0.0, duration_seconds)
        next_keepalive = start + self._keepalive_interval
        next_sample = start
        while True:
            now = self._clock()
            if now >= end:
                break
            timeout = max(
                MIN_RECV_TIMEOUT_SECONDS,
                min(self._poll_interval, end - now, next_keepalive - now),
            )
            raw: Optional[str] = None
            try:
                raw = self._recv(timeout)
            except TimeoutError:
                raw = None
            except StreamConnectionClosed:
                self._reconnect(deadline=end)
                next_keepalive = self._clock() + self._keepalive_interval
                continue
            if raw is not None:
                self._handle(raw, during_stream=True)
            now = self._clock()
            if now >= next_keepalive:
                self._send(build_keepalive_frame())
                self._summary.keepalives_sent += 1
                next_keepalive = now + self._keepalive_interval
            if now >= next_sample:
                self._state.sample(now, max_age_seconds)
                next_sample = now + self._sample_interval
        self._state.sample(self._clock(), max_age_seconds)
        self._summary.duration_seconds = self._clock() - start
        return self._summary

    def disconnect(self) -> None:
        self._close_connection()

    # -- handshake ---------------------------------------------------------

    def _handshake(self, *, force_new_token: bool) -> None:
        token = self._token_provider.get_token(force_refresh=force_new_token)
        self._state.set_realtime(token.is_realtime)
        try:
            self.rate_limiter.acquire(MARKET_DATA_STREAM_GROUP, step="dxlink_connect")
        except RateLimitCooldownActive as exc:
            raise DXLinkError(
                "DXLink connect not attempted: market-data cooldown active.",
                step="connect",
                reason="rate_limited",
                next_step="wait for the cooldown before retrying",
                rate_limit=exc.info,
            ) from exc
        try:
            assert_dxlink_stream_url(token.dxlink_url)
            self._conn = self._connect_factory(token.dxlink_url, self._handshake_timeout)
        except MarketDataUrlBlockedError as exc:
            raise DXLinkError(str(exc), step="connect", reason="dxlink_url_blocked") from exc
        except Exception as exc:
            raise DXLinkError(
                f"DXLink WebSocket connection failed: {type(exc).__name__}",
                step="connect",
                reason="connection_failed",
                next_step="check network/firewall access to the dxfeed streamer and retry",
            ) from exc
        self._summary.connected = True

        self._send(build_setup_frame(keepalive_timeout=DEFAULT_KEEPALIVE_TIMEOUT_SECONDS))
        auth_state = self._wait_for(lambda m: m.get("type") == "AUTH_STATE", step="setup")
        if str(auth_state.get("state", "")).upper() != "AUTHORIZED":
            self._send(build_auth_frame(token.token))
            auth_state = self._wait_for(lambda m: m.get("type") == "AUTH_STATE", step="auth")
            if str(auth_state.get("state", "")).upper() != "AUTHORIZED":
                raise DXLinkError(
                    "DXLink rejected the quote token (AUTH_STATE UNAUTHORIZED).",
                    step="auth",
                    reason="auth_failed",
                    next_step="quote token invalid/expired or account lacks streaming entitlement",
                )
        self._summary.authorized = True

        self._send(build_channel_request_frame(FEED_CHANNEL))
        self._wait_for(
            lambda m: m.get("type") == "CHANNEL_OPENED" and m.get("channel") == FEED_CHANNEL,
            step="channel",
        )
        self._summary.channel_opened = True

        self._send(
            build_feed_setup_frame(
                channel=FEED_CHANNEL,
                event_fields=self._requested_fields,
                aggregation_period=self._aggregation_period,
            )
        )
        config = self._wait_for(
            lambda m: m.get("type") == "FEED_CONFIG" and m.get("channel") == FEED_CHANNEL,
            step="feed_setup",
            required=False,
        )
        if config is None:
            self._summary.warnings.append(
                "FEED_CONFIG not received before timeout; using requested COMPACT fields"
            )
        self._summary.feed_configured = True

        self._send(
            build_feed_subscription_frame(
                self._symbols,
                channel=FEED_CHANNEL,
                event_types=self._event_types,
            )
        )
        self._summary.subscribed = True

    def _wait_for(
        self,
        predicate: Callable[[Dict[str, Any]], bool],
        *,
        step: str,
        required: bool = True,
    ) -> Dict[str, Any]:
        deadline = self._clock() + self._handshake_timeout
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                if not required:
                    return None  # type: ignore[return-value]
                raise DXLinkError(
                    f"Timed out waiting for DXLink response during {step}.",
                    step=step,
                    reason="timeout",
                    next_step="retry later; the streamer did not respond",
                )
            try:
                raw = self._recv(max(MIN_RECV_TIMEOUT_SECONDS, remaining))
            except TimeoutError:
                continue
            except StreamConnectionClosed as exc:
                raise DXLinkError(
                    f"DXLink connection closed during {step}.",
                    step=step,
                    reason="connection_closed",
                ) from exc
            message = self._handle(raw, during_stream=False, step=step)
            if message is not None and predicate(message):
                return message

    # -- message handling --------------------------------------------------

    def _handle(
        self,
        raw: str,
        *,
        during_stream: bool,
        step: str = "stream",
    ) -> Optional[Dict[str, Any]]:
        self._summary.messages_received += 1
        try:
            message = json.loads(raw)
        except (TypeError, ValueError):
            return None
        if not isinstance(message, dict):
            return None
        kind = message.get("type")
        if kind == "ERROR":
            error = str(message.get("error") or "UNKNOWN")
            detail = redact_oauth_body(str(message.get("message") or ""))[:300]
            reason = "auth_failed" if "UNAUTHORIZED" in error.upper() else "protocol_error"
            raise DXLinkError(
                f"DXLink ERROR during {step}: {error}",
                step=step,
                reason=reason,
                provider_message=detail or error,
            )
        if kind == "FEED_CONFIG" and message.get("channel") == FEED_CHANNEL:
            self._apply_feed_config(message)
        elif kind == "FEED_DATA" and message.get("channel") == FEED_CHANNEL:
            self._summary.feed_data_messages += 1
            received_at = self._clock()
            wall = self._wall_clock()
            for event in parse_compact_feed_data(message.get("data"), self._event_fields):
                self._state.apply_event(event, received_at=received_at, wall_time=wall)
        elif during_stream and kind == "AUTH_STATE":
            if str(message.get("state", "")).upper() != "AUTHORIZED":
                raise DXLinkError(
                    "DXLink de-authorized the session mid-stream.",
                    step="stream",
                    reason="auth_lost",
                )
        elif during_stream and kind == "CHANNEL_CLOSED" and message.get("channel") == FEED_CHANNEL:
            raise DXLinkError(
                "DXLink closed the FEED channel.",
                step="stream",
                reason="subscription_closed",
            )
        return message

    def _apply_feed_config(self, message: Mapping[str, Any]) -> None:
        data_format = message.get("dataFormat")
        if isinstance(data_format, str):
            self._summary.data_format = data_format
        fields = message.get("eventFields")
        if isinstance(fields, dict):
            for name, values in fields.items():
                if isinstance(name, str) and isinstance(values, list) and values:
                    self._event_fields[name] = [str(v) for v in values]

    # -- transport ---------------------------------------------------------

    def _send(self, frame: Dict[str, Any]) -> None:
        if self._conn is None:
            raise DXLinkError("DXLink not connected.", step="send", reason="not_connected")
        self._conn.send(json.dumps(frame, separators=(",", ":")))

    def _recv(self, timeout: float) -> str:
        if self._conn is None:
            raise StreamConnectionClosed("not connected")
        return self._conn.recv(timeout)

    def _close_connection(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def _reconnect(self, *, deadline: float) -> None:
        """Bounded reconnect with backoff; reuses the cached quote token."""
        self._close_connection()
        while True:
            if self._summary.reconnects >= self._max_reconnects:
                raise DXLinkError(
                    f"DXLink connection lost; reconnect limit ({self._max_reconnects}) reached.",
                    step="stream",
                    reason="connection_lost",
                    next_step="check network stability and retry later",
                )
            delay = RECONNECT_BACKOFF_SECONDS[
                min(self._summary.reconnects, len(RECONNECT_BACKOFF_SECONDS) - 1)
            ]
            if self._clock() + delay >= deadline:
                raise DXLinkError(
                    "DXLink connection lost near the end of the stream window.",
                    step="stream",
                    reason="connection_lost",
                )
            self._summary.reconnects += 1
            logger.info("DXLink reconnect %d in %.1fs", self._summary.reconnects, delay)
            self._sleep(delay)
            try:
                self._handshake(force_new_token=False)
                return
            except DXLinkError as exc:
                self._close_connection()
                if exc.reason in {"rate_limited", "auth_failed", "dxlink_url_blocked"}:
                    raise
