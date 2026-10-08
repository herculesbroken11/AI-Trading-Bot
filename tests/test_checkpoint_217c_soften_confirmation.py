"""Checkpoint 2.17C — late reversal only on a real final-10s reverse, softer IWM tiers, adaptive entry."""

from __future__ import annotations

import inspect
from dataclasses import replace
from unittest.mock import MagicMock

import pytest

import backend.app_factory as app_factory
import backend.bot_worker.sandbox_worker as sandbox_worker
import backend.execution.execution_router as execution_router
import backend.signals.candidate_engine_v3 as v3_mod
import backend.shadow_mode.profile_replay as profile_replay
import scripts.run_sandbox_bot_cycle as sandbox_cycle
from backend.config.settings import ConfigurationError, Settings, load_settings
from backend.execution.execution_router import ExecutionRouter
from backend.risk.live_guard import LiveTradingBlockedError, assert_order_execution_allowed
from backend.risk.models import OrderIntent, RiskContext
from backend.shadow_mode.profile_replay import compare_signal_profiles
from backend.signals import SIGNAL_ENGINE_VERSION, SignalDirection, SignalEngineConfig, SignalReason
from backend.signals.candidate_engine_v3 import CandidateEngineV3
from backend.signals.dxlink_signal_source import build_pre_submit_check
from backend.signals.profiles import build_shadow_engine
from backend.signals.tna_tza_signal_engine import TnaTzaSignalEngine
from scripts import replay_signal_engine_versions as replay_script
from tests.test_checkpoint_217a_balanced_shadow_profile import NOW, _NO_ENV, _settings, _stored, q, snap, vix
from tests.test_checkpoint_217b_direction_and_confirmation import (
    late_reversal_bull,
    negative_iwm_inverse_trap,
    positive_iwm_inverse_trap,
)


def _v3() -> CandidateEngineV3:
    return CandidateEngineV3(wall_clock=lambda: NOW, mode="shadow")


def _px(first: float, pct: float) -> float:
    return first * (1.0 + pct / 100.0)


def _book(symbol: str, first: float, pct: float, *, open_=None, prev=None):
    mid = _px(first, pct)
    low, high = (mid, first) if mid <= first else (first, mid)
    return q(symbol, mid, open_=open_ if open_ is not None else first, prev=prev if prev is not None else first, first=first, high=high, low=low)


def moderate_strong_bull():
    """IWM +0.02% with a strong pair and broad window. Score clears 70."""
    return [
        _book("TNA", 40.0, 0.20, open_=39.0, prev=39.0),
        _book("TZA", 10.0, -0.10, open_=10.2, prev=10.2),
        _book("IWM", 220.0, 0.02, open_=219.0, prev=219.0),
        _book("SPY", 500.0, 0.04),
        _book("QQQ", 400.0, 0.04),
        vix(16.0, 16.2),
    ]


def adaptive_band_bull():
    """IWM +0.02%, strong pair, flat SPY/QQQ. Score sits in [65, 70)."""
    return [
        _book("TNA", 40.0, 0.14, open_=39.0, prev=39.0),
        _book("TZA", 10.0, -0.04, open_=10.2, prev=10.2),
        _book("IWM", 220.0, 0.02, open_=219.0, prev=219.0),
        _book("SPY", 500.0, 0.0),
        _book("QQQ", 400.0, 0.0),
        vix(16.0, 16.2),
    ]


def adaptive_band_without_pair():
    """Same moderate IWM, but the inverse ETF is only flat, so pair score stays below the high tier."""
    return [
        _book("TNA", 40.0, 0.12, open_=39.0, prev=39.0),
        _book("TZA", 10.0, 0.0),
        _book("IWM", 220.0, 0.028, open_=219.0, prev=219.0),
        _book("SPY", 500.0, 0.04),
        _book("QQQ", 400.0, 0.04),
        vix(16.0, 16.2),
    ]


def weak_iwm_bull():
    return [
        _book("TNA", 40.0, 0.05, open_=39.0, prev=39.0),
        _book("TZA", 10.0, 0.0),
        _book("IWM", 220.0, 0.010, open_=219.0, prev=219.0),
        _book("SPY", 500.0, 0.0),
        _book("QQQ", 400.0, 0.0),
        vix(16.0, 16.2),
    ]


def supporting_bear():
    """Observed pattern: IWM mid slightly negative, final 10s still down, TZA final 10s up."""
    iwm = replace(
        _book("IWM", 220.0, (219.85 - 220.0) / 220.0 * 100.0, open_=221.0, prev=221.0),
        mid_path=((0.0, 220.0), (8.0, 219.98), (15.0, 219.96), (20.0, 219.93), (30.0, 219.85)),
    )
    tza = replace(
        _book("TZA", 10.0, 0.80, open_=9.8, prev=9.8),
        mid_path=((0.0, 10.0), (15.0, 10.03), (20.0, 10.04), (30.0, 10.08)),
    )
    return [
        _book("TNA", 40.0, -0.50, open_=41.0, prev=41.0),
        tza,
        iwm,
        _book("SPY", 500.0, 0.0),
        _book("QQQ", 400.0, 0.0),
        vix(16.0, 16.2),
    ]


def supporting_bull_weak_mid():
    """Final 10s still rises. A weak mid-window must not become late_reversal_risk."""
    iwm = replace(
        _book("IWM", 219.0, (219.50 - 219.0) / 219.0 * 100.0, open_=218.0, prev=217.0),
        mid_path=((0.0, 219.0), (8.0, 219.005), (15.0, 219.01), (20.0, 219.40), (30.0, 219.50)),
    )
    quotes = moderate_strong_bull()
    quotes[2] = iwm
    return quotes


def test_supporting_bearish_final_10s_is_not_late_reversal():
    decision = _v3().decide(snap(*supporting_bear()))
    assert decision.skip_reason is not SignalReason.LATE_REVERSAL_RISK
    assert decision.decision is SignalDirection.BEARISH
    assert decision.selected_symbol == "TZA"
    scores = decision.profile_scores
    assert scores["window_mid_move"] < 0
    assert scores["window_mid_move"] > -0.03
    assert scores["final_10s_move"] < 0
    assert scores["selected_etf_final_10s_move"] > 0
    assert scores["final_10s_reversal"] is False


def test_supporting_bullish_final_10s_is_not_late_reversal():
    decision = _v3().decide(snap(*supporting_bull_weak_mid()))
    assert decision.skip_reason is not SignalReason.LATE_REVERSAL_RISK
    assert decision.decision is SignalDirection.BULLISH
    assert decision.selected_symbol == "TNA"
    assert decision.profile_scores["final_10s_move"] > 0
    assert decision.profile_scores["final_10s_reversal"] is False


def test_clear_final_10s_reversal_still_skips():
    decision = _v3().decide(snap(*late_reversal_bull()))
    assert decision.decision is SignalDirection.SKIP
    assert decision.skip_reason is SignalReason.LATE_REVERSAL_RISK
    assert decision.profile_scores["final_10s_move"] < -0.03


def test_moderate_iwm_passes_when_the_pair_is_strong():
    decision = _v3().decide(snap(*moderate_strong_bull()))
    assert decision.decision is SignalDirection.BULLISH
    assert decision.selected_symbol == "TNA"
    scores = decision.profile_scores
    assert 0.015 <= scores["window_final_move"] < 0.03
    assert scores["pair_confirmation_score"] >= 22
    assert scores["candidate_score"] >= 70
    assert 0 < scores["iwm_momentum_score"] < 30


def test_weak_iwm_still_skips():
    decision = _v3().decide(snap(*weak_iwm_bull()))
    assert decision.decision is SignalDirection.SKIP
    assert decision.skip_reason is SignalReason.INSUFFICIENT_SIGNAL_STRENGTH
    assert decision.profile_scores["rejection_stage"] == "iwm_threshold"
    assert decision.profile_scores["window_final_move"] < 0.015


def test_negative_iwm_cannot_select_tna():
    decision = _v3().decide(snap(*negative_iwm_inverse_trap()))
    assert decision.decision is not SignalDirection.BULLISH
    assert decision.selected_symbol != "TNA"


def test_positive_iwm_cannot_select_tza():
    decision = _v3().decide(snap(*positive_iwm_inverse_trap()))
    assert decision.decision is not SignalDirection.BEARISH
    assert decision.selected_symbol != "TZA"


def test_adaptive_band_passes_only_with_strong_confirmation():
    confirmed = _v3().decide(snap(*adaptive_band_bull()))
    assert confirmed.decision is SignalDirection.BULLISH
    assert confirmed.selected_symbol == "TNA"
    assert confirmed.profile_scores["final_reason"] == "balanced_confirmed"
    assert 65 <= confirmed.profile_scores["candidate_score"] < 70
    assert confirmed.profile_scores["pair_confirmation_score"] >= 22

    rejected = _v3().decide(snap(*adaptive_band_without_pair()))
    assert rejected.decision is SignalDirection.SKIP
    assert rejected.skip_reason is SignalReason.INSUFFICIENT_SIGNAL_STRENGTH
    assert rejected.profile_scores["rejection_stage"] == "adaptive_entry"
    assert 65 <= rejected.profile_scores["candidate_score"] < 70
    assert rejected.profile_scores["pair_confirmation_score"] < 22


def test_default_conservative_v2_is_unchanged():
    clock = lambda: NOW
    settings = _settings()
    direct = TnaTzaSignalEngine(SignalEngineConfig.from_settings(settings), wall_clock=clock)
    built = build_shadow_engine("conservative_v2", settings, wall_clock=clock)
    snapshot = snap(*moderate_strong_bull())
    assert built.decide(snapshot).to_dict() == direct.decide(snapshot).to_dict()
    assert built.decide(snapshot).engine_version == SIGNAL_ENGINE_VERSION


def test_balanced_stays_shadow_only():
    engine = _v3()
    assert engine.IS_SHADOW_ONLY is True
    assert engine.ALLOWS_EXECUTION is False
    with pytest.raises(ConfigurationError, match="cannot authorize orders"):
        CandidateEngineV3(mode="live")
    with pytest.raises(ConfigurationError, match="shadow/replay only"):
        build_pre_submit_check(MagicMock(), engine=engine, expected=SignalDirection.BULLISH, provider=MagicMock())


_FORBIDDEN = (
    "order_executor",
    "OrderExecutor",
    "execution_router",
    "ExecutionRouter",
    "tastytrade_sandbox",
    "sandbox_worker",
    "submit_equity_order",
    "execute_order",
    "dry_run_equity_order",
)


@pytest.mark.parametrize("module", [v3_mod, profile_replay])
def test_confirmation_modules_do_not_call_order_execution(module):
    source = inspect.getsource(module)
    for token in _FORBIDDEN:
        assert token not in source, token


def test_execution_entrypoints_do_not_import_v3():
    for module in (sandbox_worker, sandbox_cycle, execution_router, app_factory):
        source = inspect.getsource(module)
        assert "candidate_engine_v3" not in source
        assert "balanced_v3_shadow" not in source


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


def test_replay_prints_rejection_and_correct_pct(capsys):
    run = "shadow-20261009-filters"
    rows = [
        _stored(snap(*moderate_strong_bull()), run_id=run, cycle=1, iwm_after_pct=0.08, created="2026-10-09T13:45:00"),
        _stored(snap(*weak_iwm_bull()), run_id=run, cycle=2, iwm_after_pct=0.0, created="2026-10-09T13:47:00"),
        _stored(snap(*adaptive_band_without_pair()), run_id=run, cycle=3, iwm_after_pct=0.0, created="2026-10-09T13:49:00"),
        _stored(snap(*late_reversal_bull()), run_id=run, cycle=4, iwm_after_pct=-0.08, created="2026-10-09T13:51:00"),
    ]
    report = compare_signal_profiles(rows, ["balanced_v3_shadow"], min_move_pct=0.03)
    stats = report["profiles"]["balanced_v3_shadow"]
    assert stats["candidates_before_filters"] >= 1
    assert stats["candidates_after_filters"] >= 1
    assert stats["candidates_rejected_by_iwm_threshold"] >= 1
    assert stats["candidates_rejected_by_late_reversal"] >= 1
    assert stats["candidates_rejected_by_adaptive_entry"] >= 1
    assert stats["direction_sign_failures"] == 0
    assert stats["orders_submitted"] == 0
    assert report["writes_to_database"] is False
    assert "correct_pct_before_filters" in stats
    assert "correct_pct_after_filters" in stats

    def loader(_settings, _run_ids, limit, session_dates=None):
        return rows[:limit]

    assert replay_script.run_replay(_settings(), rows_loader=loader, signal_profile="balanced_v3_shadow") == 0
    out = capsys.readouterr().out
    for text in (
        "candidates_before_filters:",
        "candidates_after_filters:",
        "candidates_rejected_by_iwm_threshold:",
        "candidates_rejected_by_late_reversal:",
        "candidates_rejected_by_adaptive_entry:",
        "false_candidates_filtered:",
        "good_candidates_preserved:",
        "missed_winners:",
        "correct_pct_before_filters:",
        "correct_pct_after_filters:",
    ):
        assert text in out, text
