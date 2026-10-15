"""Checkpoint 2.22 — validation dataset only. No decision or threshold changes."""

from __future__ import annotations

import inspect
import json

import pytest

import backend.shadow_mode.validation_dataset as dataset_mod
import backend.signals.candidate_engine_v3 as v3_mod
import backend.signals.candidate_quality_ranker as ranker_mod
import backend.signals.predictive_continuation as predictive_mod
import backend.signals.tna_tza_signal_engine as v2_mod
import scripts.report_validation_dataset as report_script
from backend.signals.candidate_engine_v3 import CandidateEngineV3, CandidateEngineV3Config
from backend.signals.candidate_quality_ranker import rank_candidate
from backend.signals.models import SignalDirection
from backend.signals.predictive_continuation import evaluate_predictive_continuation
from backend.signals.tna_tza_signal_engine import SignalEngineConfig
from backend.shadow_mode.validation_dataset import build_validation_dataset
from tests.test_checkpoint_212_shadow_mode import _settings as _shadow_settings
from tests.test_checkpoint_217a_balanced_shadow_profile import snap as profile_snap
from tests.test_checkpoint_217a_balanced_shadow_profile import window_bull
from tests.test_checkpoint_220_predictive_continuation import _stored, exhausted, strong_accel, strong_bear

_FORBIDDEN = (
    "order_executor",
    "OrderExecutor",
    "execution_router",
    "ExecutionRouter",
    "submit_equity_order",
    "/bot/start",
    "/trade/execute",
    "/trade/close",
    "/bot/stop",
    "sandbox_worker",
)


def _row(snapshot, *, run_id: str, cycle: int, iwm_after_pct: float, created: str, session_label: str = "regular_hours"):
    row = _stored(
        snapshot,
        decision="skip",
        symbol=None,
        tna_after_pct=iwm_after_pct,
        iwm_after_pct=iwm_after_pct,
        cycle=cycle,
    )
    row["run_id"] = run_id
    row["created_at"] = created
    row["session_label"] = session_label
    row["session_is_regular_hours"] = session_label == "regular_hours"
    row["freshness_gate_passed"] = True
    row["quote_age_iwm"] = 0.2
    row["quote_age_tna"] = 0.2
    return row


def test_current_engines_and_analytics_are_unchanged():
    assert SignalEngineConfig().entry_score_threshold == 70.0
    assert CandidateEngineV3Config().entry_score_threshold == 70.0
    assert CandidateEngineV3Config().vix_policy == "hard_block"
    for module in (v2_mod, v3_mod):
        source = inspect.getsource(module)
        assert "validation_dataset" not in source
        assert "candidate_quality" not in source
    assert "validation_dataset" not in inspect.getsource(predictive_mod)
    assert "validation_dataset" not in inspect.getsource(ranker_mod)
    engine = CandidateEngineV3(mode="shadow")
    bull = engine.decide(profile_snap(*window_bull()))
    faded = engine.decide(exhausted())
    assert bull.decision is SignalDirection.BULLISH
    assert faded.decision is SignalDirection.BULLISH
    signature = inspect.signature(engine.decide)
    assert signature.parameters["apply_continuation_filter"].default is False


def test_followup_labels_do_not_reach_realtime_models(monkeypatch):
    seen = []

    def spy_continuation(snapshot, symbol, **kwargs):
        seen.append(("continuation", kwargs, getattr(snapshot, "followup", None)))
        return evaluate_predictive_continuation(snapshot, symbol, **kwargs)

    def spy_rank(snapshot, symbol, **kwargs):
        seen.append(("rank", kwargs))
        return rank_candidate(snapshot, symbol, **kwargs)

    monkeypatch.setattr(dataset_mod, "evaluate_predictive_continuation", spy_continuation)
    monkeypatch.setattr(dataset_mod, "rank_candidate", spy_rank)
    winner = _row(
        strong_accel(),
        run_id="shadow-222-a",
        cycle=1,
        iwm_after_pct=0.12,
        created="2026-10-08T14:30:00+00:00",
    )
    opposite = dict(winner)
    opposite["iwm_mid_after"] = winner["iwm_mid"] * 0.99
    opposite["tna_mid_after"] = winner["tna_mid"] * 0.99
    first = build_validation_dataset([winner])
    second = build_validation_dataset([opposite])
    assert first["candidates"][0]["candidate_quality_score"] == second["candidates"][0]["candidate_quality_score"]
    assert first["candidates"][0]["predictive_continuation_probability"] == second["candidates"][0]["predictive_continuation_probability"]
    assert first["candidates"][0]["direction_correct_60s"] is True
    assert second["candidates"][0]["direction_correct_60s"] is False
    for _kind, kwargs, *_rest in [(item[0], item[1]) for item in seen]:
        assert "followup" not in kwargs
        assert "iwm_mid_after" not in kwargs
    assert inspect.signature(rank_candidate).parameters.keys() <= {"snapshot", "candidate_symbol", "candidate_score", "config"}
    assert "iwm_mid_after" not in inspect.getsource(rank_candidate)
    assert "iwm_mid_after" not in inspect.getsource(evaluate_predictive_continuation)


def test_multi_run_aggregation_and_duplicate_run_ids():
    rows = [
        _row(strong_accel(), run_id="shadow-2026-10-08-a", cycle=1, iwm_after_pct=0.12, created="2026-10-08T14:30:00+00:00"),
        _row(strong_bear(), run_id="shadow-2026-10-09-b", cycle=1, iwm_after_pct=-0.12, created="2026-10-09T15:00:00+00:00"),
    ]
    report = build_validation_dataset(rows + [dict(rows[0])])
    assert report["runs"] == 2
    assert report["total_candidates"] == 2
    assert report["tna_candidates"] == 1
    assert report["tza_candidates"] == 1
    assert report["labeled_candidates_60s"] == 2
    assert report["labeled_candidates_30s"] == 0
    assert report["labeled_candidates_120s"] == 0
    assert report["session_dates"] == ["2026-10-08", "2026-10-09"]
    assert report["generalization_status"] == "insufficient_sessions"
    assert report["sample_size_status"] == "insufficient"
    assert report["baseline_accuracy"]["60s"]["accuracy"] == 100.0
    assert report["orders_submitted"] == 0
    assert report["writes_to_database"] is False
    assert report["analytics_only"] is True
    assert "promising" not in report["generalization_status"]


def test_stale_and_invalid_runs_are_excluded():
    kept = _row(strong_accel(), run_id="shadow-keep", cycle=1, iwm_after_pct=0.12, created="2026-10-08T14:30:00+00:00")
    after_hours = _row(
        strong_accel(),
        run_id="shadow-after",
        cycle=1,
        iwm_after_pct=0.20,
        created="2026-10-08T22:00:00+00:00",
        session_label="after_hours",
    )
    stale = _row(strong_bear(), run_id="shadow-stale", cycle=1, iwm_after_pct=-0.20, created="2026-10-08T15:00:00+00:00")
    stale["freshness_gate_passed"] = False
    report = build_validation_dataset([kept, after_hours, stale])
    assert report["run_ids"] == ["shadow-keep"]
    assert report["total_candidates"] == 1
    assert "shadow-after" in report["excluded_run_ids"]
    assert "shadow-stale" in report["excluded_run_ids"]
    included = build_validation_dataset([kept, after_hours], exclude_invalid=False)
    assert included["runs"] == 2


def test_optional_horizons_stay_labels():
    row = _row(strong_accel(), run_id="shadow-horizons", cycle=1, iwm_after_pct=0.0, created="2026-10-08T14:30:00+00:00")
    row["iwm_move_30s"] = 0.04
    row["tna_move_30s"] = 0.05
    row["iwm_move_120s"] = -0.08
    row["tna_move_120s"] = -0.09
    report = build_validation_dataset([row])
    candidate = report["candidates"][0]
    assert candidate["direction_correct_30s"] is True
    assert candidate["meaningful_move_30s"] is True
    assert candidate["direction_correct_120s"] is False
    assert report["labeled_candidates_30s"] == 1
    assert report["labeled_candidates_120s"] == 1


def test_report_script_prints_summary_without_writing(capsys):
    rows = [
        _row(strong_accel(), run_id="shadow-2026-10-08-a", cycle=1, iwm_after_pct=0.12, created="2026-10-08T14:30:00+00:00")
    ]

    def loader(_settings, _run_ids, limit, session_dates=None):
        del session_dates
        return rows[:limit]

    assert report_script.run_report(_shadow_settings(), rows_loader=loader) == 0
    text = capsys.readouterr().out
    for label in (
        "=== validation dataset summary ===",
        "runs:",
        "session_dates:",
        "total_cycles:",
        "total_candidates:",
        "labeled_candidates_30s:",
        "labeled_candidates_60s:",
        "labeled_candidates_120s:",
        "TNA candidates:",
        "TZA candidates:",
        "baseline accuracy:",
        "accuracy by candidate score band:",
        "accuracy by signal reason:",
        "accuracy by day part:",
        "accuracy by VIX range:",
        "accuracy by momentum phase:",
        "accuracy by predictive probability band:",
        "accuracy by quality tier:",
        "candidate_retention_by_filter:",
        "predictive continuation:",
        "quality ranking:",
        "winners_accepted:",
        "losers_rejected:",
        "minimum_labeled_candidates: 20",
        "sample_size_status: insufficient",
        "generalization_status: insufficient_sessions",
        "orders_submitted: 0",
        "writes_to_database: false",
        "analytics_only: true",
    ):
        assert label in text
    assert report_script.run_report(_shadow_settings(), rows_loader=loader, json_output=True) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["orders_submitted"] == 0
    assert document["writes_to_database"] is False
    assert document["analytics_only"] is True


@pytest.mark.parametrize("module", [dataset_mod, report_script])
def test_validation_modules_do_not_reference_execution(module):
    source = inspect.getsource(module)
    for name in _FORBIDDEN:
        assert name not in source
    assert "orders_submitted" in source
    assert "writes_to_database" in source
