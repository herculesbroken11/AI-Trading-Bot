"""Checkpoint 2.10 — DXLink streaming market data (read-only), sandbox-only execution."""

from __future__ import annotations

import inspect
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

import backend.market_data.dxlink_stream as dx
import backend.market_data.stream_models as sm
import backend.market_data.tastytrade_market_data as md
from backend.adapters.broker.sandbox_auth import SandboxAuthError
from backend.adapters.broker.tastytrade_sandbox import TastytradeSandboxAdapter
from backend.bot_worker.sandbox_worker import SandboxBotWorker
from backend.config.settings import ConfigurationError, Settings, load_settings
from backend.config.tastytrade_urls import (
    PRODUCTION_BASE_URL,
    MarketDataCredentialMisuseError,
    MarketDataUrlBlockedError,
    assert_dxlink_stream_url,
    assert_market_data_request,
)
from backend.execution.execution_router import ExecutionRouter
from backend.market_data.config import validate_market_data_settings
from backend.market_data.dxlink_stream import (
    DXLinkError,
    DXLinkQuoteTokenProvider,
    DXLinkStreamClient,
    QuoteToken,
    StreamConnectionClosed,
)
from backend.market_data.stream_models import (
    DEFAULT_EVENT_FIELDS,
    DEFAULT_STREAM_SYMBOLS,
    StreamState,
    SymbolStreamState,
    build_auth_frame,
    build_channel_request_frame,
    build_feed_setup_frame,
    build_feed_subscription_frame,
    build_keepalive_frame,
    build_setup_frame,
    evaluate_stream_result,
    parse_compact_feed_data,
)
from backend.market_data.tastytrade_market_data import (
    MARKET_DATA_QUOTE_TOKEN_GROUP,
    MarketDataOAuthClient,
)
from backend.risk.live_guard import LiveTradingBlockedError, assert_order_execution_allowed
from backend.risk.models import OrderIntent, RiskContext
from scripts import check_tastytrade_dxlink_stream as script

_NO_ENV = Path("/nonexistent/.env")
MD_CLIENT_ID = "mdcid-1234567890-abcdef"
MD_SECRET = "mdsecret-ZZZZ-9876543210-qwerty"
MD_REFRESH = "mdrefresh-AAAA-1111222233334444-token"
MD_ACCESS = "mdaccess-BBBB-5555666677778888-jwt"
QUOTE_TOKEN = "dxquotetoken-CCCC-0000111122223333-secret"
SECRETS = (MD_CLIENT_ID, MD_SECRET, MD_REFRESH, MD_ACCESS, QUOTE_TOKEN)
DXLINK_URL = "wss://tasty-openapi-ws.dxfeed.com/realtime"
NOW = datetime(2026, 10, 2, 15, 0, 0, tzinfo=timezone.utc)
CORE = ("TNA", "TZA", "IWM", "SPY", "QQQ")
CLOSE = object()


def _settings(**overrides) -> Settings:
    base = dict(
        trading_mode="sandbox",
        tastytrade_env="sandbox",
        live_trading_enabled=False,
        tastytrade_client_id="sandbox-cid-000000000000",
        tastytrade_client_secret="sandbox-secret-00000000000",
        tastytrade_refresh_token="sandbox-refresh-000000000000",
        tastytrade_market_data_client_id=MD_CLIENT_ID,
        tastytrade_market_data_client_secret=MD_SECRET,
        tastytrade_market_data_refresh_token=MD_REFRESH,
        tastytrade_market_data_scopes="read",
    )
    base.update(overrides)
    return Settings(**base)


def _config(**overrides):
    return validate_market_data_settings(_settings(**overrides))


# ---------------------------------------------------------------------------
# Fakes: clock, WebSocket server, token provider
# ---------------------------------------------------------------------------


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


class FakeConnection:
    """Pre-scripted DXLink server. Each item: (delay_seconds, payload | CLOSE)."""

    def __init__(self, clock: FakeClock, incoming: list) -> None:
        self.clock = clock
        self.incoming = list(incoming)
        self.sent: list[dict] = []
        self.closed = False

    def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    def recv(self, timeout: float) -> str:
        if not self.incoming:
            self.clock.advance(timeout)
            raise TimeoutError
        delay, payload = self.incoming[0]
        if delay > timeout:
            self.clock.advance(timeout)
            self.incoming[0] = (delay - timeout, payload)
            raise TimeoutError
        self.clock.advance(delay)
        self.incoming.pop(0)
        if payload is CLOSE:
            raise StreamConnectionClosed("closed")
        return payload if isinstance(payload, str) else json.dumps(payload)

    def close(self) -> None:
        self.closed = True

    def sent_types(self) -> list[str]:
        return [frame["type"] for frame in self.sent]


class FakeTokenProvider:
    IS_MARKET_DATA_ONLY = True

    def __init__(self, clock: FakeClock, *, url: str = DXLINK_URL, exc: Exception | None = None):
        self.clock = clock
        self.url = url
        self.exc = exc
        self.calls: list[bool] = []
        self.invalidations = 0

    def get_token(self, *, force_refresh: bool = False) -> QuoteToken:
        self.calls.append(force_refresh)
        if self.exc:
            raise self.exc
        return QuoteToken(
            token=QUOTE_TOKEN,
            dxlink_url=self.url,
            level="api",
            issued_at=NOW,
            expires_at=NOW + timedelta(hours=24),
            fetched_at=self.clock(),
        )

    def invalidate(self) -> None:
        self.invalidations += 1


def handshake(*, feed_config: bool = True, authorized: bool = True) -> list:
    messages = [
        (0, {"type": "SETUP", "channel": 0, "version": "1.0", "keepaliveTimeout": 60}),
        (0, {"type": "AUTH_STATE", "channel": 0, "state": "UNAUTHORIZED"}),
        (0, {"type": "AUTH_STATE", "channel": 0, "state": "AUTHORIZED" if authorized else "UNAUTHORIZED"}),
    ]
    if not authorized:
        return messages
    messages.append(
        (0, {"type": "CHANNEL_OPENED", "channel": 3, "service": "FEED", "parameters": {"contract": "AUTO"}})
    )
    if feed_config:
        messages.append(
            (0, {"type": "FEED_CONFIG", "channel": 3, "dataFormat": "COMPACT", "eventFields": DEFAULT_EVENT_FIELDS})
        )
    return messages


def quote_frame(symbols=CORE, bid=10.0, ask=10.1) -> dict:
    flat: list = []
    for i, symbol in enumerate(symbols):
        flat += ["Quote", symbol, bid + i, ask + i, 100, 200]
    return {"type": "FEED_DATA", "channel": 3, "data": ["Quote", flat]}


def trade_frame(symbols=CORE + ("VIX",)) -> dict:
    flat: list = []
    for i, symbol in enumerate(symbols):
        flat += ["Trade", symbol, 20.0 + i, 1_000_000 + i, 100]
    return {"type": "FEED_DATA", "channel": 3, "data": ["Trade", flat]}


def summary_frame(symbols=CORE + ("VIX",)) -> dict:
    flat: list = []
    for i, symbol in enumerate(symbols):
        flat += ["Summary", symbol, 19.0 + i, 21.0 + i, 18.5 + i, 19.5 + i]
    return {"type": "FEED_DATA", "channel": 3, "data": ["Summary", flat]}


def steady_stream(seconds: float = 4.0, every: float = 0.2, gap_at: float | None = None, gap: float = 0.0) -> list:
    messages: list = [(0.05, summary_frame()), (0.05, trade_frame())]
    elapsed = 0.1
    while elapsed < seconds:
        delay = every
        if gap_at is not None and abs(elapsed - gap_at) < every / 2:
            delay = gap
        messages.append((delay, quote_frame()))
        elapsed += delay
    return messages


def make_client(clock, connections, *, provider=None, symbols=DEFAULT_STREAM_SYMBOLS, **kwargs):
    queue = list(connections)
    urls: list[str] = []

    def factory(url, _timeout):
        urls.append(url)
        if not queue:
            raise OSError("no more connections")
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    client = DXLinkStreamClient(
        provider or FakeTokenProvider(clock),
        symbols=symbols,
        connect_factory=factory,
        clock=clock,
        wall_clock=lambda: NOW,
        sleep=clock.sleep,
        **kwargs,
    )
    client.factory_urls = urls  # type: ignore[attr-defined]
    return client


# ---------------------------------------------------------------------------
# Frame generation
# ---------------------------------------------------------------------------


def test_setup_frame():
    frame = build_setup_frame()
    assert frame["type"] == "SETUP"
    assert frame["channel"] == 0
    assert frame["keepaliveTimeout"] == 60
    assert frame["acceptKeepaliveTimeout"] == 60
    assert frame["version"]


def test_auth_frame():
    assert build_auth_frame(QUOTE_TOKEN) == {"type": "AUTH", "channel": 0, "token": QUOTE_TOKEN}
    with pytest.raises(ValueError):
        build_auth_frame("")


def test_channel_request_and_keepalive_frames():
    assert build_channel_request_frame() == {
        "type": "CHANNEL_REQUEST",
        "channel": 3,
        "service": "FEED",
        "parameters": {"contract": "AUTO"},
    }
    assert build_keepalive_frame() == {"type": "KEEPALIVE", "channel": 0}


def test_feed_setup_frame_uses_compact():
    frame = build_feed_setup_frame()
    assert frame["type"] == "FEED_SETUP"
    assert frame["channel"] == 3
    assert frame["acceptDataFormat"] == "COMPACT"
    assert frame["acceptAggregationPeriod"] == 0.1
    assert set(frame["acceptEventFields"]) == {"Quote", "Trade", "Summary"}
    assert frame["acceptEventFields"]["Quote"][:4] == ["eventType", "eventSymbol", "bidPrice", "askPrice"]
    assert "prevDayClosePrice" in frame["acceptEventFields"]["Summary"]


def test_feed_subscription_frame():
    frame = build_feed_subscription_frame(["TNA", "VIX"])
    assert frame["type"] == "FEED_SUBSCRIPTION"
    assert frame["channel"] == 3
    assert frame["reset"] is True
    assert frame["add"] == [
        {"type": "Quote", "symbol": "TNA"},
        {"type": "Trade", "symbol": "TNA"},
        {"type": "Summary", "symbol": "TNA"},
        {"type": "Quote", "symbol": "VIX"},
        {"type": "Trade", "symbol": "VIX"},
        {"type": "Summary", "symbol": "VIX"},
    ]
    with pytest.raises(ValueError):
        build_feed_subscription_frame([])


# ---------------------------------------------------------------------------
# COMPACT parsing
# ---------------------------------------------------------------------------


def test_parse_compact_quote():
    data = ["Quote", ["Quote", "TNA", 40.1, 40.12, 300, 500, "Quote", "TZA", 15.5, 15.52, 100, 200]]
    events = parse_compact_feed_data(data, DEFAULT_EVENT_FIELDS)
    assert events == [
        {"eventType": "Quote", "eventSymbol": "TNA", "bidPrice": 40.1, "askPrice": 40.12, "bidSize": 300, "askSize": 500},
        {"eventType": "Quote", "eventSymbol": "TZA", "bidPrice": 15.5, "askPrice": 15.52, "bidSize": 100, "askSize": 200},
    ]


def test_parse_compact_trade():
    events = parse_compact_feed_data(["Trade", ["Trade", "SPY", 559.36, 13743299, 100.0]], DEFAULT_EVENT_FIELDS)
    assert events == [
        {"eventType": "Trade", "eventSymbol": "SPY", "price": 559.36, "dayVolume": 13743299, "size": 100.0}
    ]


def test_parse_compact_summary():
    events = parse_compact_feed_data(
        ["Summary", ["Summary", "IWM", 220.0, 222.5, 219.1, 221.0]], DEFAULT_EVENT_FIELDS
    )
    assert events == [
        {
            "eventType": "Summary",
            "eventSymbol": "IWM",
            "dayOpenPrice": 220.0,
            "dayHighPrice": 222.5,
            "dayLowPrice": 219.1,
            "prevDayClosePrice": 221.0,
        }
    ]


def test_parse_compact_mixed_nested_full_and_partial():
    mixed = ["Quote", ["Quote", "QQQ", 1, 2, 3, 4], "Trade", ["Trade", "QQQ", 5, 6, 7], "Greeks", [1, 2]]
    assert [e["eventType"] for e in parse_compact_feed_data(mixed, DEFAULT_EVENT_FIELDS)] == ["Quote", "Trade"]
    nested = [["Quote", ["Quote", "SPY", 1, 2, 3, 4]], ["Trade", ["Trade", "SPY", 5, 6, 7]]]
    assert len(parse_compact_feed_data(nested, DEFAULT_EVENT_FIELDS)) == 2
    full = [{"eventType": "Quote", "eventSymbol": "SPY", "bidPrice": 1}]
    assert parse_compact_feed_data(full, DEFAULT_EVENT_FIELDS) == full
    partial = ["Quote", ["Quote", "SPY", 1, 2, 3, 4, "Quote", "TNA", 1]]
    assert len(parse_compact_feed_data(partial, DEFAULT_EVENT_FIELDS)) == 1
    assert parse_compact_feed_data(None, DEFAULT_EVENT_FIELDS) == []


def test_parse_uses_server_field_order():
    fields = {"Quote": ["eventSymbol", "askPrice", "bidPrice", "eventType"]}
    events = parse_compact_feed_data(["Quote", ["SPY", 2.0, 1.0, "Quote"]], fields)
    assert events[0]["bidPrice"] == 1.0 and events[0]["askPrice"] == 2.0


def test_nan_values_become_none():
    state = StreamState(["VIX"])
    state.apply_event(
        {"eventType": "Quote", "eventSymbol": "VIX", "bidPrice": "NaN", "askPrice": None, "bidSize": "NaN", "askSize": 0},
        received_at=1.0,
    )
    assert state["VIX"].bid is None and state["VIX"].ask is None
    assert state["VIX"].mid is None


# ---------------------------------------------------------------------------
# State, freshness, VIX
# ---------------------------------------------------------------------------


def _quoted(symbol="TNA", at=100.0, bid=10.0, ask=10.1) -> SymbolStreamState:
    sym = SymbolStreamState(symbol)
    sym.apply({"eventType": "Quote", "bidPrice": bid, "askPrice": ask}, received_at=at, wall_time=NOW)
    return sym


def test_quote_freshness_calculation():
    sym = _quoted(at=100.0)
    assert sym.quote_age_seconds(100.4) == pytest.approx(0.4)
    assert sym.mid == pytest.approx(10.05)
    assert sym.is_stale(100.4, 1.0) is False
    assert sym.usable_for_trading(100.4, 1.0) == (True, "ok")


def test_stale_above_one_second():
    sym = _quoted(at=100.0)
    assert sym.is_stale(101.0, 1.0) is False
    assert sym.is_stale(101.001, 1.0) is True
    assert sym.usable_for_trading(101.5, 1.0) == (False, "stale_quote")


def test_default_stream_max_age_is_one_second(monkeypatch):
    assert sm.DEFAULT_STREAM_MAX_QUOTE_AGE_SECONDS == 1.0
    assert Settings().stream_max_quote_age_seconds == 1.0
    monkeypatch.setenv("STREAM_MAX_QUOTE_AGE_SECONDS", "0.5")
    assert load_settings(env_path=_NO_ENV).stream_max_quote_age_seconds == 0.5


def test_trade_and_summary_do_not_refresh_quote_freshness():
    sym = _quoted(at=100.0)
    sym.apply({"eventType": "Trade", "price": 10.05, "dayVolume": 5, "size": 1}, received_at=105.0, wall_time=NOW)
    sym.apply({"eventType": "Summary", "dayOpenPrice": 9.0, "prevDayClosePrice": "NaN"}, received_at=105.0, wall_time=NOW)
    assert sym.quote_age_seconds(105.0) == pytest.approx(5.0)
    assert sym.is_stale(105.0, 1.0) is True
    assert sym.last_price == 10.05 and sym.day_open == 9.0 and sym.prev_close is None
    assert sym.last_event_received_at == 105.0
    assert sym.event_types_seen == {"Quote", "Trade", "Summary"}


@pytest.mark.parametrize(
    "bid, ask, reason",
    [(None, 10.0, "missing_bid_ask"), (10.2, 10.1, "crossed_market")],
)
def test_unusable_quotes(bid, ask, reason):
    assert _quoted(bid=bid, ask=ask).usable_for_trading(100.1, 1.0) == (False, reason)


def test_vix_allowed_as_diagnostic_without_bid_ask():
    state = StreamState(["TNA", "VIX"])
    state.apply_event({"eventType": "Quote", "eventSymbol": "TNA", "bidPrice": 1, "askPrice": 1.01}, received_at=10.0)
    state.apply_event({"eventType": "Trade", "eventSymbol": "VIX", "price": 16.2, "dayVolume": 0, "size": 0}, received_at=10.0)
    vix = state["VIX"]
    assert vix.diagnostic_only is True
    assert vix.has_bid_ask is False
    assert vix.usable_for_trading(10.1, 1.0) == (False, "volatility_diagnostic_only")
    assert vix.quote_age_seconds(10.5) == pytest.approx(0.5)
    verdict = evaluate_stream_result(state, now=10.2, max_age_seconds=1.0)
    assert verdict.passed is True
    assert any("VIX" in w and "diagnostic" in w for w in verdict.warnings)


def test_vix_missing_is_only_a_warning():
    state = StreamState(["TNA", "VIX"])
    state.apply_event({"eventType": "Quote", "eventSymbol": "TNA", "bidPrice": 1, "askPrice": 1.01}, received_at=10.0)
    verdict = evaluate_stream_result(state, now=10.2, max_age_seconds=1.0)
    assert verdict.passed is True
    assert any("VIX: no events" in w for w in verdict.warnings)


def test_age_samples_max_avg_and_stayed_fresh():
    sym = _quoted(at=100.0)
    for now in (100.2, 100.6, 101.4):
        sym.record_age_sample(now, 1.0)
    assert sym.age_samples == 3
    assert sym.age_max == pytest.approx(1.4)
    assert sym.avg_age_seconds == pytest.approx((0.2 + 0.6 + 1.4) / 3)
    assert sym.fresh_samples == 2
    assert sym.stayed_fresh(1.0) is False
    assert sym.stayed_fresh(2.0) is True


def test_verdict_failures():
    empty = StreamState(CORE)
    assert evaluate_stream_result(empty, now=1.0, max_age_seconds=1.0).no_data is True

    state = StreamState(["TNA", "TZA"])
    state.apply_event({"eventType": "Quote", "eventSymbol": "TNA", "bidPrice": 1, "askPrice": 1.01}, received_at=10.0)
    verdict = evaluate_stream_result(state, now=12.0, max_age_seconds=1.0)
    assert verdict.passed is False
    assert any("TZA: no Quote updates" in f for f in verdict.failures)
    assert any("TNA: stale" in f for f in verdict.failures)


def test_verdict_require_all_fresh():
    state = StreamState(["TNA"])
    state.apply_event({"eventType": "Quote", "eventSymbol": "TNA", "bidPrice": 1, "askPrice": 1.01}, received_at=10.0)
    state.sample(11.5, 1.0)
    state.apply_event({"eventType": "Quote", "eventSymbol": "TNA", "bidPrice": 1, "askPrice": 1.01}, received_at=11.6)
    relaxed = evaluate_stream_result(state, now=11.7, max_age_seconds=1.0)
    assert relaxed.passed is True and relaxed.warnings
    strict = evaluate_stream_result(state, now=11.7, max_age_seconds=1.0, require_all_fresh=True)
    assert strict.passed is False


# ---------------------------------------------------------------------------
# DXLink client protocol
# ---------------------------------------------------------------------------


def test_handshake_sends_frames_in_order_and_streams_events():
    clock = FakeClock()
    conn = FakeConnection(clock, handshake() + steady_stream(seconds=3.5))
    client = make_client(clock, [conn], keepalive_interval=1.0)
    client.connect()
    assert conn.sent_types() == ["SETUP", "AUTH", "CHANNEL_REQUEST", "FEED_SETUP", "FEED_SUBSCRIPTION"]
    assert conn.sent[1]["token"] == QUOTE_TOKEN
    assert conn.sent[3]["acceptDataFormat"] == "COMPACT"
    assert len(conn.sent[4]["add"]) == 18
    assert client.factory_urls == [DXLINK_URL]

    summary = client.stream(3.0, max_age_seconds=1.0)
    assert summary.authorized and summary.channel_opened and summary.subscribed
    assert summary.data_format == "COMPACT"
    assert summary.keepalives_sent >= 2
    assert "KEEPALIVE" in conn.sent_types()
    for symbol in CORE:
        sym = client.state[symbol]
        assert sym.quote_updates >= 10
        assert sym.trade_updates == 1 and sym.summary_updates == 1
        assert sym.stayed_fresh(1.0)
        assert not sym.is_stale(client.now(), 1.0)
        assert sym.is_realtime is True
    assert client.state["VIX"].trade_updates == 1
    client.disconnect()
    assert conn.closed


def test_feed_config_missing_falls_back_to_requested_fields():
    clock = FakeClock()
    conn = FakeConnection(clock, handshake(feed_config=False) + steady_stream(seconds=1.0))
    client = make_client(clock, [conn], handshake_timeout=0.5)
    client.connect()
    client.stream(0.5)
    assert any("FEED_CONFIG not received" in w for w in client.summary.warnings)
    assert client.state["SPY"].quote_updates > 0


def test_auth_rejected_retries_once_with_new_token_then_fails():
    clock = FakeClock()
    provider = FakeTokenProvider(clock)
    conns = [FakeConnection(clock, handshake(authorized=False)), FakeConnection(clock, handshake(authorized=False))]
    client = make_client(clock, conns, provider=provider)
    with pytest.raises(DXLinkError) as info:
        client.connect()
    assert info.value.reason == "auth_failed"
    assert info.value.step == "auth"
    assert provider.calls == [False, True]
    assert provider.invalidations == 1


def test_dxlink_error_message_is_protocol_error():
    clock = FakeClock()
    conn = FakeConnection(
        clock,
        [(0, {"type": "ERROR", "channel": 0, "error": "INVALID_MESSAGE", "message": "bad setup"})],
    )
    with pytest.raises(DXLinkError) as info:
        make_client(clock, [conn]).connect()
    assert info.value.reason == "protocol_error"
    assert info.value.provider_message == "bad setup"


def test_handshake_timeout():
    clock = FakeClock()
    with pytest.raises(DXLinkError) as info:
        make_client(clock, [FakeConnection(clock, [])], handshake_timeout=2.0).connect()
    assert info.value.reason == "timeout"
    assert info.value.step == "setup"


def test_connection_failure_classified():
    clock = FakeClock()
    with pytest.raises(DXLinkError) as info:
        make_client(clock, [OSError("refused")]).connect()
    assert info.value.reason == "connection_failed"
    assert info.value.step == "connect"


def test_reconnect_uses_backoff_and_cached_token():
    clock = FakeClock()
    provider = FakeTokenProvider(clock)
    first = FakeConnection(clock, handshake() + [(0.2, quote_frame()), (0.1, CLOSE)])
    second = FakeConnection(clock, handshake() + steady_stream(seconds=6.0))
    client = make_client(clock, [first, second], provider=provider)
    client.stream(4.0)
    assert client.summary.reconnects == 1
    assert clock.sleeps == [1.0]
    assert provider.calls == [False, False]
    assert client.state["TNA"].quote_updates > 1


def test_reconnect_is_bounded_and_never_busy_loops():
    clock = FakeClock()
    conns = [FakeConnection(clock, handshake() + [(0.1, CLOSE)]) for _ in range(3)]
    client = make_client(clock, conns, max_reconnects=2)
    with pytest.raises(DXLinkError) as info:
        client.stream(60.0)
    assert info.value.reason == "connection_lost"
    assert client.summary.reconnects == 2
    assert clock.sleeps == [1.0, 2.0]
    assert len(client.factory_urls) == 3


def test_mid_stream_deauthorization_stops():
    clock = FakeClock()
    conn = FakeConnection(clock, handshake() + [(0.1, {"type": "AUTH_STATE", "channel": 0, "state": "UNAUTHORIZED"})])
    client = make_client(clock, [conn])
    with pytest.raises(DXLinkError) as info:
        client.stream(5.0)
    assert info.value.reason == "auth_lost"


def test_websockets_adapter_maps_close_timeout_and_bytes():
    from websockets.exceptions import ConnectionClosedOK

    ws = MagicMock()
    ws.recv.side_effect = [b'{"type":"KEEPALIVE"}', TimeoutError(), ConnectionClosedOK(None, None)]
    conn = dx._WebsocketsConnection(ws)
    assert conn.recv(0.1) == '{"type":"KEEPALIVE"}'
    with pytest.raises(TimeoutError):
        conn.recv(0.1)
    with pytest.raises(StreamConnectionClosed):
        conn.recv(0.1)
    conn.close()
    ws.close.assert_called_once()


def test_websockets_connect_refuses_bad_url_before_opening_socket(monkeypatch):
    import websockets.sync.client as ws_client

    opened = MagicMock()
    monkeypatch.setattr(ws_client, "connect", opened)
    with pytest.raises(MarketDataUrlBlockedError):
        dx.websockets_connect("wss://api.tastyworks.com/stream", 1.0)
    opened.assert_not_called()


def test_non_dxfeed_url_refused_before_connecting():
    clock = FakeClock()
    provider = FakeTokenProvider(clock, url="wss://evil.example.com/realtime")
    client = make_client(clock, [FakeConnection(clock, handshake())], provider=provider)
    with pytest.raises(DXLinkError) as info:
        client.connect()
    assert info.value.reason == "dxlink_url_blocked"
    assert client.factory_urls == []


# ---------------------------------------------------------------------------
# Quote token provider (mocked production REST)
# ---------------------------------------------------------------------------


class FakeREST:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.oauth: list[httpx.Response] = []
        self.tokens: list[httpx.Response] = []

    @staticmethod
    def token_response(**overrides) -> httpx.Response:
        data = {
            "token": QUOTE_TOKEN,
            "dxlink-url": DXLINK_URL,
            "level": "api",
            "issued-at": "2026-10-02T14:30:00.000Z",
            "expires-at": "2026-10-03T14:30:00.000Z",
        }
        data.update(overrides)
        return httpx.Response(200, json={"data": data, "context": "/api-quote-tokens"})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/oauth/token":
            if self.oauth:
                return self.oauth.pop(0)
            return httpx.Response(200, json={"access_token": MD_ACCESS, "expires_in": 900})
        if request.url.path == "/api-quote-tokens":
            return self.tokens.pop(0) if self.tokens else self.token_response()
        return httpx.Response(599, json={"error": "unexpected"})

    def count(self, path: str) -> int:
        return sum(1 for r in self.requests if r.url.path == path)


@pytest.fixture
def rest(monkeypatch):
    fake = FakeREST()
    real_client = httpx.Client

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(fake)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", factory)
    return fake


def _provider(clock=None, wall=None, **kwargs) -> DXLinkQuoteTokenProvider:
    config = _config()
    auth = MarketDataOAuthClient(config, refresh_token_persister=lambda *_: True)
    return DXLinkQuoteTokenProvider(
        config,
        auth=auth,
        clock=clock or FakeClock(),
        wall_clock=wall or (lambda: NOW),
        **kwargs,
    )


def test_quote_token_fetch_parses_and_uses_production_rest(rest):
    token = _provider().get_token()
    assert token.token == QUOTE_TOKEN
    assert token.dxlink_url == DXLINK_URL
    assert token.level == "api"
    assert token.expires_at == datetime(2026, 10, 3, 14, 30, tzinfo=timezone.utc)
    request = [r for r in rest.requests if r.url.path == "/api-quote-tokens"][0]
    assert request.method == "GET"
    assert request.url.host == "api.tastyworks.com"
    assert request.headers["Authorization"] == f"Bearer {MD_ACCESS}"
    assert QUOTE_TOKEN not in repr(token)
    assert QUOTE_TOKEN not in str(token.safe_summary())


def test_quote_token_is_cached_until_near_expiry(rest):
    wall = {"now": NOW}
    provider = _provider(wall=lambda: wall["now"])
    provider.get_token()
    provider.get_token()
    assert rest.count("/api-quote-tokens") == 1
    wall["now"] = datetime(2026, 10, 3, 14, 26, tzinfo=timezone.utc)
    provider.get_token()
    assert rest.count("/api-quote-tokens") == 2
    provider.get_token(force_refresh=True)
    assert rest.count("/api-quote-tokens") == 3


def test_quote_token_without_expiry_uses_safe_ttl(rest):
    rest.tokens.append(FakeREST.token_response(**{"expires-at": None}))
    clock = FakeClock()
    provider = _provider(clock=clock)
    provider.get_token()
    clock.advance(dx.DEFAULT_QUOTE_TOKEN_TTL_SECONDS - 600)
    provider.get_token()
    assert rest.count("/api-quote-tokens") == 1
    clock.advance(600)
    provider.get_token()
    assert rest.count("/api-quote-tokens") == 2


def test_quote_token_429_rate_limited_and_stops(rest, _isolate_market_data_rate_limiter):
    rest.tokens.append(httpx.Response(429, headers={"Retry-After": "30"}, json={}))
    provider = _provider()
    with pytest.raises(DXLinkError) as info:
        provider.get_token()
    assert info.value.reason == "rate_limited"
    assert info.value.rate_limit.cooldown_seconds == 30
    assert info.value.rate_limit.endpoint_group == MARKET_DATA_QUOTE_TOKEN_GROUP
    with pytest.raises(DXLinkError) as again:
        provider.get_token()
    assert again.value.rate_limit.cooldown_already_active is True
    assert rest.count("/api-quote-tokens") == 1


def test_quote_token_403_not_permitted(rest):
    rest.tokens.append(httpx.Response(403, json={"error": {"code": "forbidden", "message": "no access"}}))
    with pytest.raises(DXLinkError) as info:
        _provider().get_token()
    assert info.value.reason == "quote_token_not_permitted"


def test_quote_token_401_refreshes_oauth_once(rest):
    rest.tokens.append(httpx.Response(401, json={"error": "expired"}))
    provider = _provider()
    assert provider.get_token().token == QUOTE_TOKEN
    assert rest.count("/oauth/token") == 2
    assert rest.count("/api-quote-tokens") == 2


@pytest.mark.parametrize(
    "url",
    ["wss://evil.example.com/realtime", "ws://tasty-openapi-ws.dxfeed.com/realtime", "https://api.tastyworks.com"],
)
def test_quote_token_with_unexpected_streamer_url_is_refused(rest, url):
    rest.tokens.append(FakeREST.token_response(**{"dxlink-url": url}))
    with pytest.raises(DXLinkError) as info:
        _provider().get_token()
    assert info.value.reason == "dxlink_url_blocked"


def test_quote_token_endpoint_allowed_only_as_get():
    assert_market_data_request("GET", f"{PRODUCTION_BASE_URL}/api-quote-tokens")
    with pytest.raises(MarketDataUrlBlockedError):
        assert_market_data_request("POST", f"{PRODUCTION_BASE_URL}/api-quote-tokens")
    assert_dxlink_stream_url(DXLINK_URL)
    for bad in ("ws://tasty-openapi-ws.dxfeed.com/realtime", "wss://dxfeed.com.evil.io/x", "wss://api.tastyworks.com"):
        with pytest.raises(MarketDataUrlBlockedError):
            assert_dxlink_stream_url(bad)


def test_provider_refuses_non_read_only_config():
    from backend.market_data.config import MarketDataConfig

    config = MarketDataConfig(
        provider="tastytrade",
        env="production",
        read_only=False,
        scopes="read",
        max_quote_age_seconds=60,
        client_id=MD_CLIENT_ID,
        client_secret=MD_SECRET,
        refresh_token=MD_REFRESH,
    )
    with pytest.raises(DXLinkError):
        DXLinkQuoteTokenProvider(config, auth=MagicMock())


# ---------------------------------------------------------------------------
# Safety guards
# ---------------------------------------------------------------------------

_FORBIDDEN = ("order", "submit", "cancel", "execute", "trade", "position", "account", "balance", "close")


@pytest.mark.parametrize("cls", [DXLinkStreamClient, DXLinkQuoteTokenProvider])
def test_stream_classes_have_no_order_methods(cls):
    public = [n for n in dir(cls) if not n.startswith("_")]
    assert [n for n in public if any(w in n.lower() for w in _FORBIDDEN)] == []
    assert cls.IS_MARKET_DATA_ONLY is True


def test_stream_modules_have_no_order_payloads_or_execution_imports():
    for module in (dx, sm):
        source = inspect.getsource(module)
        for token in ('"/orders', "order-type", "time-in-force", "dry-run", "/positions", "/balances"):
            assert token not in source
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")):
                for bad in ("execution", "order_executor", "tastytrade_sandbox", "sandbox_auth", "bot_worker"):
                    assert bad not in stripped, stripped


def test_execution_router_rejects_dxlink_client_and_provider():
    clock = FakeClock()
    with pytest.raises(MarketDataCredentialMisuseError):
        ExecutionRouter(sandbox_adapter=make_client(clock, []))
    with pytest.raises(MarketDataCredentialMisuseError):
        ExecutionRouter(sandbox_adapter=_provider())


def _context(**overrides) -> RiskContext:
    base = dict(
        trading_mode="sandbox",
        live_trading_enabled=False,
        tastytrade_env="sandbox",
        emergency_halt=False,
        buying_power=10000.0,
        current_price=50.0,
        open_positions_count=0,
        pending_orders_count=0,
        trades_today_count=0,
        daily_pnl=0.0,
        market_data_healthy=True,
        max_trades_per_day=5,
        max_daily_loss_usd=500.0,
        buying_power_reserve_pct=0.1,
        max_position_pct_of_buying_power=0.5,
    )
    base.update(overrides)
    return RiskContext(**base)


def test_execution_router_route_time_guard_blocks_dxlink_client():
    router = ExecutionRouter(sandbox_adapter=MagicMock())
    router._sandbox = make_client(FakeClock(), [])
    intent = OrderIntent(symbol="TNA", side="buy", quantity=1, trading_mode="sandbox")
    result = router.route(intent, _context())
    assert result.success is False and result.status == "rejected"


def test_production_execution_still_blocked():
    adapter = MagicMock()
    router = ExecutionRouter(sandbox_adapter=adapter)
    intent = OrderIntent(symbol="TNA", side="buy", quantity=1, trading_mode="sandbox")
    for ctx in (_context(live_trading_enabled=True), _context(trading_mode="live"), _context(tastytrade_env="production")):
        assert router.route(intent, ctx).success is False
    adapter.execute_order.assert_not_called()
    with pytest.raises(LiveTradingBlockedError):
        assert_order_execution_allowed(Settings(live_trading_enabled=True))


def test_sandbox_execution_rejects_market_data_credentials():
    provider = _provider()
    with pytest.raises(SandboxAuthError):
        TastytradeSandboxAdapter(_settings(), auth=provider.auth)
    with pytest.raises(ConfigurationError):
        SandboxBotWorker(_settings(), make_client(FakeClock(), []))


@pytest.mark.parametrize("key, value", [("LIVE_TRADING_ENABLED", "true"), ("TRADING_MODE", "live")])
def test_live_settings_still_fail(monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    with pytest.raises(ConfigurationError):
        load_settings(env_path=_NO_ENV)


def test_sandbox_worker_does_not_submit_by_default():
    adapter = MagicMock()
    adapter._auth = MagicMock(is_authenticated=True)
    adapter.get_accounts.return_value = [{"account-number": "5WM30541"}]
    adapter.get_balance.return_value = {"account_number": "5WM30541", "buying_power": 0.0, "cash_balance": 1e5}
    adapter.get_positions.return_value = []
    adapter.list_live_orders.return_value = []
    adapter.dry_run_equity_order.return_value = {"data": {}}
    result = SandboxBotWorker(_settings(), adapter).run_cycle(signal="bullish")
    assert result.submitted is False
    adapter.submit_equity_order.assert_not_called()
    adapter.execute_order.assert_not_called()


def test_public_trading_routes_remain_blocked(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "sandbox")
    monkeypatch.setenv("TASTYTRADE_ENV", "sandbox")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    from backend.app_factory import create_app
    from backend.config.settings import reset_settings_cache

    reset_settings_cache()
    client = create_app(skip_db_init=True, defer_heavy_services=True).test_client()
    assert client.post("/trade/execute", json={"symbol": "TNA"}).status_code == 423
    assert client.post("/bot/start").status_code == 423
    assert client.post("/trade/close/1").status_code == 423


# ---------------------------------------------------------------------------
# Diagnostic script
# ---------------------------------------------------------------------------


def _run(capsys, connections, *, provider_exc=None, settings=None, symbols=DEFAULT_STREAM_SYMBOLS, **kwargs):
    clock = FakeClock()
    provider = FakeTokenProvider(clock, exc=provider_exc)
    code = script.run_stream_check(
        settings or _settings(),
        symbols=symbols,
        token_provider_factory=lambda _cfg: provider,
        stream_factory=lambda prov, syms: make_client(clock, connections(clock), provider=prov, symbols=syms),
        **kwargs,
    )
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def _assert_no_secrets(text: str) -> None:
    for secret in SECRETS:
        assert secret not in text


def test_script_passes_with_fresh_stream(capsys):
    code, out = _run(capsys, lambda c: [FakeConnection(c, handshake() + steady_stream(seconds=4.0))], duration_seconds=3.0)
    assert code == 0, out
    assert script.PASSED in out
    assert "dxlink_handshake: ok" in out
    assert "TNA: quote=" in out
    assert "[VIX] (volatility diagnostic only)" in out
    assert "max_quote_age_seconds:" in out and "avg_quote_age_seconds:" in out
    assert "stayed_fresh: true" in out
    assert "VIX: no bid/ask; allowed as volatility diagnostic only" in out
    assert "production_order_execution: blocked" in out
    assert "quote_token: dxqu" in out
    _assert_no_secrets(out)


def test_script_stale_stream_exits_4(capsys):
    code, out = _run(capsys, lambda c: [FakeConnection(c, handshake() + steady_stream(seconds=1.0))], duration_seconds=3.0)
    assert code == script.STALE_EXIT_CODE
    assert "stale at end of window" in out
    assert script.FAILED in out


def test_script_require_all_fresh(capsys):
    def conns(c):
        return [FakeConnection(c, handshake() + steady_stream(seconds=5.0, gap_at=1.5, gap=1.5))]

    code, out = _run(capsys, conns, duration_seconds=4.0)
    assert code == 0, out
    assert "briefly stale" in out
    code, out = _run(capsys, conns, duration_seconds=4.0, require_all_fresh=True)
    assert code == script.STALE_EXIT_CODE
    assert "exceeded max age" in out


def test_script_no_data_is_subscription_failure(capsys):
    code, out = _run(capsys, lambda c: [FakeConnection(c, handshake())], duration_seconds=2.0)
    assert code == 1
    assert "error_step: subscription" in out


def test_script_auth_failure_exits_1(capsys):
    code, out = _run(
        capsys,
        lambda c: [FakeConnection(c, handshake(authorized=False)), FakeConnection(c, handshake(authorized=False))],
        duration_seconds=2.0,
    )
    assert code == 1
    assert "failure_reason: auth_failed" in out
    _assert_no_secrets(out)


def test_script_connection_failure_exits_1(capsys):
    code, out = _run(capsys, lambda c: [OSError("refused")], duration_seconds=2.0)
    assert code == 1
    assert "failure_reason: connection_failed" in out


def test_script_quote_token_rate_limited_exits_3(capsys):
    from backend.adapters.broker.sandbox_rate_limiter import RateLimitInfo

    exc = DXLinkError(
        "Quote token request rate limited (429).",
        step="quote_token",
        reason="rate_limited",
        status_code=429,
        rate_limit=RateLimitInfo(cooldown_seconds=60, endpoint_group=MARKET_DATA_QUOTE_TOKEN_GROUP, step="quote_token"),
    )
    code, out = _run(capsys, lambda c: [], provider_exc=exc, duration_seconds=2.0)
    assert code == 3
    assert "market-data rate limit" in out
    assert script.FAILED in out


def test_script_config_invalid_exits_2(capsys):
    code, out = _run(capsys, lambda c: [], settings=_settings(tastytrade_market_data_scopes="read trade"))
    assert code == script.CONFIG_EXIT_CODE
    assert "non-read-only" in out
    _assert_no_secrets(out)


def test_script_execution_not_sandbox_exits_2(capsys):
    code, out = _run(capsys, lambda c: [], settings=_settings(tastytrade_env="production"))
    assert code == script.CONFIG_EXIT_CODE


@pytest.mark.parametrize("kwargs", [{"duration_seconds": 0}, {"duration_seconds": 1000}, {"max_age_seconds": 0}])
def test_script_rejects_bad_arguments(capsys, kwargs):
    code, _ = _run(capsys, lambda c: [], **kwargs)
    assert code == script.CONFIG_EXIT_CODE


def test_script_main_parses_arguments(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(script, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(script, "load_settings", lambda **_: _settings())
    monkeypatch.setattr(script, "run_stream_check", lambda settings, **kw: captured.update(kw) or 0)
    code = script.main(
        ["--symbols", "tna,tza,vix", "--duration-seconds", "5", "--max-age-seconds", "0.5", "--no-vix", "--require-all-fresh"]
    )
    assert code == 0
    assert captured == {
        "symbols": ["TNA", "TZA"],
        "duration_seconds": 5.0,
        "max_age_seconds": 0.5,
        "require_all_fresh": True,
    }
    script.main([])
    assert captured["symbols"] == list(DEFAULT_STREAM_SYMBOLS)
    assert captured["max_age_seconds"] is None
