"""Checkpoint 2.11 — TNA / TZA Signal Engine v1 (signal generation only, no orders)."""

from __future__ import annotations

import importlib.util
import inspect
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import backend.signals.dxlink_signal_source as signal_source_mod
import backend.signals.models as signal_models_mod
import backend.signals.tna_tza_signal_engine as engine_mod
from backend.bot_worker.sandbox_worker import SandboxBotCycleResult, SandboxBotWorker
from backend.config.settings import ConfigurationError, Settings, load_settings
from backend.config.tastytrade_urls import PRODUCTION_BASE_URL, MarketDataUrlBlockedError, assert_market_data_request
from backend.execution.execution_router import ExecutionRouter
from backend.market_data.config import validate_market_data_settings
from backend.market_data.dxlink_stream import DXLinkStreamClient, QuoteToken, StreamConnectionClosed
from backend.market_data.stream_models import DEFAULT_EVENT_FIELDS
from backend.market_data.tastytrade_market_data import MarketDataError
from backend.risk.live_guard import LiveTradingBlockedError, assert_order_execution_allowed
from backend.risk.models import OrderIntent, RiskContext
from backend.signals import (
    MarketSnapshot,
    SignalDecision,
    SignalDirection,
    SignalEngineConfig,
    SignalReason,
    SignalScoreBreakdown,
    TnaTzaSignalEngine,
)
from backend.signals.dxlink_signal_source import build_pre_submit_check, collect_signal_from_dxlink
from backend.signals.models import SymbolQuote
from scripts import check_tna_tza_signal_engine as script

REPO_ROOT = Path(__file__).resolve().parents[1]
_NO_ENV = Path("/nonexistent/.env")
MD_CLIENT_ID = "mdcid-1234567890-abcdef"
MD_SECRET = "mdsecret-ZZZZ-9876543210-qwerty"
MD_REFRESH = "mdrefresh-AAAA-1111222233334444-token"
SANDBOX_SECRET = "sandbox-secret-00000000000"
SANDBOX_REFRESH = "sandbox-refresh-000000000000"
QUOTE_TOKEN = "dxquotetoken-CCCC-0000111122223333-secret"
SECRETS = (MD_CLIENT_ID, MD_SECRET, MD_REFRESH, SANDBOX_SECRET, SANDBOX_REFRESH, QUOTE_TOKEN)
DXLINK_URL = "wss://tasty-openapi-ws.dxfeed.com/realtime"
NOW = datetime(2026, 10, 2, 15, 0, 0, tzinfo=timezone.utc)
CLOSE = object()


def _settings(**overrides) -> Settings:
    base = dict(
        trading_mode="sandbox",
        tastytrade_env="sandbox",
        live_trading_enabled=False,
        tastytrade_client_id="sandbox-cid-000000000000",
        tastytrade_client_secret=SANDBOX_SECRET,
        tastytrade_refresh_token=SANDBOX_REFRESH,
        tastytrade_market_data_client_id=MD_CLIENT_ID,
        tastytrade_market_data_client_secret=MD_SECRET,
        tastytrade_market_data_refresh_token=MD_REFRESH,
        tastytrade_market_data_scopes="read",
    )
    base.update(overrides)
    return Settings(**base)


def _engine(**overrides) -> TnaTzaSignalEngine:
    return TnaTzaSignalEngine(SignalEngineConfig(**overrides), wall_clock=lambda: NOW)


# ---------------------------------------------------------------------------
# Snapshot fixtures
# ---------------------------------------------------------------------------


def q(symbol, mid, *, open_=None, prev=None, first=None, spread=0.02, age=0.2, updates=10) -> SymbolQuote:
    half = spread / 2
    return SymbolQuote(
        symbol=symbol,
        bid=round(mid - half, 4),
        ask=round(mid + half, 4),
        day_open=open_,
        prev_close=prev,
        first_mid=first,
        quote_age_seconds=age,
        quote_updates=updates,
    )


def vix(level, prev, *, age=5.0) -> SymbolQuote:
    return SymbolQuote(symbol="VIX", last=level, prev_close=prev, quote_age_seconds=age, quote_updates=3, diagnostic_only=True)


def snapshot(*quotes: SymbolQuote) -> MarketSnapshot:
    return MarketSnapshot(quotes={x.symbol: x for x in quotes}, created_at=NOW)


def bullish_quotes(**ages):
    return [
        q("TNA", 45.0, open_=43.0, prev=42.5, age=ages.get("TNA", 0.2)),
        q("TZA", 10.0, open_=10.5, prev=10.6, age=ages.get("TZA", 0.3)),
        q("IWM", 220.0, open_=218.0, prev=217.0, first=219.0, age=ages.get("IWM", 0.1)),
        q("SPY", 580.0, open_=575.0, prev=574.0, age=ages.get("SPY", 0.2)),
        q("QQQ", 500.0, open_=495.0, prev=494.0, age=ages.get("QQQ", 0.4)),
    ]


def bearish_quotes():
    return [
        q("TNA", 41.0, open_=43.0, prev=43.5),
        q("TZA", 11.0, open_=10.5, prev=10.4),
        q("IWM", 216.0, open_=218.0, prev=219.0, first=217.0),
        q("SPY", 570.0, open_=575.0, prev=576.0),
        q("QQQ", 490.0, open_=495.0, prev=496.0),
    ]


def choppy_quotes():
    # IWM above open but below previous close, flat momentum; SPY up, QQQ down.
    return [
        q("TNA", 43.0, open_=42.9, prev=43.4),
        q("TZA", 10.5, open_=10.48, prev=10.4),
        q("IWM", 218.0, open_=217.5, prev=218.6, first=218.0),
        q("SPY", 580.0, open_=575.0, prev=574.0),
        q("QQQ", 490.0, open_=495.0, prev=496.0),
    ]


def _example(label: str, decision: SignalDecision) -> None:
    data = decision.to_dict()
    print(f"\n=== example {label} ===")
    for key in (
        "decision",
        "selected_symbol",
        "confidence_score",
        "bullish_score",
        "bearish_score",
        "skip_reason",
        "freshness_gate_passed",
        "market_regime",
        "explanation",
    ):
        print(f"{key}: {data[key]}")
    for symbol, status in data["quote_freshness_by_symbol"].items():
        print(f"  {symbol}: age={status['age_seconds']} fresh={status['fresh']} {status['note']}")
    for warning in data["warnings"]:
        print(f"warning: {warning}")


# ---------------------------------------------------------------------------
# Models / config
# ---------------------------------------------------------------------------


def test_models_expose_required_types():
    assert {d.value for d in SignalDirection} == {"bullish", "bearish", "skip"}
    assert SignalReason.STALE_MARKET_DATA.value == "stale_market_data"
    assert SignalReason.MISSING_REQUIRED_QUOTE.value == "missing_required_quote"
    assert SignalReason.UNCLEAR_MARKET_DIRECTION.value == "unclear_market_direction"
    assert isinstance(SignalScoreBreakdown(), SignalScoreBreakdown)


def test_decision_contains_all_required_fields():
    data = _engine().decide(snapshot(*bullish_quotes())).to_dict()
    for key in (
        "decision",
        "selected_symbol",
        "confidence_score",
        "bullish_score",
        "bearish_score",
        "skip_reason",
        "quote_freshness_by_symbol",
        "market_regime",
        "explanation",
        "created_at",
    ):
        assert key in data
    json.dumps(data)


def test_default_config_is_safe():
    cfg = SignalEngineConfig()
    assert cfg.max_quote_age_seconds == 1.0
    assert cfg.entry_score_threshold == 70.0
    assert cfg.opposing_score_max == 40.0
    assert set(cfg.required_symbols) == {"TNA", "TZA", "IWM", "SPY", "QQQ"}
    assert "VIX" not in cfg.required_symbols


@pytest.mark.parametrize(
    "overrides",
    [
        {"entry_score_threshold": 50.0},
        {"opposing_score_max": 60.0},
        {"min_score_gap": 5.0},
        {"max_quote_age_seconds": 30.0},
        {"max_quote_age_seconds": 0.0},
        {"required_symbols": ("TNA", "TZA", "IWM")},
    ],
)
def test_unsafe_config_rejected(overrides):
    with pytest.raises(ConfigurationError):
        SignalEngineConfig(**overrides).validate()


def test_config_from_settings_and_env():
    cfg = SignalEngineConfig.from_settings(
        _settings(stream_max_quote_age_seconds=0.8),
        env={"SIGNAL_ENTRY_SCORE_THRESHOLD": "80", "SIGNAL_MAX_SPREAD_PCT": "0.2"},
    )
    assert cfg.max_quote_age_seconds == 0.8
    assert cfg.entry_score_threshold == 80.0
    assert cfg.max_tradable_spread_pct == 0.2
    with pytest.raises(ConfigurationError):
        SignalEngineConfig.from_settings(env={"SIGNAL_ENTRY_SCORE_THRESHOLD": "10"})
    with pytest.raises(ConfigurationError):
        SignalEngineConfig.from_settings(env={"SIGNAL_MIN_SCORE_GAP": "abc"})


def test_stream_max_quote_age_default_is_one_second():
    assert load_settings(env_path=_NO_ENV).stream_max_quote_age_seconds == 1.0


# ---------------------------------------------------------------------------
# Freshness gate
# ---------------------------------------------------------------------------


def test_missing_quote_skips():
    quotes = [x for x in bullish_quotes() if x.symbol != "QQQ"]
    decision = _engine().decide(snapshot(*quotes))
    assert decision.decision is SignalDirection.SKIP
    assert decision.skip_reason is SignalReason.MISSING_REQUIRED_QUOTE
    assert decision.selected_symbol is None
    assert decision.freshness_gate_passed is False
    assert decision.score_breakdown is None
    assert decision.quote_freshness_by_symbol["QQQ"].present is False
    assert decision.worker_signal == "none"


def test_quote_with_no_updates_counts_as_missing():
    quotes = bullish_quotes()
    quotes[0] = SymbolQuote(symbol="TNA")
    decision = _engine().decide(snapshot(*quotes))
    assert decision.skip_reason is SignalReason.MISSING_REQUIRED_QUOTE


def test_stale_quote_skips():
    decision = _engine().decide(snapshot(*bullish_quotes(TNA=1.4)))
    _example("SKIP (stale_market_data)", decision)
    assert decision.decision is SignalDirection.SKIP
    assert decision.skip_reason is SignalReason.STALE_MARKET_DATA
    assert decision.freshness_gate_passed is False
    assert decision.score_breakdown is None
    assert decision.bullish_score == 0 and decision.bearish_score == 0
    assert decision.quote_freshness_by_symbol["TNA"].fresh is False
    assert "TNA" in decision.explanation


@pytest.mark.parametrize("symbol", ["TNA", "TZA", "IWM", "SPY", "QQQ"])
def test_every_core_symbol_is_gated(symbol):
    decision = _engine().decide(snapshot(*bullish_quotes(**{symbol: 1.01})))
    assert decision.skip_reason is SignalReason.STALE_MARKET_DATA


def test_age_exactly_at_limit_is_fresh():
    decision = _engine().decide(snapshot(*bullish_quotes(TNA=1.0)))
    assert decision.freshness_gate_passed is True
    assert decision.decision is SignalDirection.BULLISH


def test_max_age_is_configurable():
    decision = _engine(max_quote_age_seconds=0.25).decide(snapshot(*bullish_quotes()))
    assert decision.skip_reason is SignalReason.STALE_MARKET_DATA  # QQQ age 0.4


def test_stale_vix_does_not_fail_gate():
    decision = _engine().decide(snapshot(*bullish_quotes(), vix(15.0, 15.5, age=120.0)))
    assert decision.freshness_gate_passed is True
    assert decision.decision is SignalDirection.BULLISH
    assert decision.quote_freshness_by_symbol["VIX"].required is False


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def test_fresh_bullish_snapshot_selects_tna():
    decision = _engine().decide(snapshot(*bullish_quotes(), vix(15.0, 15.5)))
    _example("BULLISH -> TNA", decision)
    assert decision.decision is SignalDirection.BULLISH
    assert decision.selected_symbol == "TNA"
    assert decision.bullish_score >= 70 and decision.bearish_score <= 40
    assert decision.confidence_score == decision.bullish_score
    assert decision.skip_reason is None
    assert decision.market_regime == "risk_on"
    assert decision.worker_signal == "bullish"
    names = {c.name for c in decision.score_breakdown.components}
    assert {"iwm_vs_day_open", "iwm_vs_prev_close", "iwm_momentum", "spy_confirmation", "qqq_confirmation"} <= names


def test_fresh_bearish_snapshot_selects_tza():
    decision = _engine().decide(snapshot(*bearish_quotes(), vix(18.0, 17.9)))
    _example("BEARISH -> TZA", decision)
    assert decision.decision is SignalDirection.BEARISH
    assert decision.selected_symbol == "TZA"
    assert decision.bearish_score >= 70 and decision.bullish_score <= 40
    assert decision.market_regime == "risk_off"
    assert decision.worker_signal == "bearish"


def test_mixed_choppy_snapshot_skips():
    decision = _engine().decide(snapshot(*choppy_quotes(), vix(16.0, 16.0)))
    _example("SKIP (mixed/choppy)", decision)
    assert decision.decision is SignalDirection.SKIP
    assert decision.skip_reason is SignalReason.UNCLEAR_MARKET_DIRECTION
    assert decision.freshness_gate_passed is True
    assert decision.confidence_score == 0
    assert decision.worker_signal == "none"


@pytest.mark.parametrize("symbol", ["TNA", "TZA"])
def test_wide_tna_tza_spread_skips(symbol):
    quotes = bullish_quotes()
    idx = [x.symbol for x in quotes].index(symbol)
    base = quotes[idx]
    quotes[idx] = q(symbol, base.mid, open_=base.day_open, prev=base.prev_close, spread=base.mid * 0.01)
    decision = _engine().decide(snapshot(*quotes))
    assert decision.decision is SignalDirection.SKIP
    assert decision.skip_reason is SignalReason.WIDE_SPREAD
    assert symbol in decision.explanation


def test_crossed_quote_skips():
    quotes = bullish_quotes()
    quotes[2] = SymbolQuote(symbol="IWM", bid=220.5, ask=220.0, day_open=218.0, prev_close=217.0, quote_age_seconds=0.1, quote_updates=5)
    assert _engine().decide(snapshot(*quotes)).skip_reason is SignalReason.INVALID_QUOTE


def test_missing_iwm_reference_skips():
    quotes = bullish_quotes()
    quotes[2] = q("IWM", 220.0, first=219.0)
    assert _engine().decide(snapshot(*quotes)).skip_reason is SignalReason.INSUFFICIENT_REFERENCE_DATA


def test_spy_and_qqq_both_disagree_skips():
    quotes = bullish_quotes()
    quotes[3] = q("SPY", 570.0, open_=575.0, prev=576.0)
    quotes[4] = q("QQQ", 490.0, open_=495.0, prev=496.0)
    decision = _engine().decide(snapshot(*quotes))
    assert decision.skip_reason is SignalReason.BROAD_MARKET_DISAGREEMENT


def test_one_index_disagreeing_lowers_confidence():
    quotes = bullish_quotes()
    quotes[4] = q("QQQ", 490.0, open_=495.0, prev=496.0)
    decision = _engine().decide(snapshot(*quotes))
    full = _engine().decide(snapshot(*bullish_quotes()))
    assert decision.bullish_score < full.bullish_score
    assert decision.decision is SignalDirection.SKIP or decision.confidence_score < full.confidence_score


def test_vix_missing_does_not_fail():
    decision = _engine().decide(snapshot(*bullish_quotes()))
    assert decision.decision is SignalDirection.BULLISH
    assert any("VIX missing" in w for w in decision.warnings)


def test_vix_warning_reduces_confidence():
    calm = _engine().decide(snapshot(*bullish_quotes(), vix(15.0, 15.5)))
    elevated = _engine().decide(snapshot(*bullish_quotes(), vix(27.0, 24.0)))
    assert elevated.confidence_score < calm.confidence_score
    assert elevated.market_regime == "high_volatility"
    assert any("VIX elevated" in w for w in elevated.warnings)
    penalties = {p.name for p in elevated.score_breakdown.penalties}
    assert {"vix_high", "vix_rising"} <= penalties


def test_vix_rising_alone_reduces_confidence():
    calm = _engine().decide(snapshot(*bullish_quotes(), vix(15.0, 15.5)))
    rising = _engine().decide(snapshot(*bullish_quotes(), vix(17.0, 15.0)))
    assert rising.confidence_score < calm.confidence_score


def test_extreme_vix_skips():
    decision = _engine().decide(snapshot(*bullish_quotes(), vix(40.0, 30.0)))
    assert decision.skip_reason is SignalReason.HIGH_VOLATILITY


def test_weak_signal_skips_with_insufficient_strength():
    quotes = bullish_quotes()
    quotes[2] = q("IWM", 220.0, open_=218.0, prev=217.0, first=220.0)  # no momentum
    quotes[3] = q("SPY", 575.0, open_=575.0, prev=574.0)  # flat vs open
    quotes[4] = q("QQQ", 495.0, open_=495.0, prev=494.0)
    decision = _engine().decide(snapshot(*quotes))
    assert decision.decision is SignalDirection.SKIP
    assert decision.skip_reason is SignalReason.INSUFFICIENT_SIGNAL_STRENGTH


def test_stricter_threshold_turns_trade_into_skip():
    quotes = bullish_quotes()
    quotes[4] = q("QQQ", 495.0, open_=495.0, prev=494.0)
    assert _engine().decide(snapshot(*quotes)).decision is SignalDirection.BULLISH
    assert _engine(entry_score_threshold=95.0).decide(snapshot(*quotes)).decision is SignalDirection.SKIP


# ---------------------------------------------------------------------------
# DXLink stream -> snapshot -> decision (fake WebSocket)
# ---------------------------------------------------------------------------


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds

    def sleep(self, seconds: float) -> None:
        self.t += seconds


class FakeConnection:
    def __init__(self, clock: FakeClock, incoming: list) -> None:
        self.clock = clock
        self.incoming = list(incoming)
        self.sent: list = []
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
        return json.dumps(payload)

    def close(self) -> None:
        self.closed = True


class FakeTokenProvider:
    IS_MARKET_DATA_ONLY = True

    def __init__(self, clock: FakeClock, exc: Exception | None = None) -> None:
        self.clock = clock
        self.exc = exc

    def get_token(self, *, force_refresh: bool = False) -> QuoteToken:
        if self.exc:
            raise self.exc
        return QuoteToken(
            token=QUOTE_TOKEN,
            dxlink_url=DXLINK_URL,
            level="api",
            issued_at=NOW,
            expires_at=NOW + timedelta(hours=24),
            fetched_at=self.clock(),
        )

    def invalidate(self) -> None:
        pass


def _handshake() -> list:
    return [
        (0, {"type": "SETUP", "channel": 0, "version": "1.0", "keepaliveTimeout": 60}),
        (0, {"type": "AUTH_STATE", "channel": 0, "state": "UNAUTHORIZED"}),
        (0, {"type": "AUTH_STATE", "channel": 0, "state": "AUTHORIZED"}),
        (0, {"type": "CHANNEL_OPENED", "channel": 3, "service": "FEED", "parameters": {"contract": "AUTO"}}),
        (0, {"type": "FEED_CONFIG", "channel": 3, "dataFormat": "COMPACT", "eventFields": DEFAULT_EVENT_FIELDS}),
    ]


BULL_REFS = {"TNA": (43.0, 42.5), "TZA": (10.5, 10.6), "IWM": (218.0, 217.0), "SPY": (575.0, 574.0), "QQQ": (495.0, 494.0)}
BULL_START = {"TNA": 44.0, "TZA": 10.2, "IWM": 219.0, "SPY": 579.0, "QQQ": 499.0}
BULL_END = {"TNA": 45.0, "TZA": 10.0, "IWM": 220.0, "SPY": 580.0, "QQQ": 500.0}


def _summary_frame(refs, vix_prev=15.5) -> dict:
    flat: list = []
    for symbol, (open_, prev) in refs.items():
        flat += ["Summary", symbol, open_, open_ * 1.01, open_ * 0.99, prev]
    flat += ["Summary", "VIX", vix_prev, vix_prev, vix_prev, vix_prev]
    return {"type": "FEED_DATA", "channel": 3, "data": ["Summary", flat]}


def _vix_trade(level=15.0) -> dict:
    return {"type": "FEED_DATA", "channel": 3, "data": ["Trade", ["Trade", "VIX", level, 0, 0]]}


def _quote_frame(prices, spread=0.02) -> dict:
    flat: list = []
    for symbol, mid in prices.items():
        flat += ["Quote", symbol, round(mid - spread / 2, 4), round(mid + spread / 2, 4), 100, 200]
    return {"type": "FEED_DATA", "channel": 3, "data": ["Quote", flat]}


def _market_stream(seconds=4.0, every=0.2, *, gap_at=None, gap=0.0, stop_at=None) -> list:
    """Quotes drift from BULL_START to BULL_END. Optional mid-window gap or early stop."""
    messages: list = [(0.05, _summary_frame(BULL_REFS)), (0.05, _vix_trade())]
    elapsed = 0.1
    steps = int(seconds / every)
    for i in range(steps):
        frac = i / max(steps - 1, 1)
        prices = {s: BULL_START[s] + (BULL_END[s] - BULL_START[s]) * frac for s in BULL_START}
        delay = gap if gap_at is not None and abs(elapsed - gap_at) < every / 2 else every
        if stop_at is not None and elapsed + delay > stop_at:
            break
        messages.append((delay, _quote_frame(prices)))
        elapsed += delay
    return messages


def _stream_factory(clock, connections):
    queue = list(connections)

    def connect(url, _timeout):
        return queue.pop(0)

    def factory(provider, symbols):
        return DXLinkStreamClient(
            provider,
            symbols=symbols,
            connect_factory=connect,
            clock=clock,
            wall_clock=lambda: NOW,
            sleep=clock.sleep,
        )

    return factory


def _collect(messages, *, duration=4.0, engine=None):
    clock = FakeClock()
    connection = FakeConnection(clock, _handshake() + messages)
    result = collect_signal_from_dxlink(
        validate_market_data_settings(_settings()),
        engine=engine or _engine(),
        duration_seconds=duration,
        provider=FakeTokenProvider(clock),
        stream_factory=_stream_factory(clock, [connection]),
    )
    return result, connection


def test_stream_collects_bullish_decision():
    result, connection = _collect(_market_stream())
    decision = result.decision
    assert decision.freshness_gate_passed is True
    assert decision.decision is SignalDirection.BULLISH
    assert decision.selected_symbol == "TNA"
    iwm = result.snapshot.get("IWM")
    assert iwm.first_mid == pytest.approx(219.0)
    assert iwm.price == pytest.approx(220.0, abs=0.1)
    assert connection.closed is True


def test_mid_window_gap_does_not_fail_decision_moment_gate():
    """Correct 2.11 rule: only freshness at the decision moment matters."""
    result, _ = _collect(_market_stream(seconds=6.0, gap_at=2.05, gap=2.5), duration=6.0)
    assert result.decision.freshness_gate_passed is True
    assert result.decision.decision is SignalDirection.BULLISH


def test_stale_at_decision_moment_skips():
    result, _ = _collect(_market_stream(seconds=6.0, stop_at=3.0), duration=6.0)
    assert result.decision.decision is SignalDirection.SKIP
    assert result.decision.skip_reason is SignalReason.STALE_MARKET_DATA


def test_no_stream_data_skips_missing():
    result, _ = _collect([], duration=2.0)
    assert result.decision.skip_reason is SignalReason.MISSING_REQUIRED_QUOTE


def test_pre_submit_check_requires_fresh_matching_signal():
    clock = FakeClock()
    config = validate_market_data_settings(_settings())
    provider = FakeTokenProvider(clock)
    fresh = FakeConnection(clock, _handshake() + _market_stream(seconds=3.0))
    stale = FakeConnection(clock, _handshake() + _market_stream(seconds=3.0, stop_at=1.0))
    check = build_pre_submit_check(
        config,
        engine=_engine(),
        expected=SignalDirection.BULLISH,
        provider=provider,
        revalidation_seconds=3.0,
        stream_factory=_stream_factory(clock, [fresh, stale]),
    )
    assert check()[0] is True
    ok, reason = check()
    assert ok is False and "stale_market_data" in reason

    bearish_check = build_pre_submit_check(
        config,
        engine=_engine(),
        expected=SignalDirection.BEARISH,
        provider=provider,
        revalidation_seconds=3.0,
        stream_factory=_stream_factory(clock, [FakeConnection(clock, _handshake() + _market_stream(seconds=3.0))]),
    )
    ok, reason = bearish_check()
    assert ok is False and "signal changed" in reason
    with pytest.raises(ValueError):
        build_pre_submit_check(config, engine=_engine(), expected=SignalDirection.SKIP, provider=provider)


def test_pre_submit_check_denies_on_stream_error():
    clock = FakeClock()
    err = MarketDataError("boom", step="quote_token", reason="network_error")
    check = build_pre_submit_check(
        validate_market_data_settings(_settings()),
        engine=_engine(),
        expected=SignalDirection.BULLISH,
        provider=FakeTokenProvider(clock, exc=err),
    )
    ok, reason = check()
    assert ok is False and "network_error" in reason


# ---------------------------------------------------------------------------
# Diagnostic script
# ---------------------------------------------------------------------------


def _run_script(capsys, messages=None, *, provider_exc=None, settings=None, **kwargs):
    clock = FakeClock()
    connection = FakeConnection(clock, _handshake() + (messages if messages is not None else _market_stream()))
    provider = FakeTokenProvider(clock, exc=provider_exc)
    kwargs.setdefault("duration_seconds", 4.0)
    code = script.run_signal_check(
        settings or _settings(),
        token_provider_factory=lambda _cfg: provider,
        stream_factory=_stream_factory(clock, [connection]),
        **kwargs,
    )
    return code, capsys.readouterr().out


def test_script_bullish_human_output(capsys):
    code, out = _run_script(capsys)
    assert code == 0
    assert "production_order_execution: blocked" in out
    assert "--- quote freshness (at decision moment) ---" in out
    assert "--- market data status ---" in out
    assert "--- score breakdown ---" in out
    assert "decision: TNA (bullish)" in out
    assert "explanation:" in out
    assert script.COMPLETED in out


def test_script_exits_zero_on_skip(capsys):
    code, out = _run_script(capsys, _market_stream(seconds=6.0, stop_at=2.0), duration_seconds=6.0)
    assert code == 0
    assert "decision: SKIP (skip)" in out
    assert "skip_reason: stale_market_data" in out
    assert "not computed" in out


def test_script_json_output_is_single_redacted_document(capsys):
    code, out = _run_script(capsys, json_output=True)
    assert code == 0
    doc = json.loads(out)
    assert doc["signal_engine_check"] == "completed"
    assert doc["decision"]["decision"] == "bullish"
    assert doc["decision"]["selected_symbol"] == "TNA"
    assert doc["execution"]["production_order_execution"] == "blocked"
    assert doc["execution"]["orders_submitted"] == 0
    assert set(doc["decision"]["quote_freshness_by_symbol"]) >= {"TNA", "TZA", "IWM", "SPY", "QQQ"}
    for secret in SECRETS:
        assert secret not in out


def test_script_never_prints_secrets(capsys):
    _, out = _run_script(capsys)
    for secret in SECRETS:
        assert secret not in out


def test_script_config_error_exit_2(capsys):
    code, out = _run_script(capsys, settings=_settings(tastytrade_market_data_refresh_token=""))
    assert code == script.CONFIG_EXIT_CODE
    assert "TASTYTRADE_MARKET_DATA_REFRESH_TOKEN is not set" in out


def test_script_config_error_json_redacted(capsys):
    code, out = _run_script(
        capsys,
        settings=_settings(tastytrade_market_data_client_secret=SANDBOX_SECRET),
        json_output=True,
    )
    assert code == 2
    doc = json.loads(out)
    assert doc["signal_engine_check"] == "failed"
    for secret in SECRETS:
        assert secret not in out


def test_script_requires_core_symbols(capsys):
    code, out = _run_script(capsys, symbols=["TNA", "TZA", "VIX"])
    assert code == 2
    assert "IWM" in out


def test_script_rejects_non_sandbox_execution(capsys):
    code, _ = _run_script(capsys, settings=_settings(tastytrade_env="production"))
    assert code == 2


@pytest.mark.parametrize(
    "reason, expected",
    [("rate_limited", 3), ("network_error", 1), ("unauthorized", 1)],
)
def test_script_auth_stream_failures_non_zero(capsys, reason, expected):
    err = MarketDataError("failed", step="quote_token", reason=reason)
    code, out = _run_script(capsys, provider_exc=err)
    assert code == expected
    assert script.FAILED in out


def test_script_no_vix_still_runs(capsys):
    code, out = _run_script(capsys, symbols=["TNA", "TZA", "IWM", "SPY", "QQQ"])
    assert code == 0
    assert "VIX missing" in out


def test_verify_production_execution_blocked():
    checks = script.verify_production_execution_blocked()
    assert checks and all(checks.values())
    with pytest.raises(MarketDataUrlBlockedError):
        assert_market_data_request("POST", f"{PRODUCTION_BASE_URL}/accounts/X/orders")


# ---------------------------------------------------------------------------
# Safety: no orders, no RiskEngine bypass, blocked routes/live/production
# ---------------------------------------------------------------------------


_FORBIDDEN_IMPORTS = (
    "order_executor",
    "execution_router",
    "tastytrade_sandbox",
    "sandbox_worker",
    "OrderExecutor",
    "submit_equity_order",
    "execute_order",
)


@pytest.mark.parametrize("module", [signal_models_mod, engine_mod, signal_source_mod, script])
def test_signal_engine_never_submits_orders(module):
    source = inspect.getsource(module)
    for token in _FORBIDDEN_IMPORTS:
        assert token not in source, f"{module.__name__} references {token}"


def test_engine_has_no_order_capability():
    engine = _engine()
    assert engine.IS_SIGNAL_ONLY is True
    for name in ("execute", "submit", "place_order", "route"):
        assert not hasattr(engine, name)


def _worker_adapter():
    adapter = MagicMock()
    adapter._auth = MagicMock(is_authenticated=True)
    adapter.get_accounts.return_value = [{"account-number": "5WM30541"}]
    adapter.get_balance.return_value = {"account_number": "5WM30541", "buying_power": 100000.0, "cash_balance": 1e5}
    adapter.get_positions.return_value = []
    adapter.list_live_orders.return_value = []
    adapter.dry_run_equity_order.return_value = {"data": {}}
    return adapter


def test_dxlink_signal_does_not_bypass_risk_engine():
    adapter = _worker_adapter()
    worker = SandboxBotWorker(_settings(), adapter)
    worker._executor = MagicMock()
    result = worker.run_cycle(
        signal="bullish",
        confirm_submit=True,
        signal_source="dxlink",
        market_data_healthy=False,
        pre_submit_check=lambda: (True, "ok"),
    )
    assert result.decision_status == "risk_rejected"
    assert result.submitted is False
    worker._executor.execute.assert_not_called()
    adapter.dry_run_equity_order.assert_not_called()


def test_pre_submit_gate_blocks_confirmed_submit():
    adapter = _worker_adapter()
    worker = SandboxBotWorker(_settings(), adapter)
    worker._executor = MagicMock()
    result = worker.run_cycle(
        signal="bullish",
        confirm_submit=True,
        signal_source="dxlink",
        pre_submit_check=lambda: (False, "stale_market_data at submit time"),
    )
    assert result.decision_status == "skipped_market_data_gate"
    assert result.submitted is False and result.dry_run_passed is True
    worker._executor.execute.assert_not_called()


def test_pre_submit_gate_exception_blocks_submit():
    worker = SandboxBotWorker(_settings(), _worker_adapter())
    worker._executor = MagicMock()

    def boom():
        raise RuntimeError("stream died")

    result = worker.run_cycle(signal="bearish", confirm_submit=True, pre_submit_check=boom)
    assert result.decision_status == "skipped_market_data_gate"
    worker._executor.execute.assert_not_called()


def test_pre_submit_gate_not_called_without_confirm():
    worker = SandboxBotWorker(_settings(), _worker_adapter())
    worker._executor = MagicMock()
    check = MagicMock(return_value=(True, "ok"))
    result = worker.run_cycle(signal="bullish", signal_source="dxlink", pre_submit_check=check)
    assert result.decision_status == "dry_run_passed"
    check.assert_not_called()
    worker._executor.execute.assert_not_called()


def test_pre_submit_gate_pass_allows_sandbox_executor_only_when_confirmed():
    worker = SandboxBotWorker(_settings(), _worker_adapter())
    worker._executor = MagicMock()
    worker._executor.execute.return_value = MagicMock(success=True, order_id="o1", raw={}, message="ok")
    result = worker.run_cycle(signal="bullish", confirm_submit=True, pre_submit_check=lambda: (True, "fresh"))
    assert result.decision_status == "submitted"
    intent = worker._executor.execute.call_args[0][0]
    assert intent.trading_mode == "sandbox"


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


def test_production_execution_remains_blocked():
    adapter = MagicMock()
    router = ExecutionRouter(sandbox_adapter=adapter)
    intent = OrderIntent(symbol="TNA", side="buy", quantity=1, trading_mode="sandbox")
    for ctx in (_context(live_trading_enabled=True), _context(trading_mode="live"), _context(tastytrade_env="production")):
        assert router.route(intent, ctx).success is False
    adapter.execute_order.assert_not_called()


def test_live_trading_remains_blocked(monkeypatch):
    with pytest.raises(LiveTradingBlockedError):
        assert_order_execution_allowed(Settings(live_trading_enabled=True))
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    with pytest.raises(ConfigurationError):
        load_settings(env_path=_NO_ENV)


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


def test_decision_repr_and_json_contain_no_secrets():
    decision = _engine().decide(snapshot(*bullish_quotes()))
    blob = json.dumps(decision.to_dict()) + repr(decision)
    for secret in SECRETS:
        assert secret not in blob


# ---------------------------------------------------------------------------
# run_sandbox_bot_cycle.py --signal-source dxlink (dry-run by default)
# ---------------------------------------------------------------------------


def _load_cycle_module():
    path = REPO_ROOT / "scripts" / "run_sandbox_bot_cycle.py"
    spec = importlib.util.spec_from_file_location("run_sandbox_bot_cycle_211", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class _RecordingWorker:
    instances: list = []

    def __init__(self) -> None:
        self.calls: list = []

    @classmethod
    def from_settings(cls, settings, with_db=False):
        worker = cls()
        cls.instances.append(worker)
        return worker

    def run_cycle(self, **kwargs):
        self.calls.append(kwargs)
        status = "dry_run_passed" if not kwargs.get("confirm_submit") else "skipped_market_data_gate"
        return SandboxBotCycleResult(success=True, decision_status=status, signal=kwargs["signal"])


def _cycle(monkeypatch, messages, argv, *, revalidation=None):
    mod = _load_cycle_module()
    clock = FakeClock()
    connections = [FakeConnection(clock, _handshake() + messages)]
    if revalidation is not None:
        connections.append(FakeConnection(clock, _handshake() + revalidation))
    _RecordingWorker.instances = []
    monkeypatch.setattr(mod, "SandboxBotWorker", _RecordingWorker)
    monkeypatch.setattr(mod, "validate_sandbox_env", lambda script_name: _settings())
    monkeypatch.setattr(mod, "print_env_check", lambda settings: True)
    monkeypatch.setattr(mod, "TOKEN_PROVIDER_FACTORY", lambda cfg: FakeTokenProvider(clock))
    monkeypatch.setattr(mod, "STREAM_FACTORY", _stream_factory(clock, connections))
    return mod.main(argv)


def test_cycle_default_signal_source_is_manual(monkeypatch, capsys):
    mod = _load_cycle_module()
    _RecordingWorker.instances = []
    monkeypatch.setattr(mod, "SandboxBotWorker", _RecordingWorker)
    monkeypatch.setattr(mod, "validate_sandbox_env", lambda script_name: _settings())
    monkeypatch.setattr(mod, "print_env_check", lambda settings: True)
    monkeypatch.setattr(mod, "STREAM_FACTORY", MagicMock(side_effect=AssertionError("no stream in manual")))
    assert mod.main(["--signal", "bullish"]) == 0
    call = _RecordingWorker.instances[0].calls[0]
    assert call["signal"] == "bullish"
    assert "signal_source" not in call
    assert "signal_source: manual" in capsys.readouterr().out


def test_cycle_dxlink_bullish_is_dry_run_by_default(monkeypatch, capsys):
    code = _cycle(monkeypatch, _market_stream(), ["--signal-source", "dxlink", "--signal-duration-seconds", "4"])
    assert code == 0
    call = _RecordingWorker.instances[0].calls[0]
    assert call["signal"] == "bullish"
    assert call["confirm_submit"] is False
    assert call["pre_submit_check"] is None
    assert call["signal_source"] == "dxlink"
    assert call["market_data_healthy"] is True
    assert call["reference_price"] == pytest.approx(45.0, abs=0.1)
    out = capsys.readouterr().out
    assert "decision: TNA (bullish)" in out
    assert "decision_status: dry_run_passed" in out


def test_cycle_dxlink_skip_never_invokes_worker(monkeypatch, capsys):
    code = _cycle(
        monkeypatch,
        _market_stream(seconds=6.0, stop_at=2.0),
        ["--signal-source", "dxlink", "--signal-duration-seconds", "6", "--confirm-sandbox-submit"],
    )
    assert code == 0
    assert _RecordingWorker.instances == []
    out = capsys.readouterr().out
    assert "decision: SKIP (skip)" in out
    assert "stale_market_data" in out
    assert "decision_status: skipped_no_signal" in out


def test_cycle_dxlink_confirm_wires_pre_submit_revalidation(monkeypatch):
    code = _cycle(
        monkeypatch,
        _market_stream(),
        ["--signal-source", "dxlink", "--signal-duration-seconds", "4", "--confirm-sandbox-submit"],
        revalidation=_market_stream(seconds=3.0, stop_at=1.0),
    )
    assert code == 0
    call = _RecordingWorker.instances[0].calls[0]
    assert call["confirm_submit"] is True
    ok, reason = call["pre_submit_check"]()
    assert ok is False and "stale_market_data" in reason


def test_cycle_dxlink_rejects_manual_signal(monkeypatch):
    mod = _load_cycle_module()
    assert mod.main(["--signal-source", "dxlink", "--signal", "bullish"]) == 2


def test_cycle_dxlink_stream_failure_exit_codes(monkeypatch):
    mod = _load_cycle_module()
    clock = FakeClock()
    monkeypatch.setattr(mod, "SandboxBotWorker", _RecordingWorker)
    monkeypatch.setattr(mod, "validate_sandbox_env", lambda script_name: _settings())
    monkeypatch.setattr(mod, "print_env_check", lambda settings: True)
    for reason, expected in (("rate_limited", 3), ("network_error", 1)):
        err = MarketDataError("x", step="quote_token", reason=reason)
        monkeypatch.setattr(mod, "TOKEN_PROVIDER_FACTORY", lambda cfg, e=err: FakeTokenProvider(clock, exc=e))
        assert mod.main(["--signal-source", "dxlink"]) == expected


def test_cycle_dxlink_requires_market_data_config(monkeypatch):
    mod = _load_cycle_module()
    monkeypatch.setattr(mod, "validate_sandbox_env", lambda script_name: _settings(tastytrade_market_data_client_id=""))
    monkeypatch.setattr(mod, "print_env_check", lambda settings: True)
    assert mod.main(["--signal-source", "dxlink"]) == 2
