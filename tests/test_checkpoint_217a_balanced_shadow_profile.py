"""Checkpoint 2.17A — balanced_v3_shadow candidates, shadow-only, v2 default unchanged."""

from __future__ import annotations

import inspect
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import backend.app_factory as app_factory
import backend.bot_worker.sandbox_worker as sandbox_worker
import backend.execution.execution_router as execution_router
import backend.execution.order_executor as order_executor
import backend.signals.candidate_engine_v3 as v3_mod
import backend.shadow_mode.profile_replay as profile_replay
import scripts.run_sandbox_bot_cycle as sandbox_cycle
from backend.config.settings import ConfigurationError, Settings, load_settings
from backend.execution.execution_router import ExecutionRouter
from backend.risk.live_guard import LiveTradingBlockedError, assert_order_execution_allowed
from backend.risk.models import OrderIntent, RiskContext
from backend.shadow_mode.profile_replay import compare_signal_profiles, replay_profile_row
from backend.signals import (
    PROFILE_SCORE_FIELDS,
    SIGNAL_ENGINE_VERSION,
    MarketSnapshot,
    SignalDirection,
    SignalEngineConfig,
    SignalReason,
    SymbolQuote,
    TnaTzaSignalEngine,
    build_shadow_engine,
)
from backend.signals.candidate_engine_v3 import CandidateEngineV3, CandidateEngineV3Config
from backend.signals.dxlink_signal_source import build_pre_submit_check
from scripts import replay_signal_engine_versions as replay_script
from scripts import run_shadow_signal_logger as shadow_script
from tests.test_checkpoint_212_shadow_mode import BULL, _run_cli, _settings as _shadow_settings, _stream

_NO_ENV = Path("/nonexistent/.env")
NOW = datetime(2026, 10, 6, 14, 30, tzinfo=timezone.utc)


def _settings(**overrides) -> Settings:
    base = dict(
        trading_mode="sandbox",
        tastytrade_env="sandbox",
        live_trading_enabled=False,
        signal_profile="conservative_v2",
    )
    base.update(overrides)
    return Settings(**base)


def q(symbol, mid, *, open_=None, prev=None, first=None, high=None, low=None, age=0.2) -> SymbolQuote:
    return SymbolQuote(
        symbol=symbol,
        bid=round(mid - 0.01, 4),
        ask=round(mid + 0.01, 4),
        day_open=open_,
        prev_close=prev,
        first_mid=first,
        window_high_mid=high,
        window_low_mid=low,
        quote_age_seconds=age,
        quote_updates=10,
    )


def vix(level, prev, *, age=5.0) -> SymbolQuote:
    return SymbolQuote(
        symbol="VIX", last=level, prev_close=prev, quote_age_seconds=age, quote_updates=3, diagnostic_only=True
    )


def snap(*quotes: SymbolQuote) -> MarketSnapshot:
    return MarketSnapshot(quotes={item.symbol: item for item in quotes}, created_at=NOW)


def _v3(**overrides) -> CandidateEngineV3:
    config = CandidateEngineV3Config(**overrides) if overrides else None
    return CandidateEngineV3(config, wall_clock=lambda: NOW, mode="shadow")


def window_bull():
    """IWM and TNA/TZA confirm up in-window. SPY/QQQ are down versus the open and previous close."""
    return [
        q("TNA", 45.0, open_=43.0, prev=42.5, first=44.4, high=45.0, low=44.4),
        q("TZA", 10.0, open_=10.5, prev=10.6, first=10.12, high=10.12, low=10.0),
        q("IWM", 220.0, open_=218.0, prev=217.0, first=219.0, high=220.0, low=219.0),
        q("SPY", 579.05, open_=580.0, prev=582.0, first=579.0, high=579.05, low=579.0),
        q("QQQ", 498.90, open_=500.0, prev=502.0, first=499.0, high=499.0, low=498.90),
        vix(16.0, 16.2),
    ]


def window_bear():
    """IWM and TNA/TZA confirm down in-window. SPY/QQQ are up versus the open and previous close."""
    return [
        q("TNA", 41.0, open_=43.0, prev=43.5, first=41.6, high=41.6, low=41.0),
        q("TZA", 11.0, open_=10.5, prev=10.4, first=10.85, high=11.0, low=10.85),
        q("IWM", 216.0, open_=218.0, prev=219.0, first=217.0, high=217.0, low=216.0),
        q("SPY", 575.2, open_=570.0, prev=569.0, first=575.3, high=575.3, low=575.2),
        q("QQQ", 500.1, open_=496.0, prev=495.0, first=500.2, high=500.2, low=500.1),
        vix(16.0, 16.2),
    ]


def weak_quotes():
    return [
        q("TNA", 45.02, open_=44.0, prev=44.0, first=45.0, high=45.02, low=45.0),
        q("TZA", 10.0, open_=10.0, prev=10.0, first=10.0, high=10.0, low=10.0),
        q("IWM", 220.02, open_=219.0, prev=219.0, first=220.0, high=220.02, low=220.0),
        q("SPY", 580.0, open_=579.0, prev=579.0, first=580.0, high=580.0, low=580.0),
        q("QQQ", 500.0, open_=499.0, prev=499.0, first=500.0, high=500.0, low=500.0),
        vix(16.0, 16.0),
    ]


def opposed_bull():
    quotes = window_bull()
    quotes[3] = q("SPY", 578.3, open_=580.0, prev=582.0, first=579.2, high=579.2, low=578.3)
    quotes[4] = q("QQQ", 498.2, open_=500.0, prev=502.0, first=499.2, high=499.2, low=498.2)
    return quotes


def mixed_broad_bull():
    quotes = window_bull()
    quotes[3] = q("SPY", 579.29, open_=580.0, prev=582.0, first=579.0, high=579.29, low=579.0)
    quotes[4] = q("QQQ", 498.90, open_=500.0, prev=502.0, first=499.0, high=499.0, low=498.90)
    return quotes


def fading_bull():
    quotes = window_bull()
    quotes[2] = q("IWM", 219.5, open_=218.0, prev=217.0, first=219.0, high=220.5, low=219.0)
    return quotes


def choppy_bull():
    quotes = window_bull()
    quotes[2] = q("IWM", 219.2, open_=218.0, prev=217.0, first=219.0, high=219.3, low=218.0)
    return quotes


def _assert_score_fields(decision):
    scores = decision.profile_scores
    assert scores is not None
    assert set(PROFILE_SCORE_FIELDS) <= set(scores)
    assert scores["profile"] == "balanced_v3_shadow"


# ---------------------------------------------------------------------------
# Profile config and unchanged v2
# ---------------------------------------------------------------------------


def test_default_signal_profile_is_conservative_v2(monkeypatch):
    monkeypatch.delenv("SIGNAL_PROFILE", raising=False)
    settings = load_settings(env_path=_NO_ENV)
    assert settings.signal_profile == "conservative_v2"
    assert settings.safe_summary()["signal_profile"] == "conservative_v2"


def test_invalid_signal_profile_is_rejected(monkeypatch):
    monkeypatch.setenv("SIGNAL_PROFILE", "loose_v9")
    with pytest.raises(ConfigurationError, match="SIGNAL_PROFILE"):
        load_settings(env_path=_NO_ENV)


def test_balanced_profile_in_settings_does_not_change_v2_engine(monkeypatch):
    monkeypatch.setenv("SIGNAL_PROFILE", "balanced_v3_shadow")
    settings = load_settings(env_path=_NO_ENV)
    assert settings.signal_profile == "balanced_v3_shadow"
    clock = lambda: NOW
    engine = TnaTzaSignalEngine(SignalEngineConfig.from_settings(settings), wall_clock=clock)
    decision = engine.decide(snap(*window_bull()))
    assert decision.engine_version == SIGNAL_ENGINE_VERSION
    assert decision.profile_scores is None
    assert decision.decision is SignalDirection.SKIP
    assert decision.skip_reason is SignalReason.BROAD_MARKET_DISAGREEMENT


def test_conservative_v2_factory_matches_current_engine():
    clock = lambda: NOW
    settings = _settings()
    direct = TnaTzaSignalEngine(
        SignalEngineConfig.from_settings(settings, max_quote_age_seconds=1.0), wall_clock=clock
    )
    built = build_shadow_engine("conservative_v2", settings, max_quote_age_seconds=1.0, wall_clock=clock)
    assert type(built) is TnaTzaSignalEngine
    for quotes in (window_bull(), window_bear(), weak_quotes(), opposed_bull(), fading_bull()):
        snapshot = snap(*quotes)
        assert built.decide(snapshot).to_dict() == direct.decide(snapshot).to_dict()


def test_v2_entry_threshold_stays_70():
    config = SignalEngineConfig()
    assert (config.entry_score_threshold, config.opposing_score_max, config.min_score_gap) == (70.0, 40.0, 20.0)
    with pytest.raises(ConfigurationError):
        CandidateEngineV3Config(entry_score_threshold=50).validate()


# ---------------------------------------------------------------------------
# balanced_v3_shadow candidates
# ---------------------------------------------------------------------------


def test_balanced_profile_emits_tna_when_iwm_and_pair_confirm():
    decision = _v3().decide(snap(*window_bull()))
    _assert_score_fields(decision)
    assert decision.decision is SignalDirection.BULLISH
    assert decision.selected_symbol == "TNA"
    assert decision.skip_reason is None
    assert decision.freshness_gate_passed is True
    scores = decision.profile_scores
    assert scores["direction"] == "bullish"
    assert scores["final_reason"] == "bullish_confirmed"
    assert scores["candidate_score"] >= 70
    assert scores["iwm_momentum_score"] == 30.0
    assert scores["pair_confirmation_score"] == 25.0
    assert scores["chop_risk_score"] == 0.0
    assert scores["pullback_risk_score"] == 0.0
    assert "day open" not in decision.explanation.lower()


def test_balanced_profile_ignores_spy_qqq_session_level_disagreement():
    bullish_levels = window_bull()
    bullish_levels[3] = q("SPY", 579.05, open_=570.0, prev=569.0, first=579.0, high=579.05, low=579.0)
    bullish_levels[4] = q("QQQ", 498.90, open_=490.0, prev=489.0, first=499.0, high=499.0, low=498.90)
    bearish_levels = window_bull()
    first = _v3().decide(snap(*window_bull()))
    second = _v3().decide(snap(*bullish_levels))
    third = _v3().decide(snap(*bearish_levels))
    assert first.selected_symbol == second.selected_symbol == third.selected_symbol == "TNA"
    assert TnaTzaSignalEngine(wall_clock=lambda: NOW).decide(snap(*window_bull())).skip_reason is (
        SignalReason.BROAD_MARKET_DISAGREEMENT
    )


def test_mixed_spy_qqq_window_still_allows_strong_pair():
    decision = _v3().decide(snap(*mixed_broad_bull()))
    assert decision.selected_symbol == "TNA"
    assert decision.profile_scores["candidate_score"] >= 70


def test_balanced_profile_emits_tza_when_iwm_and_pair_confirm():
    decision = _v3().decide(snap(*window_bear()))
    _assert_score_fields(decision)
    assert decision.decision is SignalDirection.BEARISH
    assert decision.selected_symbol == "TZA"
    assert decision.profile_scores["direction"] == "bearish"
    assert decision.profile_scores["final_reason"] == "bearish_confirmed"
    assert decision.profile_scores["candidate_score"] >= 70
    assert decision.profile_scores["iwm_momentum_score"] == 30.0
    assert decision.profile_scores["pair_confirmation_score"] == 25.0
    assert TnaTzaSignalEngine(wall_clock=lambda: NOW).decide(snap(*window_bear())).decision is SignalDirection.SKIP


def test_weak_movement_still_skips():
    decision = _v3().decide(snap(*weak_quotes()))
    _assert_score_fields(decision)
    assert decision.decision is SignalDirection.SKIP
    assert decision.selected_symbol is None
    assert decision.skip_reason is SignalReason.INSUFFICIENT_SIGNAL_STRENGTH
    assert decision.profile_scores["candidate_score"] < 70
    assert decision.profile_scores["final_reason"] == "insufficient_signal_strength"


def test_stale_data_still_skips():
    quotes = window_bull()
    quotes[2] = q("IWM", 220.0, open_=218.0, prev=217.0, first=219.0, high=220.0, low=219.0, age=3.0)
    decision = _v3().decide(snap(*quotes))
    _assert_score_fields(decision)
    assert decision.decision is SignalDirection.SKIP
    assert decision.freshness_gate_passed is False
    assert decision.skip_reason is SignalReason.STALE_MARKET_DATA
    assert decision.profile_scores["final_reason"] == "stale_market_data"
    assert decision.profile_scores["candidate_score"] == 0.0


def test_strong_spy_qqq_window_opposition_skips():
    decision = _v3().decide(snap(*opposed_bull()))
    _assert_score_fields(decision)
    assert decision.decision is SignalDirection.SKIP
    assert decision.skip_reason is SignalReason.BROAD_MARKET_DISAGREEMENT
    assert decision.profile_scores["final_reason"] == "broad_market_disagreement"
    assert "in-window" in decision.explanation


def test_single_index_very_strong_opposition_skips():
    quotes = window_bull()
    quotes[4] = q("QQQ", 498.0, open_=500.0, prev=502.0, first=499.2, high=499.2, low=498.0)
    decision = _v3().decide(snap(*quotes))
    assert decision.decision is SignalDirection.SKIP
    assert decision.skip_reason is SignalReason.BROAD_MARKET_DISAGREEMENT


def test_pullback_and_severe_chop_skip():
    pullback = _v3().decide(snap(*fading_bull()))
    assert pullback.decision is SignalDirection.SKIP
    assert pullback.skip_reason is SignalReason.PULLBACK_RISK
    assert pullback.profile_scores["pullback_risk_score"] >= 40

    chop = _v3().decide(snap(*choppy_bull()))
    assert chop.decision is SignalDirection.SKIP
    assert chop.skip_reason is SignalReason.CHOPPY_CONFIRMATION
    assert chop.profile_scores["chop_risk_score"] >= 60


def test_v3_refuses_non_shadow_modes_and_loosened_entry():
    with pytest.raises(ConfigurationError, match="shadow or replay"):
        CandidateEngineV3(mode="live")
    engine = CandidateEngineV3(mode="replay", wall_clock=lambda: NOW)
    assert engine.IS_SHADOW_ONLY is True
    assert engine.ALLOWS_EXECUTION is False
    assert engine.decide(snap(*window_bull())).selected_symbol == "TNA"


# ---------------------------------------------------------------------------
# Shadow logger wiring
# ---------------------------------------------------------------------------


def test_shadow_logger_defaults_to_conservative_v2(capsys):
    code, out, _, _ = _run_cli(capsys, [_stream(BULL)], cycles=1, json_output=True)
    assert code == 0
    doc = json.loads(out)
    assert doc["signal_profile"] == "conservative_v2"
    assert doc["engine_version"] == SIGNAL_ENGINE_VERSION
    assert doc["cycles"][0]["decision"] == "bullish"
    assert doc["cycles"][0].get("profile_scores") is None
    assert doc["orders_submitted"] == 0
    code, text, _, _ = _run_cli(capsys, [_stream(BULL)], cycles=1)
    assert code == 0
    assert "signal_profile: conservative_v2" in text
    assert "shadow mode never submits orders" in text


def test_shadow_logger_balanced_profile_logs_scores_and_submits_nothing(capsys, monkeypatch):
    def explode(*_args, **_kwargs):
        raise AssertionError("order execution must not be constructed")

    monkeypatch.setattr(order_executor.OrderExecutor, "__init__", explode)
    monkeypatch.setattr(execution_router.ExecutionRouter, "route", explode)
    code, out, _, _ = _run_cli(
        capsys, [_stream(BULL)], cycles=1, signal_profile="balanced_v3_shadow"
    )
    assert code == 0
    assert "shadow mode never submits orders" in out
    assert "submitted: false (shadow mode never submits orders)" in out
    assert "candidate_score=" in out
    assert "final_reason: bullish_confirmed" in out
    code, out, _, _ = _run_cli(
        capsys, [_stream(BULL)], cycles=1, json_output=True, signal_profile="balanced_v3_shadow"
    )
    assert code == 0
    doc = json.loads(out)
    assert doc["signal_profile"] == "balanced_v3_shadow"
    assert doc["engine_version"] == "balanced_v3_shadow"
    assert doc["shadow_only"] is True
    assert doc["orders_submitted"] == 0
    assert doc["run"]["orders_submitted"] == 0
    cycle = doc["cycles"][0]
    assert cycle["decision"] == "bullish"
    assert cycle["selected_symbol"] == "TNA"
    assert cycle["submitted"] is False
    scores = cycle["profile_scores"]
    assert set(PROFILE_SCORE_FIELDS) <= set(scores)
    assert scores["candidate_score"] >= 70
    assert scores["final_reason"] == "bullish_confirmed"
    assert cycle["submitted"] is False


def test_explicit_conservative_flag_overrides_balanced_settings(capsys):
    code, out, _, _ = _run_cli(
        capsys,
        [_stream(BULL)],
        cycles=1,
        json_output=True,
        settings=_shadow_settings(signal_profile="balanced_v3_shadow"),
        signal_profile="conservative_v2",
    )
    assert code == 0
    assert json.loads(out)["signal_profile"] == "conservative_v2"


# ---------------------------------------------------------------------------
# Replay comparison
# ---------------------------------------------------------------------------


def _stored(snapshot: MarketSnapshot, *, run_id: str, cycle: int, iwm_after_pct: float, created: str):
    mids = {symbol: snapshot.get(symbol).mid for symbol in ("TNA", "TZA", "IWM", "SPY", "QQQ")}

    def moved(symbol: str, pct: float) -> float:
        return mids[symbol] * (1.0 + pct / 100.0)

    return {
        "run_id": run_id,
        "cycle_number": cycle,
        "created_at": created,
        "decision": "skip",
        "selected_symbol": None,
        "skip_reason": "insufficient_signal_strength",
        "raw_snapshot_json": json.dumps(snapshot.to_dict()),
        "raw_score_json": json.dumps({"thresholds": {"max_quote_age_seconds": 1.0}}),
        "followup_seconds": 60,
        "tna_mid": mids["TNA"],
        "tza_mid": mids["TZA"],
        "iwm_mid": mids["IWM"],
        "spy_mid": mids["SPY"],
        "qqq_mid": mids["QQQ"],
        "tna_mid_after": moved("TNA", iwm_after_pct),
        "tza_mid_after": moved("TZA", -iwm_after_pct),
        "iwm_mid_after": moved("IWM", iwm_after_pct),
        "spy_mid_after": mids["SPY"],
        "qqq_mid_after": mids["QQQ"],
    }


def _replay_rows():
    return [
        _stored(snap(*window_bull()), run_id="shadow-20261006T143000Z-aaa", cycle=1, iwm_after_pct=0.12, created="2026-10-06T14:30:00"),
        _stored(snap(*window_bear()), run_id="shadow-20261006T150000Z-bbb", cycle=2, iwm_after_pct=-0.12, created="2026-10-06T15:00:00"),
        _stored(snap(*weak_quotes()), run_id="shadow-20261007T143000Z-ccc", cycle=1, iwm_after_pct=0.0, created="2026-10-07T14:30:00"),
        _stored(snap(*opposed_bull()), run_id="shadow-20261007T144000Z-ddd", cycle=2, iwm_after_pct=0.0, created="2026-10-07T14:40:00"),
    ]


def test_profile_replay_counts_candidates_and_misses():
    rows = _replay_rows()
    before = json.dumps(rows, sort_keys=True)
    report = compare_signal_profiles(rows, ["conservative_v2", "balanced_v3_shadow"], min_move_pct=0.03)
    assert json.dumps(rows, sort_keys=True) == before
    assert report["orders_submitted"] == 0
    assert report["writes_to_database"] is False
    assert report["applied_to_live"] is False
    v2 = report["profiles"]["conservative_v2"]
    v3 = report["profiles"]["balanced_v3_shadow"]
    assert v2["candidate_count"] == 0
    assert v2["skip_count"] == 4
    assert v3["tna_count"] == 1
    assert v3["tza_count"] == 1
    assert v3["skip_count"] == 2
    assert v3["candidate_count"] == 2
    assert v3["scored_count"] == 2
    assert v3["correct_count"] == 2
    assert v3["incorrect_count"] == 0
    assert v3["correct_pct"] == 100.0
    assert v3["false_candidates"] == 0
    assert v3["missed_opportunities"] == 0
    assert v2["missed_opportunities"] >= 2
    assert v3["shadow_only"] is True
    assert any(example["example_kind"] == "candidate" for example in v3["examples"])
    replayed = replay_profile_row(rows[0], "balanced_v3_shadow")
    assert replayed["selected_symbol"] == "TNA"
    assert set(PROFILE_SCORE_FIELDS) <= set(replayed["profile_scores"])


def test_replay_script_prints_profile_comparison_and_default_stays_v2(capsys):
    rows = _replay_rows()

    def loader(_settings, run_ids, limit):
        return rows[:limit]

    assert replay_script.run_replay(_settings(), rows_loader=loader) == 0
    default_out = capsys.readouterr().out
    assert "balanced_v3_shadow" not in default_out
    assert "orders_submitted: 0" in default_out

    assert replay_script.run_replay(
        _settings(),
        rows_loader=loader,
        signal_profile="both",
        session_dates="2026-10-06,2026-10-07",
    ) == 0
    out = capsys.readouterr().out
    for text in (
        "--- profile: conservative_v2 ---",
        "--- profile: balanced_v3_shadow ---",
        "candidate_count:",
        "tna_count:",
        "tza_count:",
        "skip_count:",
        "scored_count:",
        "correct_count:",
        "incorrect_count:",
        "correct_pct:",
        "false_candidates:",
        "missed_opportunities:",
        "--- examples ---",
        "orders_submitted: 0  writes_to_database: false",
    ):
        assert text in out, text
    assert replay_script.run_replay(_settings(), rows_loader=loader, session_dates="not-a-date") == 2


# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------


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


@pytest.mark.parametrize("module", [v3_mod, profile_replay, shadow_script, replay_script])
def test_new_profile_modules_do_not_reference_order_execution(module):
    source = inspect.getsource(module)
    for token in _FORBIDDEN:
        assert token not in source, f"{module.__name__} references {token}"


def test_v3_cannot_revalidate_a_submit():
    with pytest.raises(ConfigurationError, match="shadow/replay only"):
        build_pre_submit_check(
            MagicMock(),
            engine=CandidateEngineV3(mode="shadow"),
            expected=SignalDirection.BULLISH,
            provider=MagicMock(),
        )


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
