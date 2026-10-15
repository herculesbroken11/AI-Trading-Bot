"""Checkpoint 2.21 — experimental candidate quality ranking. No orders, no engine changes."""

from __future__ import annotations

import inspect
import json

import pytest

import backend.signals.candidate_engine_v3 as v3_mod
import backend.signals.candidate_quality_ranker as ranker_mod
import backend.signals.predictive_continuation as predictive_mod
import backend.signals.tna_tza_signal_engine as v2_mod
import backend.shadow_mode.candidate_ranking_replay as ranking_replay_mod
from backend.config.settings import ConfigurationError, Settings, load_settings, reset_settings_cache
from backend.risk.live_guard import LiveTradingBlockedError, assert_order_execution_allowed
from backend.shadow_mode.candidate_ranking_replay import compare_candidate_ranking
from backend.shadow_mode.runner import production_execution_block_checks
from backend.signals.candidate_engine_v3 import CandidateEngineV3, CandidateEngineV3Config
from backend.signals.candidate_quality_ranker import (
    CandidateQualityTier,
    rank_candidate,
)
from backend.signals.models import SignalDirection
from backend.signals.tna_tza_signal_engine import SignalEngineConfig
from scripts import replay_signal_engine_versions as replay_script
from tests.test_checkpoint_212_shadow_mode import _settings as _shadow_settings
from tests.test_checkpoint_217a_balanced_shadow_profile import snap as profile_snap
from tests.test_checkpoint_217a_balanced_shadow_profile import window_bull
from tests.test_checkpoint_217b_direction_and_confirmation import _NO_ENV
from tests.test_checkpoint_220_predictive_continuation import (
    _stored,
    exhausted,
    quote,
    small_accel,
    snap,
    strong_accel,
    strong_bear,
)

_FORBIDDEN = (
    "order_executor",
    "OrderExecutor",
    "execution_router",
    "ExecutionRouter",
    "submit_equity_order",
    "execute_order",
    "dry_run_equity_order",
    "/bot/start",
    "/trade/execute",
    "/trade/close",
    "/bot/stop",
    "sandbox_worker",
)


def _weak_pair():
    return snap(
        quote("TNA", [(0, 40.0), (20, 40.0004), (40, 40.0008)]),
        quote("TZA", [(0, 10.0), (20, 10.0002), (40, 10.0004)]),
        quote("IWM", [(0, 220.0), (20, 220.044), (40, 220.176)]),
        quote("SPY", [(0, 500.0), (20, 500.10), (40, 500.25)]),
        quote("QQQ", [(0, 400.0), (20, 400.08), (40, 400.20)]),
    )


def _broad_disagreement():
    return snap(
        quote("TNA", [(0, 40.0), (20, 40.008), (40, 40.040)]),
        quote("TZA", [(0, 10.0), (20, 9.998), (40, 9.990)]),
        quote("IWM", [(0, 220.0), (20, 220.044), (40, 220.176)]),
        quote("SPY", [(0, 500.0), (20, 499.90), (40, 499.75)]),
        quote("QQQ", [(0, 400.0), (20, 399.92), (40, 399.80)]),
    )


def _choppy():
    return snap(
        quote("TNA", [(0, 40.0), (8, 40.08), (16, 39.98), (24, 40.10), (32, 39.97), (40, 40.02)]),
        quote("TZA", [(0, 10.0), (8, 9.98), (16, 10.02), (24, 9.97), (32, 10.03), (40, 10.00)]),
        quote("IWM", [(0, 220.0), (8, 220.40), (16, 219.70), (24, 220.55), (32, 219.60), (40, 220.10)]),
        quote("SPY", [(0, 500.0), (20, 500.05), (40, 500.08)]),
        quote("QQQ", [(0, 400.0), (20, 400.04), (40, 400.06)]),
    )


def _iwm_only():
    return snap(
        quote("TNA", [(0, 40.0), (20, 40.0), (40, 40.0)]),
        quote("TZA", [(0, 10.0), (20, 10.0), (40, 10.0)]),
        quote("IWM", [(0, 220.0), (20, 220.044), (40, 220.176)]),
        quote("SPY", [(0, 500.0), (20, 500.0), (40, 500.0)]),
        quote("QQQ", [(0, 400.0), (20, 400.0), (40, 400.0)]),
    )


def _exhausted_bear():
    return snap(
        quote("TZA", [(0, 10.0), (20, 10.025), (40, 10.026)]),
        quote("TNA", [(0, 40.0), (20, 39.90), (40, 39.896)]),
        quote("IWM", [(0, 220.0), (20, 219.45), (40, 219.428)]),
        quote("SPY", [(0, 500.0), (20, 498.75), (40, 498.70)]),
        quote("QQQ", [(0, 400.0), (20, 399.00), (40, 398.96)]),
    )


def _row(snapshot, *, run_id: str, cycle: int, iwm_after_pct: float):
    row = _stored(
        snapshot,
        decision="skip",
        symbol=None,
        tna_after_pct=iwm_after_pct,
        iwm_after_pct=iwm_after_pct,
        cycle=cycle,
    )
    row["run_id"] = run_id
    return row


def test_strong_progressive_bullish_setup_is_tier_a_or_b():
    result = rank_candidate(strong_accel(), "TNA", candidate_score=80.0)
    assert result is not None
    assert result.quality_tier in {CandidateQualityTier.A, CandidateQualityTier.B}
    assert result.rank_passed is True
    assert result.pair_structure_score >= 55.0
    assert result.independent_confirmation_count >= 3
    bear = rank_candidate(strong_bear(), "TZA")
    assert bear is not None
    assert bear.quality_tier in {CandidateQualityTier.A, CandidateQualityTier.B}


def test_exhausted_setup_is_not_accepted():
    result = rank_candidate(exhausted(), "TNA", candidate_score=97.0)
    assert result is not None
    assert result.quality_tier in {CandidateQualityTier.C, CandidateQualityTier.REJECT}
    assert result.rank_passed is False
    assert result.extension_penalty >= 60.0


def test_strong_iwm_with_weak_pair_is_not_tier_a():
    result = rank_candidate(_weak_pair(), "TNA", candidate_score=90.0)
    assert result is not None
    assert result.momentum_quality_score >= 55.0
    assert result.pair_structure_score < 55.0
    assert result.quality_tier is not CandidateQualityTier.A


def test_broad_disagreement_reduces_quality_without_a_hard_reject():
    aligned = rank_candidate(strong_accel(), "TNA")
    disagreed = rank_candidate(_broad_disagreement(), "TNA")
    assert aligned is not None and disagreed is not None
    assert disagreed.disagreement_penalty > 0.0
    assert disagreed.quality_score < aligned.quality_score
    assert disagreed.quality_tier is not CandidateQualityTier.REJECT


def test_choppy_path_keeps_timing_and_path_quality_low():
    result = rank_candidate(_choppy(), "TNA", candidate_score=95.0)
    assert result is not None
    assert result.timing_quality_score < 50.0
    assert result.path_consistency_score < 50.0
    assert result.quality_score < 50.0
    untouched = rank_candidate(_choppy(), "TNA", candidate_score=5.0)
    assert untouched is not None
    assert abs(result.quality_score - untouched.quality_score) < 5.0


def test_accelerating_moderate_setup_outranks_an_exhausted_move():
    fresh = rank_candidate(small_accel(), "TNA", candidate_score=70.0)
    faded = rank_candidate(exhausted(), "TNA", candidate_score=97.0)
    assert fresh is not None and faded is not None
    assert fresh.quality_score > faded.quality_score
    assert fresh.rank_passed is True
    assert faded.rank_passed is False


def test_rank_candidate_does_not_read_followup_outcomes():
    signature = inspect.signature(rank_candidate)
    assert set(signature.parameters) <= {"snapshot", "candidate_symbol", "candidate_score", "config"}
    source = inspect.getsource(rank_candidate)
    for token in ("iwm_mid_after", "tna_mid_after", "tza_mid_after", "followup_move", "actual_followup_correct"):
        assert token not in source
    first = rank_candidate(strong_accel(), "TNA", candidate_score=86.0)
    second = rank_candidate(strong_accel(), "TNA", candidate_score=86.0)
    assert first is not None and second is not None
    assert first.quality_score == second.quality_score
    assert "actual_followup_correct" not in first.to_dict()


def test_one_iwm_move_is_not_several_independent_confirmations():
    result = rank_candidate(_iwm_only(), "TNA", candidate_score=90.0)
    assert result is not None
    assert result.momentum_quality_score >= 55.0
    assert result.evidence_count > result.independent_confirmation_count
    assert result.independent_confirmation_count <= 1
    assert result.effective_evidence_count == result.independent_confirmation_count
    assert result.effective_evidence_count < 3
    assert result.correlated_evidence_penalty > 0.0
    assert result.quality_tier is not CandidateQualityTier.A


def test_no_candidate_produces_no_ranking():
    assert rank_candidate(strong_accel(), None) is None
    assert rank_candidate(strong_accel(), "SPY") is None
    assert rank_candidate(strong_accel(), "") is None


def test_conservative_v2_and_balanced_base_decisions_stay_unchanged():
    assert SignalEngineConfig().entry_score_threshold == 70.0
    assert CandidateEngineV3Config().entry_score_threshold == 70.0
    assert CandidateEngineV3Config().vix_policy == "hard_block"
    assert "candidate_quality" not in inspect.getsource(v2_mod)
    assert "candidate_quality" not in inspect.getsource(v3_mod)
    engine = CandidateEngineV3(mode="shadow")
    bull = engine.decide(profile_snap(*window_bull()))
    faded = engine.decide(exhausted())
    assert bull.decision is SignalDirection.BULLISH
    assert faded.decision is SignalDirection.BULLISH
    assert faded.confidence_score > bull.confidence_score
    ranked = rank_candidate(exhausted(), "TNA")
    assert ranked is not None and ranked.rank_passed is False


def test_predictive_continuation_stays_analytics_only():
    source = inspect.getsource(predictive_mod)
    assert "candidate_quality_ranker" not in source
    for name in _FORBIDDEN:
        assert name not in source
    assert "analytics_only" in source


def test_replay_reports_before_and_after_without_writing():
    rows = [
        _row(strong_accel(), run_id="shadow-221-winner", cycle=1, iwm_after_pct=0.12),
        _row(exhausted(), run_id="shadow-221-loser", cycle=1, iwm_after_pct=-0.12),
    ]
    before = json.dumps(rows, sort_keys=True)
    report = compare_candidate_ranking(rows, min_move_pct=0.03)
    assert json.dumps(rows, sort_keys=True) == before
    combined = report["combined"]
    assert report["candidate_source"] == "balanced_v3_shadow"
    assert combined["candidate_count"] == 2
    assert combined["accuracy_before_ranking"] == 50.0
    assert combined["accuracy_after_ranking"] == 100.0
    assert combined["false_signals_removed"] == 1
    assert combined["good_signals_removed"] == 0
    assert combined["false_signals_kept"] == 0
    assert combined["good_signals_preserved"] == 1
    assert report["sample_size_status"] == "insufficient"
    assert report["orders_submitted"] == 0
    assert report["writes_to_database"] is False
    assert report["analytics_only"] is True
    assert combined["examples"]["false_signal_correctly_rejected"]
    assert combined["examples"]["false_signal_correctly_rejected"][0]["what_if_result"] == "would_have_been_incorrect"
    assert combined["examples"]["winner_correctly_accepted"]


def test_generalization_flags_a_rule_that_helps_one_run_and_hurts_another():
    improved = [
        _row(strong_accel(), run_id="shadow-221-up", cycle=1, iwm_after_pct=0.12),
        _row(_exhausted_bear(), run_id="shadow-221-up", cycle=2, iwm_after_pct=0.12),
    ]
    worsened = [
        _row(strong_accel(), run_id="shadow-221-down", cycle=1, iwm_after_pct=-0.12),
        _row(_exhausted_bear(), run_id="shadow-221-down", cycle=2, iwm_after_pct=-0.12),
    ]
    report = compare_candidate_ranking(improved + worsened, min_move_pct=0.03)
    assert report["generalization_status"] == "inconsistent"
    assert {item["run_id"] for item in report["per_run"]} == {"shadow-221-up", "shadow-221-down"}


def test_replay_script_prints_ranking_section(capsys):
    rows = [_row(strong_accel(), run_id="shadow-221-cli", cycle=1, iwm_after_pct=0.12)]

    def loader(_settings, _run_ids, limit, session_dates=None):
        del session_dates
        return rows[:limit]

    assert replay_script.run_replay(
        _shadow_settings(),
        rows_loader=loader,
        candidate_ranking=True,
        predictive_continuation=True,
        signal_profile="balanced_v3_shadow",
    ) == 0
    text = capsys.readouterr().out
    for label in (
        "=== candidate quality ranking analysis ===",
        "--- predictive continuation analysis ---",
        "candidate_source: balanced_v3_shadow",
        "tier_a_count:",
        "tier_b_count:",
        "tier_c_count:",
        "rejected_count:",
        "accuracy_before_ranking:",
        "accuracy_after_ranking:",
        "false_signals_removed:",
        "good_signals_removed:",
        "precision_by_tier:",
        "quality_separation:",
        "generalization_status:",
        "sample_size_status:",
        "per-run",
        "combined",
        "orders_submitted: 0",
        "writes_to_database: false",
        "analytics_only: true",
    ):
        assert label in text

    assert replay_script.run_replay(_shadow_settings(), rows_loader=loader, json_output=True, candidate_ranking=True) == 0
    document = json.loads(capsys.readouterr().out)
    section = document["candidate_ranking"]
    assert document["orders_submitted"] == 0
    assert document["writes_to_database"] is False
    assert section["analytics_only"] is True
    assert section["writes_to_database"] is False
    assert section["orders_submitted"] == 0


def test_repeated_run_id_flags_combine():
    assert replay_script._parse_run_ids(["shadow-2026-10-08-balanced-v3-warmup-midday", "shadow-2026-10-09-balanced-v3-confirmed-open"]) == [
        "shadow-2026-10-08-balanced-v3-warmup-midday",
        "shadow-2026-10-09-balanced-v3-confirmed-open",
    ]
    assert replay_script._parse_run_ids(["shadow-a,shadow-b"]) == ["shadow-a", "shadow-b"]


@pytest.mark.parametrize("module", [ranker_mod, ranking_replay_mod])
def test_ranking_modules_do_not_reference_execution(module):
    source = inspect.getsource(module)
    for name in _FORBIDDEN:
        assert name not in source
    assert "orders_submitted" in source
    assert "analytics_only" in source


def test_production_execution_and_public_routes_remain_blocked(monkeypatch):
    assert all(production_execution_block_checks().values())
    with pytest.raises(LiveTradingBlockedError):
        assert_order_execution_allowed(Settings(live_trading_enabled=True))
    monkeypatch.setenv("TRADING_MODE", "sandbox")
    monkeypatch.setenv("TASTYTRADE_ENV", "sandbox")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    monkeypatch.setenv("API_ADMIN_KEY", "checkpoint-221-test-key")
    reset_settings_cache()
    from backend.app_factory import create_app

    client = create_app(skip_db_init=True, defer_heavy_services=True).test_client()
    assert client.post("/trade/execute", json={"symbol": "TNA"}).status_code == 423
    assert client.post("/bot/start").status_code == 423
    assert client.post("/bot/stop").status_code == 423
    assert client.post("/trade/close/1").status_code == 423
    reset_settings_cache()
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    with pytest.raises(ConfigurationError):
        load_settings(env_path=_NO_ENV)
