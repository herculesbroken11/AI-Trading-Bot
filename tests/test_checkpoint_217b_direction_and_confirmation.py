"""Checkpoint 2.17B — balanced_v3_shadow direction sign, persistence, and flip cooldown."""

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
from backend.market_data.stream_models import SymbolStreamState
from backend.risk.live_guard import LiveTradingBlockedError, assert_order_execution_allowed
from backend.risk.models import OrderIntent, RiskContext
from backend.shadow_mode.profile_replay import compare_signal_profiles
from backend.signals import SIGNAL_ENGINE_VERSION, MarketSnapshot, SignalDirection, SignalEngineConfig, SignalReason
from backend.signals.candidate_engine_v3 import CandidateEngineV3
from backend.signals.dxlink_signal_source import build_pre_submit_check
from backend.signals.profiles import build_shadow_engine
from backend.signals.tna_tza_signal_engine import TnaTzaSignalEngine
from scripts import replay_signal_engine_versions as replay_script
from tests.test_checkpoint_217a_balanced_shadow_profile import (
    NOW,
    _NO_ENV,
    _settings,
    _stored,
    q,
    snap,
    vix,
    window_bear,
    window_bull,
)

STABILITY = (
    "window_start_move",
    "window_mid_move",
    "window_final_move",
    "final_10s_move",
    "final_10s_reversal",
    "selected_etf_final_10s_move",
    "pair_final_10s_confirmation",
)


def _v3() -> CandidateEngineV3:
    return CandidateEngineV3(wall_clock=lambda: NOW, mode="shadow")


def marginal_bear():
    """Bearish score in [70, 80): real IWM decline, pair confirms, not strong enough to flip."""
    iwm = 220.0 * (1.0 - 0.00045)
    return [
        q("TNA", 40.0 * (1.0 - 0.0004), open_=41.0, prev=41.0, first=40.0, high=40.0, low=40.0 * (1.0 - 0.0004)),
        q("TZA", 10.0 * 1.0008, open_=10.0, prev=10.0, first=10.0, high=10.0 * 1.0008, low=10.0),
        q("IWM", iwm, open_=220.0, prev=220.0, first=220.0, high=220.0, low=iwm),
        q("SPY", 500.0, open_=500.0, prev=500.0, first=500.0, high=500.0, low=500.0),
        q("QQQ", 400.0, open_=400.0, prev=400.0, first=400.0, high=400.0, low=400.0),
        vix(16.0, 16.2),
    ]


def stronger_bear():
    quotes = marginal_bear()
    iwm = 220.0 * (1.0 - 0.0010)
    quotes[2] = q("IWM", iwm, open_=220.0, prev=220.0, first=220.0, high=220.0, low=iwm)
    return quotes


def positive_iwm_inverse_trap():
    """IWM is up. TZA rising and TNA falling must not override that sign."""
    return [
        q("TNA", 44.5, open_=45.0, prev=45.0, first=45.2, high=45.2, low=44.5),
        q("TZA", 10.3, open_=10.0, prev=10.0, first=10.0, high=10.3, low=10.0),
        q("IWM", 220.44, open_=220.0, prev=220.0, first=220.0, high=220.44, low=220.0),
        q("SPY", 500.0, open_=500.0, prev=500.0, first=500.0, high=500.0, low=500.0),
        q("QQQ", 400.0, open_=400.0, prev=400.0, first=400.0, high=400.0, low=400.0),
        vix(16.0, 16.2),
    ]


def negative_iwm_inverse_trap():
    return [
        q("TNA", 45.3, open_=45.0, prev=45.0, first=45.0, high=45.3, low=45.0),
        q("TZA", 9.7, open_=10.0, prev=10.0, first=10.2, high=10.2, low=9.7),
        q("IWM", 219.56, open_=220.0, prev=220.0, first=220.0, high=220.0, low=219.56),
        q("SPY", 500.0, open_=500.0, prev=500.0, first=500.0, high=500.0, low=500.0),
        q("QQQ", 400.0, open_=400.0, prev=400.0, first=400.0, high=400.0, low=400.0),
        vix(16.0, 16.2),
    ]


def late_reversal_bull():
    """Full window is still up, but the last 10 seconds give back more than 0.03%."""
    iwm = replace(
        q("IWM", 219.60, open_=218.0, prev=217.0, first=219.0, high=219.70, low=219.0),
        mid_path=((0.0, 219.0), (8.0, 219.40), (15.0, 219.55), (20.0, 219.70), (30.0, 219.60)),
    )
    quotes = window_bull()
    quotes[2] = iwm
    return quotes


def persistent_bull():
    iwm = replace(
        q("IWM", 219.80, open_=218.0, prev=217.0, first=219.0, high=219.80, low=219.0),
        mid_path=((0.0, 219.0), (8.0, 219.25), (15.0, 219.50), (20.0, 219.65), (30.0, 219.80)),
    )
    tna = replace(
        q("TNA", 45.0, open_=43.0, prev=42.5, first=44.4, high=45.0, low=44.4),
        mid_path=((0.0, 44.4), (15.0, 44.7), (20.0, 44.85), (30.0, 45.0)),
    )
    tza = replace(
        q("TZA", 10.0, open_=10.5, prev=10.6, first=10.12, high=10.12, low=10.0),
        mid_path=((0.0, 10.12), (15.0, 10.06), (20.0, 10.03), (30.0, 10.0)),
    )
    quotes = window_bull()
    quotes[0] = tna
    quotes[1] = tza
    quotes[2] = iwm
    return quotes


def test_tna_explanation_uses_positive_raw_iwm_window():
    decision = _v3().decide(snap(*window_bull()))
    assert decision.decision is SignalDirection.BULLISH
    assert decision.selected_symbol == "TNA"
    assert "IWM window +" in decision.explanation
    assert decision.profile_scores["window_final_move"] > 0


def test_tza_explanation_uses_negative_raw_iwm_window():
    decision = _v3().decide(snap(*window_bear()))
    assert decision.decision is SignalDirection.BEARISH
    assert decision.selected_symbol == "TZA"
    assert "IWM window -" in decision.explanation
    assert "IWM window +" not in decision.explanation
    assert decision.profile_scores["window_final_move"] < 0


def test_positive_iwm_cannot_select_tza():
    decision = _v3().decide(snap(*positive_iwm_inverse_trap()))
    assert decision.decision is not SignalDirection.BEARISH
    assert decision.selected_symbol != "TZA"
    assert decision.decision is SignalDirection.SKIP


def test_negative_iwm_cannot_select_tna():
    decision = _v3().decide(snap(*negative_iwm_inverse_trap()))
    assert decision.decision is not SignalDirection.BULLISH
    assert decision.selected_symbol != "TNA"
    assert decision.decision is SignalDirection.SKIP


def test_missing_path_does_not_block_a_confirmed_candidate():
    decision = _v3().decide(snap(*window_bull()))
    assert decision.decision is SignalDirection.BULLISH
    assert decision.skip_reason is not SignalReason.LATE_REVERSAL_RISK
    for name in STABILITY:
        assert name in decision.profile_scores


def test_late_reversal_converts_candidate_to_skip():
    engine = _v3()
    snapshot = snap(*late_reversal_bull())
    unfiltered = engine.decide(snapshot, apply_path_filters=False)
    assert unfiltered.decision is SignalDirection.BULLISH
    assert unfiltered.selected_symbol == "TNA"
    decision = engine.decide(snapshot)
    assert decision.decision is SignalDirection.SKIP
    assert decision.skip_reason is SignalReason.LATE_REVERSAL_RISK
    assert decision.profile_scores["final_10s_reversal"] is True
    assert decision.profile_scores["final_10s_move"] < -0.03
    assert decision.profile_scores["window_final_move"] > 0


def test_persistent_path_keeps_the_candidate_and_reports_diagnostics():
    decision = _v3().decide(snap(*persistent_bull()))
    assert decision.decision is SignalDirection.BULLISH
    assert decision.selected_symbol == "TNA"
    scores = decision.profile_scores
    assert scores["window_start_move"] > 0
    assert scores["window_mid_move"] > 0
    assert scores["window_final_move"] > 0
    assert scores["final_10s_move"] > 0
    assert scores["final_10s_reversal"] is False
    assert scores["selected_etf_final_10s_move"] > 0
    assert scores["pair_final_10s_confirmation"] is True


def test_direction_flip_requires_a_stronger_score():
    engine = _v3()
    bull = engine.decide(snap(*window_bull()))
    assert bull.selected_symbol == "TNA"
    engine.note_shadow_cycle(bull)
    flipped = engine.decide(snap(*marginal_bear()))
    assert flipped.decision is SignalDirection.SKIP
    assert flipped.skip_reason is SignalReason.DIRECTION_FLIP_COOLDOWN
    assert flipped.profile_scores["candidate_score"] < 80
    fresh = _v3().decide(snap(*marginal_bear()))
    assert fresh.decision is SignalDirection.BEARISH
    assert fresh.selected_symbol == "TZA"
    assert 70 <= fresh.profile_scores["candidate_score"] < 80
    stronger = engine.decide(snap(*stronger_bear()))
    assert stronger.decision is SignalDirection.BEARISH
    assert stronger.selected_symbol == "TZA"
    assert stronger.profile_scores["candidate_score"] >= 80


def test_explicit_no_previous_direction_does_not_apply_cooldown():
    engine = _v3()
    engine.note_shadow_cycle(engine.decide(snap(*window_bull())))
    decision = engine.decide(snap(*marginal_bear()), previous_direction=None)
    assert decision.selected_symbol == "TZA"


def test_stream_records_mid_path_on_the_snapshot():
    sym = SymbolStreamState("IWM")
    for i, (bid, ask) in enumerate([(218.99, 219.01), (219.39, 219.41), (219.59, 219.61)]):
        sym.apply({"eventType": "Quote", "bidPrice": bid, "askPrice": ask}, received_at=float(i * 5), wall_time=NOW)
    snapshot = MarketSnapshot.from_stream_state(
        type("State", (), {"symbols": {"IWM": sym}})(),
        now=10.0,
        created_at=NOW,
    )
    path = snapshot.get("IWM").mid_path
    assert path is not None and len(path) == 3
    rebuilt = MarketSnapshot.from_dict(snapshot.to_dict())
    assert rebuilt.get("IWM").mid_path[1][0] == pytest.approx(5.0)


def test_default_conservative_v2_is_unchanged():
    clock = lambda: NOW
    settings = _settings()
    direct = TnaTzaSignalEngine(SignalEngineConfig.from_settings(settings), wall_clock=clock)
    built = build_shadow_engine("conservative_v2", settings, wall_clock=clock)
    for quotes in (window_bull(), window_bear(), marginal_bear()):
        assert built.decide(snap(*quotes)).to_dict() == direct.decide(snap(*quotes)).to_dict()
    assert built.decide(snap(*window_bull())).engine_version == SIGNAL_ENGINE_VERSION
    assert not hasattr(built, "note_shadow_cycle")


def test_balanced_v3_shadow_cannot_submit_orders():
    engine = _v3()
    assert engine.IS_SHADOW_ONLY is True
    assert engine.ALLOWS_EXECUTION is False
    with pytest.raises(ConfigurationError, match="cannot authorize orders"):
        CandidateEngineV3(mode="live")
    with pytest.raises(ConfigurationError, match="shadow/replay only"):
        build_pre_submit_check(
            MagicMock(),
            engine=engine,
            expected=SignalDirection.BULLISH,
            provider=MagicMock(),
        )


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


def _filter_rows():
    run = "shadow-20261008-filters"
    return [
        _stored(snap(*window_bull()), run_id=run, cycle=1, iwm_after_pct=0.12, created="2026-10-08T13:45:00"),
        _stored(snap(*marginal_bear()), run_id=run, cycle=2, iwm_after_pct=0.10, created="2026-10-08T13:47:00"),
        _stored(snap(*late_reversal_bull()), run_id=run, cycle=3, iwm_after_pct=-0.08, created="2026-10-08T13:49:00"),
    ]


def test_replay_reports_before_and_after_confirmation_filters(capsys):
    rows = _filter_rows()
    report = compare_signal_profiles(rows, ["balanced_v3_shadow"], min_move_pct=0.03)
    stats = report["profiles"]["balanced_v3_shadow"]
    assert stats["candidates_before_filters"] == 3
    assert stats["candidates_after_filters"] == 1
    assert stats["false_candidates_filtered"] == 2
    assert stats["good_candidates_preserved"] == 1
    assert stats["missed_winners"] == 0
    assert stats["direction_sign_failures"] == 0
    assert stats["late_reversal_risk_count"] == 1
    assert stats["direction_flip_cooldown_count"] == 1
    assert stats["orders_submitted"] == 0
    assert report["applied_to_live"] is False

    def loader(_settings, _run_ids, limit, session_dates=None):
        return rows[:limit]

    assert replay_script.run_replay(_settings(), rows_loader=loader, signal_profile="balanced_v3_shadow") == 0
    out = capsys.readouterr().out
    for text in (
        "candidates_before_filters: 3",
        "candidates_after_filters: 1",
        "false_candidates_filtered: 2",
        "good_candidates_preserved: 1",
        "missed_winners: 0",
        "direction_sign_failures: 0",
        "late_reversal_risk_count: 1",
        "direction_flip_cooldown_count: 1",
    ):
        assert text in out, text
    assert replay_script.run_replay(_settings(), rows_loader=loader) == 0
    assert "balanced_v3_shadow" not in capsys.readouterr().out
