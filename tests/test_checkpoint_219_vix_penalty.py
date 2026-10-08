"""Checkpoint 2.19 — balanced_v3_shadow treats elevated VIX as a penalty, not a hard skip."""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock

import pytest

import backend.app_factory as app_factory
import backend.execution.execution_router as execution_router
import backend.shadow_mode.profile_replay as profile_replay
import backend.signals.candidate_engine_v3 as v3_mod
from backend.config.settings import ConfigurationError, Settings, load_settings
from backend.execution.execution_router import ExecutionRouter
from backend.risk.live_guard import LiveTradingBlockedError, assert_order_execution_allowed
from backend.risk.models import OrderIntent, RiskContext
from backend.shadow_mode.profile_replay import compare_signal_profiles
from backend.shadow_mode.report import summarize_shadow_logs
from backend.signals.candidate_engine_v3 import (
    VIX_DIAGNOSTIC_HARD_BLOCK,
    VIX_DIAGNOSTIC_PENALTY,
    VIX_POLICY_HARD_BLOCK,
    CandidateEngineV3,
    CandidateEngineV3Config,
)
from backend.signals.dxlink_signal_source import build_pre_submit_check
from backend.signals.models import SignalDirection, SignalReason
from scripts import replay_signal_engine_versions as replay_script
from scripts import report_shadow_signal_logs as report_script
from tests.test_checkpoint_212_shadow_mode import _settings as _shadow_settings
from tests.test_checkpoint_215_signal_quality import _engine as v2_engine
from tests.test_checkpoint_215_signal_quality import snap as v2_snap
from tests.test_checkpoint_215_signal_quality import strong_bull
from tests.test_checkpoint_215_signal_quality import vix as v2_vix
from tests.test_checkpoint_217a_balanced_shadow_profile import NOW, _NO_ENV, _settings, _stored, q, snap, vix

_FORBIDDEN = (
    "order_executor",
    "OrderExecutor",
    "execution_router",
    "ExecutionRouter",
    "submit_equity_order",
    "execute_order",
    "dry_run_equity_order",
)


def _v3(**overrides) -> CandidateEngineV3:
    config = CandidateEngineV3Config(**overrides) if overrides else None
    return CandidateEngineV3(config, wall_clock=lambda: NOW, mode="shadow")


def _book(symbol: str, first: float, pct: float):
    mid = first * (1.0 + pct / 100.0)
    low, high = (mid, first) if mid <= first else (first, mid)
    return q(symbol, mid, open_=first, prev=first, first=first, high=high, low=low)


def strong_book(*extra):
    quotes = [
        _book("TNA", 40.0, 0.20),
        _book("TZA", 10.0, -0.10),
        _book("IWM", 220.0, 0.08),
        _book("SPY", 500.0, 0.05),
        _book("QQQ", 400.0, 0.05),
    ]
    quotes.extend(extra)
    return quotes


def flat_book():
    return [
        q("TNA", 40.0, first=40.0),
        q("TZA", 10.0, first=10.0),
        q("IWM", 220.0, first=220.0),
        q("SPY", 500.0, first=500.0),
        q("QQQ", 400.0, first=400.0),
    ]


def test_conservative_v2_still_hard_blocks_extreme_vix_only():
    engine = v2_engine()
    extreme = engine.decide(v2_snap(strong_bull(), v2_vix(40.0, 30.0)))
    calm = engine.decide(v2_snap(strong_bull(), v2_vix(15.0, 15.5)))
    elevated = engine.decide(v2_snap(strong_bull(), v2_vix(27.0, 24.0)))
    rising = engine.decide(v2_snap(strong_bull(), v2_vix(17.0, 15.0)))
    assert extreme.skip_reason is SignalReason.HIGH_VOLATILITY
    assert calm.decision is SignalDirection.BULLISH
    assert elevated.decision is SignalDirection.BULLISH
    assert elevated.confidence_score == calm.confidence_score - 25.0
    assert {item.name for item in elevated.score_breakdown.penalties} >= {"vix_high", "vix_rising"}
    assert rising.skip_reason is not SignalReason.HIGH_VOLATILITY
    assert rising.confidence_score < calm.confidence_score


def test_balanced_profile_penalizes_moderate_vix_change_instead_of_skipping():
    calm = _v3().decide(snap(*strong_book(vix(16.0, 16.2))))
    rising = _v3().decide(snap(*strong_book(vix(16.0, 15.09))))
    assert calm.decision is SignalDirection.BULLISH
    assert rising.decision is SignalDirection.BULLISH
    assert rising.skip_reason is None
    assert rising.profile_scores["vix_diagnostic"] == VIX_DIAGNOSTIC_PENALTY
    assert rising.profile_scores["vix_score"] < calm.profile_scores["vix_score"]
    assert rising.profile_scores["candidate_score"] < calm.profile_scores["candidate_score"]
    assert rising.profile_scores["candidate_score"] >= 70
    assert VIX_DIAGNOSTIC_PENALTY in rising.explanation or any(VIX_DIAGNOSTIC_PENALTY in item for item in rising.warnings)


def test_balanced_profile_still_hard_blocks_extreme_vix():
    decision = _v3().decide(snap(*strong_book(vix(36.0, 34.0))))
    assert decision.decision is SignalDirection.SKIP
    assert decision.skip_reason is SignalReason.HIGH_VOLATILITY
    assert decision.profile_scores["vix_diagnostic"] == VIX_DIAGNOSTIC_HARD_BLOCK
    legacy = _v3(vix_policy=VIX_POLICY_HARD_BLOCK).decide(snap(*strong_book(vix(16.0, 15.09))))
    assert legacy.skip_reason is SignalReason.HIGH_VOLATILITY
    assert legacy.profile_scores["vix_diagnostic"] == VIX_DIAGNOSTIC_HARD_BLOCK


def test_missing_vix_is_caution_and_does_not_crash():
    missing = _v3().decide(snap(*strong_book()))
    calm = _v3().decide(snap(*strong_book(vix(16.0, 16.2))))
    weak = _v3().decide(snap(*flat_book()))
    assert missing.decision is SignalDirection.BULLISH
    assert missing.skip_reason is not SignalReason.HIGH_VOLATILITY
    assert missing.profile_scores["vix_score"] == 7.0
    assert missing.profile_scores["vix_score"] < calm.profile_scores["vix_score"]
    assert missing.profile_scores.get("vix_diagnostic") is None
    assert weak.decision is SignalDirection.SKIP
    assert weak.skip_reason is not SignalReason.HIGH_VOLATILITY


def test_replay_compares_hard_block_with_vix_penalty(capsys):
    calm = _stored(snap(*strong_book(vix(16.0, 16.2))), run_id="r", cycle=1, iwm_after_pct=0.12, created="2026-10-08T14:00:00")
    recovered_win = _stored(snap(*strong_book(vix(16.0, 15.09))), run_id="r", cycle=2, iwm_after_pct=0.12, created="2026-10-08T14:02:00")
    recovered_loss = _stored(snap(*strong_book(vix(16.0, 15.09))), run_id="r", cycle=3, iwm_after_pct=-0.12, created="2026-10-08T14:04:00")
    rows = [calm, recovered_win, recovered_loss]
    report = compare_signal_profiles(rows, ["balanced_v3_shadow"], min_move_pct=0.03)
    comparison = report["vix_policy_comparison"]
    current = comparison["balanced_v3_shadow_current"]
    penalty = comparison["balanced_v3_shadow_vix_penalty"]
    assert current["candidate_count"] == 1
    assert penalty["candidate_count"] == 3
    assert comparison["candidates_added_by_vix_penalty"] == 2
    assert comparison["correct_added_by_vix_penalty"] == 1
    assert comparison["incorrect_added_by_vix_penalty"] == 1
    assert comparison["vix_blocked_candidates_recovered"] == 2
    assert comparison["strict_correctness"] == "worsened"
    assert penalty["strict_correct_pct"] < current["strict_correct_pct"]
    assert report["profiles"]["balanced_v3_shadow"]["direction_sign_failures"] == 0
    assert report["orders_submitted"] == 0
    assert report["writes_to_database"] is False

    def loader(_settings, _run_ids, limit, session_dates=None):
        return rows[:limit]

    assert replay_script.run_replay(_shadow_settings(), rows_loader=loader, signal_profile="balanced_v3_shadow") == 0
    text = capsys.readouterr().out
    for label in (
        "balanced_v3_shadow_current",
        "balanced_v3_shadow_vix_penalty",
        "candidates_added_by_vix_penalty:",
        "correct_added_by_vix_penalty:",
        "incorrect_added_by_vix_penalty:",
        "vix_blocked_candidates_recovered:",
        "strict_correctness:",
    ):
        assert label in text


def test_report_includes_vix_diagnostics(capsys):
    rows = [
        {
            "decision": "skip",
            "skip_reason": "high_volatility",
            "bullish_score": 88.0,
            "bearish_score": 10.0,
            "confidence_score": 0,
            "vix_last": 16.0,
            "vix_change_pct": 5.8,
            "submitted": False,
            "production_execution_blocked": True,
            "freshness_gate_passed": True,
            "run_id": "shadow-vix",
            "cycle_number": 2,
        },
        {
            "decision": "bearish",
            "selected_symbol": "TZA",
            "skip_reason": None,
            "bullish_score": 20.0,
            "bearish_score": 74.0,
            "confidence_score": 74,
            "vix_last": 15.9,
            "vix_change_pct": 6.1,
            "vix_diagnostic": VIX_DIAGNOSTIC_PENALTY,
            "profile_scores": {"vix_diagnostic": VIX_DIAGNOSTIC_PENALTY, "candidate_score": 74.0},
            "submitted": False,
            "production_execution_blocked": True,
            "freshness_gate_passed": True,
            "direction_was_correct": True,
            "followup_seconds": 60,
            "selected_symbol_move_pct": 0.2,
            "iwm_move_pct": -0.2,
            "run_id": "shadow-vix",
            "cycle_number": 4,
        },
    ]
    report = summarize_shadow_logs(rows)
    assert report["high_volatility_skip_count"] == 1
    assert report["vix_penalty_count"] == 1
    assert report["vix_hard_block_count"] == 1
    assert report["average_vix"] == pytest.approx(15.95, abs=0.01)
    assert report["max_vix"] == pytest.approx(16.0, abs=0.01)
    assert report["average_vix_change_pct"] == pytest.approx(5.95, abs=0.01)
    assert report["vix_blocked_examples"][0]["candidate_score"] == 88.0
    assert report_script.run_report(_shadow_settings(), rows_loader=lambda *_args: rows) == 0
    text = capsys.readouterr().out
    assert "high_volatility_skip_count: 1" in text
    assert "vix_penalty_count: 1" in text
    assert "vix_hard_block_count: 1" in text
    assert "average_vix:" in text
    assert "max_vix:" in text
    assert "average_vix_change_pct:" in text
    assert "score=88" in text


def test_balanced_profile_stays_shadow_only_and_routes_stay_blocked(monkeypatch):
    engine = CandidateEngineV3(mode="shadow")
    assert engine.ALLOWS_EXECUTION is False
    assert CandidateEngineV3Config().vix_policy == "penalty"
    with pytest.raises(ConfigurationError, match="shadow/replay only"):
        build_pre_submit_check(MagicMock(), engine=engine, expected=SignalDirection.BULLISH, provider=MagicMock())
    for module in (v3_mod, profile_replay):
        source = inspect.getsource(module)
        for token in _FORBIDDEN:
            assert token not in source
    for module in (execution_router, app_factory):
        assert "vix_penalty_applied" not in inspect.getsource(module)
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
