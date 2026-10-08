"""Checkpoint 2.18 — stream warmup, stale-feed reconnect, and strict vs meaningful scoring."""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock

import pytest

import backend.app_factory as app_factory
import backend.bot_worker.sandbox_worker as sandbox_worker
import backend.execution.execution_router as execution_router
import backend.shadow_mode.profile_replay as profile_replay
import backend.shadow_mode.runner as runner_mod
import scripts.run_sandbox_bot_cycle as sandbox_cycle
from backend.config.settings import ConfigurationError, Settings, load_settings
from backend.execution.execution_router import ExecutionRouter
from backend.market_data.config import validate_market_data_settings
from backend.risk.live_guard import LiveTradingBlockedError, assert_order_execution_allowed
from backend.risk.models import OrderIntent, RiskContext
from backend.shadow_mode.models import compute_followup_outcome
from backend.shadow_mode.profile_replay import compare_signal_profiles
from backend.shadow_mode.report import summarize_shadow_logs
from backend.shadow_mode.runner import ShadowModeRunner, ShadowRunConfig
from backend.shadow_mode.logger import ShadowSignalLogger
from backend.signals.models import MarketSnapshot, SignalDirection, SymbolQuote
from scripts import replay_signal_engine_versions as replay_script
from scripts import report_shadow_signal_logs as report_script
from tests.test_checkpoint_212_shadow_mode import (
    BULL,
    _NO_ENV,
    _engine,
    _flat,
    _run_cli,
    _settings,
    _stream,
    FakeClock,
    FakeTokenProvider,
    StreamFactory,
)
from tests.test_checkpoint_217a_balanced_shadow_profile import _stored, snap, window_bull

_FORBIDDEN = (
    "order_executor",
    "OrderExecutor",
    "execution_router",
    "ExecutionRouter",
    "submit_equity_order",
    "execute_order",
    "dry_run_equity_order",
)


def _ready_config(**overrides) -> ShadowRunConfig:
    base = dict(
        cycles=1,
        signal_duration_seconds=4.0,
        pause_seconds=0.0,
        followup_seconds=0.0,
        readiness_checks=True,
    )
    base.update(overrides)
    return ShadowRunConfig(**base)


def _run(scripts, *, engine=None, **config):
    clock = FakeClock()
    factory = StreamFactory(clock, scripts)
    events = []
    if engine is None:
        engine = MagicMock()
        engine.decide = MagicMock()
        engine.config.max_quote_age_seconds = 1.0
    logger = ShadowSignalLogger(None)
    runner = ShadowModeRunner(
        validate_market_data_settings(_settings()),
        engine,
        logger,
        _ready_config(**config),
        run_id="shadow-warmup",
        provider=FakeTokenProvider(clock),
        stream_factory=factory,
        sleep=lambda _seconds: None,
        on_event=lambda kind, payload: events.append((kind, payload)),
    )
    return runner.run(), logger, events, engine, factory


def test_shadow_logger_waits_for_fresh_quotes_before_cycle_1(capsys):
    code, out, factory, _ = _run_cli(
        capsys,
        [_stream(BULL, seconds=6.0), _stream(BULL, seconds=2.0), _stream(BULL)],
        cycles=1,
        readiness_checks=True,
    )
    assert code == 0
    assert "warmup_started" in out
    assert "warmup_symbols_ready" in out
    assert "warmup_seconds:" in out
    assert "decision: TNA (bullish)" in out
    assert factory.calls >= 3


def test_warmup_failure_logs_no_cycles(capsys):
    code, out, _, _ = _run_cli(capsys, [[]], cycles=1, readiness_checks=True)
    assert code != 0
    assert "warmup_started" in out
    assert "warmup_failed_reason:" in out
    assert "decision:" not in out


def test_stale_feed_reconnects_and_resubscribes():
    summary, logger, events, engine, factory = _run(
        [
            _stream(BULL, seconds=6.0),
            _stream(BULL, seconds=2.0),
            _stream(BULL, seconds=25.0, stop_at=0.4),
            _stream(BULL, seconds=25.0, every=0.5),
        ],
        engine=_engine(),
        signal_duration_seconds=25.0,
    )
    kinds = [kind for kind, _payload in events]
    assert "stream_reconnect_due_to_stale_feed" in kinds
    assert summary.aborted is False
    assert logger.records[0].decision == "bullish"
    assert logger.records[0].skip_reason != "stale_market_data"
    assert factory.calls == 4


def test_stream_not_ready_is_separate_from_a_signal_skip():
    summary, logger, _events, engine, _factory = _run(
        [_stream(BULL, seconds=6.0), [], []],
    )
    assert summary.cycles_completed == 1
    record = logger.records[0]
    assert record.skip_reason == "stream_not_ready"
    assert record.decision == "skip"
    assert record.freshness_gate_passed is False
    assert record.skip_reason != "stale_market_data"
    engine.decide.assert_not_called()


def test_stale_followup_is_not_scored():
    after = MarketSnapshot(
        quotes={
            "TNA": SymbolQuote("TNA", bid=45.49, ask=45.51, quote_age_seconds=0.2, quote_updates=2),
            "IWM": SymbolQuote("IWM", bid=220.49, ask=220.51, quote_age_seconds=2.5, quote_updates=2),
            "SPY": SymbolQuote("SPY", bid=580.49, ask=580.51, quote_age_seconds=0.2, quote_updates=2),
            "QQQ": SymbolQuote("QQQ", bid=500.49, ask=500.51, quote_age_seconds=0.2, quote_updates=2),
        }
    )
    outcome = compute_followup_outcome(
        decision="bullish",
        selected_symbol="TNA",
        mids_before={"TNA": 45.0, "IWM": 220.0, "SPY": 580.0, "QQQ": 500.0},
        after=after,
        followup_seconds=60.0,
    )
    assert outcome.direction_was_correct is None
    assert outcome.selected_symbol_move_pct is not None
    assert "unscored_due_to_stale_followup" in outcome.outcome_note
    assert "IWM" in outcome.outcome_note


def test_replay_strict_matches_report_and_meaningful_is_separate(capsys):
    large = _stored(snap(*window_bull()), run_id="r", cycle=1, iwm_after_pct=0.12, created="2026-10-08T14:00:00")
    tiny = _stored(snap(*window_bull()), run_id="r", cycle=2, iwm_after_pct=-0.005, created="2026-10-08T14:02:00")
    rows = [large, tiny]
    stats = compare_signal_profiles(rows, ["balanced_v3_shadow"], min_move_pct=0.03)["profiles"]["balanced_v3_shadow"]
    assert stats["strict_scored_count"] == 2
    assert stats["strict_correct"] == 1
    assert stats["strict_incorrect"] == 1
    assert stats["strict_correct_pct"] == 50.0
    assert stats["correct_pct"] == stats["strict_correct_pct"]
    assert stats["meaningful_scored_count"] == 1
    assert stats["meaningful_correct"] == 1
    assert stats["meaningful_incorrect"] == 0
    assert stats["meaningful_correct_pct"] == 100.0

    report_rows = []
    for row, decision in ((large, "bullish"), (tiny, "bullish")):
        outcome = compute_followup_outcome(
            decision=decision,
            selected_symbol="TNA",
            mids_before={"TNA": row["tna_mid"], "TZA": row["tza_mid"], "IWM": row["iwm_mid"]},
            after=MarketSnapshot(
                quotes={
                    "TNA": SymbolQuote("TNA", bid=row["tna_mid_after"] - 0.01, ask=row["tna_mid_after"] + 0.01, quote_age_seconds=0.2, quote_updates=2),
                    "IWM": SymbolQuote("IWM", bid=row["iwm_mid_after"] - 0.01, ask=row["iwm_mid_after"] + 0.01, quote_age_seconds=0.2, quote_updates=2),
                    "SPY": SymbolQuote("SPY", bid=row["spy_mid"], ask=row["spy_mid"] + 0.02, quote_age_seconds=0.2, quote_updates=2),
                    "QQQ": SymbolQuote("QQQ", bid=row["qqq_mid"], ask=row["qqq_mid"] + 0.02, quote_age_seconds=0.2, quote_updates=2),
                }
            ),
            followup_seconds=60.0,
        )
        report_rows.append(
            {
                "decision": decision,
                "selected_symbol": "TNA",
                "direction_was_correct": outcome.direction_was_correct,
                "selected_symbol_move_pct": outcome.selected_symbol_move_pct,
                "iwm_move_pct": outcome.iwm_move_pct,
                "outcome_note": outcome.outcome_note,
                "followup_seconds": 60.0,
                "freshness_gate_passed": True,
                "quote_age_tna": 0.2,
                "quote_age_tza": 0.2,
                "quote_age_iwm": 0.2,
                "quote_age_spy": 0.2,
                "quote_age_qqq": 0.2,
                "confidence_score": 80,
                "submitted": False,
                "production_execution_blocked": True,
            }
        )
    report = summarize_shadow_logs(report_rows)
    assert report["strict_direction_outcome"]["scored"] == stats["strict_scored_count"]
    assert report["strict_direction_outcome"]["correct"] == stats["strict_correct"]
    assert report["strict_direction_outcome"]["incorrect"] == stats["strict_incorrect"]
    assert report["meaningful_move_outcome"]["correct_pct"] == stats["meaningful_correct_pct"]

    def loader(_settings, _run_ids, limit, session_dates=None):
        return rows[:limit]

    assert replay_script.run_replay(_settings(), rows_loader=loader, signal_profile="balanced_v3_shadow") == 0
    text = capsys.readouterr().out
    for label in (
        "strict_scored_count:",
        "strict_correct:",
        "strict_incorrect:",
        "strict_correct_pct:",
        "meaningful_scored_count:",
        "meaningful_correct:",
        "meaningful_incorrect:",
        "meaningful_correct_pct:",
    ):
        assert label in text


def test_weak_freshness_run_is_invalid_for_signal_evaluation(capsys):
    rows = [
        {
            "decision": "skip",
            "skip_reason": "stale_market_data",
            "freshness_gate_passed": False,
            "quote_age_tna": 29.9,
            "quote_age_tza": 29.9,
            "quote_age_iwm": 29.9,
            "quote_age_spy": 29.9,
            "quote_age_qqq": 29.9,
            "confidence_score": 0,
            "submitted": False,
            "production_execution_blocked": True,
            "followup_seconds": 60,
            "outcome_note": "unscored_due_to_stale_followup: IWM",
            "direction_was_correct": None,
        },
        {
            "decision": "bearish",
            "selected_symbol": "TZA",
            "skip_reason": None,
            "freshness_gate_passed": True,
            "quote_age_tna": 1.8,
            "quote_age_tza": 1.8,
            "quote_age_iwm": 1.8,
            "quote_age_spy": 1.8,
            "quote_age_qqq": 1.8,
            "confidence_score": 70,
            "submitted": False,
            "production_execution_blocked": True,
            "followup_seconds": 60,
            "selected_symbol_move_pct": 0.2,
            "iwm_move_pct": -0.2,
            "direction_was_correct": True,
            "outcome_note": "correct",
        },
    ]
    report = summarize_shadow_logs(rows)
    assert report["freshness_gate_pass_pct"] < 90
    assert report["average_quote_age_seconds_overall"] > 1
    assert report["stale_followup_count"] == 1
    assert report["valid_for_signal_evaluation"] is False
    assert report_script.run_report(_settings(), rows_loader=lambda *_args: rows) == 0
    text = capsys.readouterr().out
    assert "valid_for_signal_evaluation: false" in text
    assert "stream_not_ready_count:" in text
    assert "stale_followup_count: 1" in text
    assert "strict_scored_count:" in text
    assert "meaningful_scored_count:" in text


def test_balanced_profile_and_routes_stay_blocked(monkeypatch):
    from backend.signals.candidate_engine_v3 import CandidateEngineV3
    from backend.signals.dxlink_signal_source import build_pre_submit_check

    engine = CandidateEngineV3(mode="shadow")
    assert engine.ALLOWS_EXECUTION is False
    with pytest.raises(ConfigurationError, match="shadow/replay only"):
        build_pre_submit_check(MagicMock(), engine=engine, expected=SignalDirection.BULLISH, provider=MagicMock())
    for module in (runner_mod, profile_replay):
        source = inspect.getsource(module)
        for token in _FORBIDDEN:
            assert token not in source, f"{module.__name__} references {token}"
    for module in (sandbox_worker, sandbox_cycle, execution_router, app_factory):
        assert "stream_readiness" not in inspect.getsource(module)
    source = inspect.getsource(runner_mod)
    for token in _FORBIDDEN:
        assert token not in source
    adapter = MagicMock()
    router = ExecutionRouter(sandbox_adapter=adapter)
    intent = OrderIntent(symbol="TNA", side="buy", quantity=1, trading_mode="sandbox")
    ctx = RiskContext(
        trading_mode="live",
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
    assert router.route(intent, ctx).success is False
    adapter.execute_order.assert_not_called()
    with pytest.raises(LiveTradingBlockedError):
        assert_order_execution_allowed(Settings(live_trading_enabled=True))
    monkeypatch.setenv("TRADING_MODE", "sandbox")
    monkeypatch.setenv("TASTYTRADE_ENV", "sandbox")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    from backend.app_factory import create_app
    from backend.config.settings import reset_settings_cache

    reset_settings_cache()
    client = create_app(skip_db_init=True, defer_heavy_services=True).test_client()
    assert client.post("/bot/start").status_code == 423
    assert client.post("/trade/execute", json={"symbol": "TNA"}).status_code == 423
    assert client.post("/trade/close/1").status_code == 423
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    with pytest.raises(ConfigurationError):
        load_settings(env_path=_NO_ENV)


def test_runner_source_has_no_order_executor():
    source = inspect.getsource(runner_mod)
    for token in _FORBIDDEN:
        assert token not in source
    assert _flat is not None
    assert _engine is not None
