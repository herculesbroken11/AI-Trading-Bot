"""Checkpoint 2.20 — shadow-only predictive continuation. No orders, no engine changes."""

from __future__ import annotations

import inspect
import json
from datetime import datetime, timezone

import pytest

import backend.signals.candidate_engine_v3 as v3_mod
import backend.signals.predictive_continuation as predictive_mod
import backend.signals.tna_tza_signal_engine as v2_mod
import backend.shadow_mode.predictive_replay as replay_mod
from backend.shadow_mode.models import ShadowCycleRecord
from backend.shadow_mode.predictive_replay import compare_predictive_continuation
from backend.signals.candidate_engine_v3 import CandidateEngineV3, CandidateEngineV3Config
from backend.signals.models import MarketSnapshot, SignalDecision, SignalDirection, SignalReason, SymbolQuote
from backend.signals.predictive_continuation import (
    PHASE_DEVELOPING,
    PHASE_EARLY,
    PHASE_EXHAUSTION,
    PHASE_REVERSAL_RISK,
    REASON_EXHAUSTION,
    REASON_REVERSAL,
    PredictiveContinuationConfig,
    evaluate_predictive_continuation,
    format_shadow_lines,
)
from backend.signals.tna_tza_signal_engine import SignalEngineConfig
from scripts import replay_signal_engine_versions as replay_script
from tests.test_checkpoint_212_shadow_mode import _settings as _shadow_settings
from tests.test_checkpoint_217a_balanced_shadow_profile import snap as profile_snap
from tests.test_checkpoint_217a_balanced_shadow_profile import window_bull

NOW = datetime(2026, 10, 8, 14, 30, tzinfo=timezone.utc)

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
    "sandbox_worker",
)


def quote(symbol: str, points):
    end = points[-1][1]
    start = points[0][1]
    prices = [price for _, price in points]
    return SymbolQuote(
        symbol=symbol,
        bid=round(end - 0.01, 4),
        ask=round(end + 0.01, 4),
        first_mid=start,
        window_high_mid=max(prices),
        window_low_mid=min(prices),
        quote_age_seconds=0.2,
        quote_updates=len(points),
        mid_path=tuple((float(t), float(price)) for t, price in points),
    )


def snap(*quotes: SymbolQuote) -> MarketSnapshot:
    return MarketSnapshot(quotes={item.symbol: item for item in quotes}, created_at=NOW)


def strong_accel():
    return snap(
        quote("TNA", [(0, 40.0), (20, 40.008), (40, 40.040)]),
        quote("TZA", [(0, 10.0), (20, 9.998), (40, 9.990)]),
        quote("IWM", [(0, 220.0), (20, 220.044), (40, 220.176)]),
        quote("SPY", [(0, 500.0), (20, 500.10), (40, 500.25)]),
        quote("QQQ", [(0, 400.0), (20, 400.08), (40, 400.20)]),
    )


def strong_bear():
    return snap(
        quote("TZA", [(0, 10.0), (20, 10.002), (40, 10.010)]),
        quote("TNA", [(0, 40.0), (20, 39.992), (40, 39.968)]),
        quote("IWM", [(0, 220.0), (20, 219.956), (40, 219.824)]),
        quote("SPY", [(0, 500.0), (20, 499.90), (40, 499.75)]),
        quote("QQQ", [(0, 400.0), (20, 399.92), (40, 399.80)]),
    )


def exhausted():
    return snap(
        quote("TNA", [(0, 40.0), (20, 40.10), (40, 40.104)]),
        quote("TZA", [(0, 10.0), (20, 9.975), (40, 9.974)]),
        quote("IWM", [(0, 220.0), (20, 220.55), (40, 220.572)]),
        quote("SPY", [(0, 500.0), (20, 501.25), (40, 501.30)]),
        quote("QQQ", [(0, 400.0), (20, 401.00), (40, 401.04)]),
    )


def reversing():
    return snap(
        quote("TNA", [(0, 40.0), (10, 40.012), (20, 40.024), (30, 40.030), (40, 39.988)]),
        quote("TZA", [(0, 10.0), (10, 9.997), (20, 9.994), (30, 9.993), (40, 10.006)]),
        quote("IWM", [(0, 220.0), (10, 220.066), (20, 220.132), (30, 220.160), (40, 219.934)]),
        quote("SPY", [(0, 500.0), (10, 500.08), (20, 500.15), (30, 500.18), (40, 499.85)]),
        quote("QQQ", [(0, 400.0), (10, 400.06), (20, 400.12), (30, 400.14), (40, 399.88)]),
    )


def small_accel():
    return snap(
        quote("TNA", [(0, 40.0), (20, 40.004), (40, 40.016)]),
        quote("TZA", [(0, 10.0), (20, 9.997), (40, 9.990)]),
        quote("IWM", [(0, 220.0), (20, 220.044), (40, 220.132)]),
        quote("SPY", [(0, 500.0), (20, 500.08), (40, 500.20)]),
        quote("QQQ", [(0, 400.0), (20, 400.06), (40, 400.16)]),
    )


def _decision(direction: SignalDirection, symbol, score: float = 86.0) -> SignalDecision:
    return SignalDecision(
        decision=direction,
        selected_symbol=symbol,
        confidence_score=score,
        bullish_score=score if direction is SignalDirection.BULLISH else 10.0,
        bearish_score=score if direction is SignalDirection.BEARISH else 10.0,
        skip_reason=None if direction is not SignalDirection.SKIP else SignalReason.UNCLEAR_MARKET_DIRECTION,
        quote_freshness_by_symbol={},
        market_regime="test",
        explanation="checkpoint 2.20",
        created_at=NOW,
        freshness_gate_passed=True,
    )


def _stored(snapshot: MarketSnapshot, *, decision: str, symbol, tna_after_pct: float, iwm_after_pct: float, cycle: int):
    mids = {name: snapshot.get(name).mid for name in ("TNA", "TZA", "IWM", "SPY", "QQQ")}

    def moved(name: str, pct: float) -> float:
        return mids[name] * (1.0 + pct / 100.0)

    return {
        "run_id": "shadow-220",
        "cycle_number": cycle,
        "created_at": f"2026-10-08T14:{cycle:02d}:00",
        "decision": decision,
        "selected_symbol": symbol,
        "raw_snapshot_json": json.dumps(snapshot.to_dict()),
        "followup_seconds": 60,
        "tna_mid": mids["TNA"],
        "tza_mid": mids["TZA"],
        "iwm_mid": mids["IWM"],
        "spy_mid": mids["SPY"],
        "qqq_mid": mids["QQQ"],
        "tna_mid_after": moved("TNA", tna_after_pct),
        "tza_mid_after": moved("TZA", -iwm_after_pct),
        "iwm_mid_after": moved("IWM", iwm_after_pct),
        "spy_mid_after": mids["SPY"],
        "qqq_mid_after": mids["QQQ"],
        "outcome_note": "scored",
    }


def test_strong_accelerating_move_passes():
    result = evaluate_predictive_continuation(strong_accel(), "TNA")
    assert result is not None
    assert result.prediction_passed is True
    assert result.momentum_phase == PHASE_DEVELOPING
    assert result.continuation_probability >= 55.0
    assert result.acceleration_score > result.exhaustion_score
    assert 0.0 <= result.continuation_probability <= 100.0
    bear = evaluate_predictive_continuation(strong_bear(), "TZA")
    assert bear is not None and bear.prediction_passed is True
    assert bear.momentum_phase == PHASE_DEVELOPING


def test_large_move_then_slowing_is_exhaustion():
    result = evaluate_predictive_continuation(exhausted(), "TNA")
    assert result is not None
    assert result.momentum_phase == PHASE_EXHAUSTION
    assert result.prediction_passed is False
    assert result.prediction_reason == REASON_EXHAUSTION
    assert result.exhaustion_score >= 60.0
    assert result.continuation_probability < 55.0


def test_final_seconds_reversal_fails():
    result = evaluate_predictive_continuation(reversing(), "TNA")
    assert result is not None
    assert result.momentum_phase == PHASE_REVERSAL_RISK
    assert result.prediction_passed is False
    assert result.prediction_reason == REASON_REVERSAL
    assert result.reversal_risk_score >= 60.0
    assert result.continuation_probability < 55.0


def test_small_accelerating_move_passes_with_strong_confirmation():
    result = evaluate_predictive_continuation(small_accel(), "TNA")
    assert result is not None
    assert result.prediction_passed is True
    assert result.momentum_phase == PHASE_EARLY
    assert result.continuation_probability >= 55.0


def test_no_candidate_generates_no_prediction():
    snapshot = strong_accel()
    assert evaluate_predictive_continuation(snapshot, None) is None
    assert evaluate_predictive_continuation(snapshot, "") is None
    assert evaluate_predictive_continuation(snapshot, "SPY") is None
    assert evaluate_predictive_continuation(snapshot, "IWM") is None
    record = ShadowCycleRecord.from_decision(
        run_id="r",
        cycle_number=1,
        decision=_decision(SignalDirection.SKIP, None, score=0.0),
        snapshot=snapshot,
    )
    assert record.predictive_continuation is None
    payload = record.to_dict()
    assert payload["prediction_passed"] is None
    assert payload["momentum_phase"] is None
    assert "predictive_continuation" not in json.loads(record.raw_score_json)


def test_shadow_output_fields_for_an_existing_candidate():
    snapshot = strong_accel()
    record = ShadowCycleRecord.from_decision(
        run_id="r",
        cycle_number=2,
        decision=_decision(SignalDirection.BULLISH, "TNA", score=86.0),
        snapshot=snapshot,
    )
    payload = record.to_dict()
    assert payload["predictive_continuation_probability"] >= 55.0
    assert payload["momentum_phase"] == PHASE_DEVELOPING
    assert payload["acceleration_score"] > 0
    assert payload["velocity_score"] > 0
    assert payload["exhaustion_score"] < 60
    assert payload["reversal_risk_score"] < 60
    assert payload["prediction_passed"] is True
    assert payload["submitted"] is False
    stored = json.loads(record.raw_score_json)["predictive_continuation"]
    assert stored["analytics_only"] is True
    assert stored["orders_submitted"] == 0
    assert stored["writes_to_database"] is False
    lines = format_shadow_lines("TNA", 86.0, evaluate_predictive_continuation(snapshot, "TNA"))
    text = "\n".join(lines)
    assert "candidate:\nTNA" in text
    assert "phase:\nDEVELOPING" in text
    assert "prediction:\nPASS" in text
    assert "orders_submitted: 0" in text
    assert "writes_to_database: false" in text
    assert "analytics_only: true" in text


def test_weights_are_configurable():
    snapshot = exhausted()
    default = evaluate_predictive_continuation(snapshot, "TNA")
    loosened = evaluate_predictive_continuation(
        snapshot,
        "TNA",
        config=PredictiveContinuationConfig(exhaustion_penalty_weight=0.0, pass_threshold=40.0),
    )
    assert default is not None and loosened is not None
    assert default.prediction_passed is False
    assert loosened.continuation_probability > default.continuation_probability
    assert loosened.prediction_passed is True
    with pytest.raises(ValueError):
        PredictiveContinuationConfig(pass_threshold=0.0).validate()


def test_replay_compares_candidates_before_and_after_prediction():
    winner = _stored(strong_accel(), decision="bullish", symbol="TNA", tna_after_pct=0.12, iwm_after_pct=0.12, cycle=1)
    false_signal = _stored(exhausted(), decision="bullish", symbol="TNA", tna_after_pct=-0.12, iwm_after_pct=-0.12, cycle=2)
    removed_winner = _stored(exhausted(), decision="bullish", symbol="TNA", tna_after_pct=0.12, iwm_after_pct=0.12, cycle=3)
    skip = _stored(strong_accel(), decision="skip", symbol=None, tna_after_pct=0.20, iwm_after_pct=0.20, cycle=4)
    before = json.dumps([winner, false_signal, removed_winner, skip], sort_keys=True)
    report = compare_predictive_continuation([winner, false_signal, removed_winner, skip], min_move_pct=0.03)
    assert json.dumps([winner, false_signal, removed_winner, skip], sort_keys=True) == before
    assert report["candidate_count"] == 3
    assert report["correct_candidates"] == 2
    assert report["incorrect_candidates"] == 1
    assert report["passed_predictions"] == 1
    assert report["failed_predictions"] == 2
    assert report["accuracy_before_prediction"] == 66.7
    assert report["accuracy_after_prediction"] == 100.0
    assert report["false_signals_removed"] == 1
    assert report["good_signals_removed"] == 1
    assert report["missed_winners"] == 1
    assert report["orders_submitted"] == 0
    assert report["writes_to_database"] is False
    assert report["analytics_only"] is True


def test_replay_script_prints_predictive_section(capsys):
    rows = [
        _stored(strong_accel(), decision="bullish", symbol="TNA", tna_after_pct=0.12, iwm_after_pct=0.12, cycle=1),
        _stored(exhausted(), decision="bullish", symbol="TNA", tna_after_pct=-0.12, iwm_after_pct=-0.12, cycle=2),
    ]

    def loader(_settings, _run_ids, limit, session_dates=None):
        del session_dates
        return rows[:limit]

    assert replay_script.run_replay(_shadow_settings(), rows_loader=loader, predictive_continuation=True) == 0
    text = capsys.readouterr().out
    for label in (
        "--- predictive continuation analysis ---",
        "candidate_count:",
        "correct_candidates:",
        "incorrect_candidates:",
        "passed_predictions:",
        "failed_predictions:",
        "accuracy_before_prediction:",
        "accuracy_after_prediction:",
        "false_signals_removed:",
        "good_signals_removed:",
        "missed_winners:",
        "orders_submitted: 0",
        "writes_to_database: false",
        "analytics_only: true",
    ):
        assert label in text
    assert "candidate_count: 2" in text
    assert "passed_predictions: 1" in text
    assert "failed_predictions: 1" in text

    assert replay_script.run_replay(_shadow_settings(), rows_loader=loader) == 0
    assert "--- predictive continuation analysis ---" not in capsys.readouterr().out


def test_replay_json_stays_analytics_only(capsys):
    def loader(_settings, _run_ids, limit, session_dates=None):
        del session_dates
        return []

    assert replay_script.run_replay(
        _shadow_settings(),
        rows_loader=loader,
        json_output=True,
        predictive_continuation=True,
    ) == 0
    document = json.loads(capsys.readouterr().out)
    section = document["predictive_continuation"]
    assert document["analytics_only"] is True
    assert document["orders_submitted"] == 0
    assert document["writes_to_database"] is False
    assert section["analytics_only"] is True
    assert section["orders_submitted"] == 0
    assert section["writes_to_database"] is False
    assert section["candidate_count"] == 0


def test_existing_engines_are_unchanged():
    decision = CandidateEngineV3(mode="shadow").decide(profile_snap(*window_bull()))
    assert decision.decision is SignalDirection.BULLISH
    assert decision.confidence_score >= 70
    assert "predictive_continuation" not in (decision.profile_scores or {})
    assert SignalEngineConfig().entry_score_threshold == 70.0
    assert CandidateEngineV3Config().entry_score_threshold == 70.0
    assert CandidateEngineV3Config().vix_policy == "penalty"
    for module in (v2_mod, v3_mod):
        source = inspect.getsource(module)
        assert "predictive_continuation" not in source
        assert "evaluate_predictive_continuation" not in source


@pytest.mark.parametrize("module", [predictive_mod, replay_mod])
def test_predictive_modules_do_not_reference_execution(module):
    source = inspect.getsource(module)
    for name in _FORBIDDEN:
        assert name not in source
    assert "orders_submitted" in source
