"""Checkpoint 2.24B — session dates come from stored timestamps, converted to New York."""

from __future__ import annotations

import inspect
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import backend.signals.candidate_engine_v3 as v3_mod
from backend.db.base import Base
from backend.db.models import ShadowSignalLog
from backend.repositories.shadow_signal_repository import ShadowSignalRepository
from backend.shadow_mode.models import TRACKED_SYMBOLS, ShadowCycleRecord
from backend.shadow_mode.validation_dataset import (
    STORED_TIMESTAMP_SEMANTICS,
    build_validation_dataset,
    diagnose_run,
    et_session_date,
    timestamp_semantics,
)
from backend.signals.tna_tza_signal_engine import SignalEngineConfig
from tests.test_checkpoint_212_shadow_mode import _settings as _shadow_settings
from tests.test_checkpoint_220_predictive_continuation import strong_accel, strong_bear
from tests.test_checkpoint_222_validation_dataset import _row
import scripts.report_validation_dataset as report_script


def test_utc_timestamp_before_midnight_eastern_uses_the_previous_date():
    raw = "2026-10-09T03:30:00"
    assert timestamp_semantics(raw) == "UTC-naive"
    assert et_session_date(raw) == "2026-10-08"
    diag = diagnose_run("shadow-2026-10-09-balanced-v3-confirmed-open", [{"created_at": raw}])
    assert diag["timestamp_semantics"] == "UTC-naive"
    assert diag["first_raw_timestamp"] == raw
    assert diag["first_et"].startswith("2026-10-08T23:30:00")
    assert diag["derived_dates"] == ["2026-10-08"]


def test_utc_timestamp_during_the_new_york_day_keeps_that_date():
    raw = datetime(2026, 10, 9, 15, 0)
    assert raw.tzinfo is None
    assert et_session_date(raw) == "2026-10-09"
    diag = diagnose_run("shadow-midday", [{"created_at": raw}])
    assert diag["first_et"].startswith("2026-10-09T11:00:00")
    assert diag["derived_dates"] == ["2026-10-09"]


def test_timezone_aware_utc_uses_the_same_new_york_conversion():
    early = "2026-10-09T03:30:00+00:00"
    later = datetime(2026, 10, 9, 15, 0, tzinfo=timezone.utc)
    assert timestamp_semantics(early) == "UTC-aware"
    assert timestamp_semantics(later) == "UTC-aware"
    assert et_session_date(early) == "2026-10-08"
    assert et_session_date(later) == "2026-10-09"


def test_shadow_logger_stores_a_naive_utc_clock_and_does_not_rewrite_it():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    aware = datetime(2026, 10, 9, 2, 15, tzinfo=timezone.utc)
    record = ShadowCycleRecord(
        run_id="shadow-2026-10-09-balanced-v3-confirmed-open",
        cycle_number=1,
        created_at=aware,
        decision="skip",
        selected_symbol=None,
        confidence_score=0.0,
        bullish_score=0.0,
        bearish_score=0.0,
        skip_reason="high_volatility",
        market_regime="mixed",
        explanation="storage convention check",
        quote_ages={symbol: 0.2 for symbol in TRACKED_SYMBOLS},
        mids={symbol: 100.0 for symbol in TRACKED_SYMBOLS},
        vix_last=16.0,
        freshness_gate_passed=True,
        raw_snapshot_json="{}",
        raw_score_json="{}",
    )
    stored = ShadowSignalRepository(session).log_signal(record)
    loaded = session.get(ShadowSignalLog, stored.id)
    column = ShadowSignalLog.__table__.columns["created_at"].type
    assert getattr(column, "timezone", False) in (False, None)
    assert "replace(tzinfo=None)" in inspect.getsource(ShadowSignalRepository.log_signal)
    assert "timezone.utc" in inspect.getsource(v3_mod._utc_now)
    assert STORED_TIMESTAMP_SEMANTICS == "UTC-naive"
    assert record.created_at == aware
    assert record.created_at.tzinfo is not None
    assert loaded.created_at.tzinfo is None
    assert loaded.created_at == datetime(2026, 10, 9, 2, 15)
    diag = diagnose_run(record.run_id, [{"created_at": loaded.created_at}])
    assert diag["timestamp_semantics"] == "UTC-naive"
    assert diag["first_raw_timestamp"] == "2026-10-09T02:15:00"
    assert diag["derived_dates"] == ["2026-10-08"]
    assert diag["first_et"].startswith("2026-10-08T22:15:00-04:00")
    session.close()


def test_run_id_and_a_stored_session_date_field_do_not_choose_the_date():
    row = _row(
        strong_accel(),
        run_id="shadow-2026-10-09-balanced-v3-confirmed-open",
        cycle=1,
        iwm_after_pct=0.12,
        created="2026-10-09T02:15:00",
    )
    row["session_date"] = "2026-10-09"
    report = build_validation_dataset([row])
    assert report["session_dates"] == ["2026-10-08"]
    assert report["candidates"][0]["session_date"] == "2026-10-08"
    diag = report["session_date_diagnostics"][0]
    assert diag["run_id"] == row["run_id"]
    assert diag["derived_dates"] == ["2026-10-08"]
    assert "2026-10-09" not in diag["derived_dates"]


def test_multiple_runs_on_the_same_et_date_count_as_one_session():
    morning = _row(strong_accel(), run_id="shadow-2026-10-16-a", cycle=1, iwm_after_pct=0.12, created="2026-10-16T14:46:00")
    afternoon = _row(strong_bear(), run_id="shadow-2026-10-16-b", cycle=1, iwm_after_pct=-0.12, created="2026-10-16T17:33:00")
    report = build_validation_dataset([morning, afternoon])
    assert report["runs"] == 2
    assert report["session_dates"] == ["2026-10-16"]
    assert report["generalization_status"] == "insufficient_sessions"
    assert len(report["session_date_diagnostics"]) == 2
    assert all(item["derived_dates"] == ["2026-10-16"] for item in report["session_date_diagnostics"])


def test_adjacent_et_dates_stay_separate():
    late = _row(strong_accel(), run_id="shadow-late", cycle=1, iwm_after_pct=0.12, created="2026-10-09T03:30:00")
    early = _row(strong_bear(), run_id="shadow-early", cycle=1, iwm_after_pct=-0.12, created="2026-10-09T04:30:00")
    report = build_validation_dataset([late, early])
    assert report["session_dates"] == ["2026-10-08", "2026-10-09"]
    by_run = {item["run_id"]: item["derived_dates"] for item in report["session_date_diagnostics"]}
    assert by_run["shadow-late"] == ["2026-10-08"]
    assert by_run["shadow-early"] == ["2026-10-09"]


def test_generalization_counts_unique_verified_et_dates():
    rows = [
        _row(strong_accel(), run_id="shadow-2026-10-08-balanced-v3-warmup-midday", cycle=1, iwm_after_pct=0.12, created="2026-10-08T16:00:00"),
        _row(strong_bear(), run_id="shadow-2026-10-09-balanced-v3-confirmed-open", cycle=1, iwm_after_pct=-0.12, created="2026-10-09T02:15:00"),
        _row(strong_accel(), run_id="shadow-2026-10-15-balanced-v3-validation-1", cycle=1, iwm_after_pct=0.12, created="2026-10-15T18:00:00"),
        _row(strong_bear(), run_id="shadow-2026-10-16-balanced-v3-validation-1", cycle=1, iwm_after_pct=-0.12, created="2026-10-16T14:46:00"),
        _row(strong_accel(), run_id="shadow-2026-10-16-balanced-v3-semi-context-1", cycle=1, iwm_after_pct=0.12, created="2026-10-16T17:33:00"),
    ]
    report = build_validation_dataset(rows)
    assert report["runs"] == 5
    assert report["session_dates"] == ["2026-10-08", "2026-10-15", "2026-10-16"]
    assert report["generalization_status"] == "awaiting_comparison"
    two = build_validation_dataset(rows[:2])
    assert two["session_dates"] == ["2026-10-08"]
    assert two["generalization_status"] == "insufficient_sessions"


def test_semiconductor_accounting_invariant_still_holds():
    row = _row(strong_accel(), run_id="shadow-old", cycle=1, iwm_after_pct=0.12, created="2026-10-09T02:15:00")
    report = build_validation_dataset([row])
    accounting = report["semiconductor_context"]["semiconductor_candidate_accounting"]
    assert (
        accounting["agree"]
        + accounting["disagree"]
        + accounting["mixed"]
        + accounting["unavailable"]
        + accounting["excluded"]
        == accounting["labeled_candidates"]
    )
    assert accounting["accounting_valid"] is True
    assert accounting["accounted_total"] == accounting["labeled_candidates"]
    assert report["orders_submitted"] == 0
    assert SignalEngineConfig().entry_score_threshold == 70.0
    assert "session_date_diagnostics" not in inspect.getsource(v3_mod)


def test_report_prints_session_date_diagnostics(capsys):
    rows = [
        _row(
            strong_accel(),
            run_id="shadow-2026-10-09-balanced-v3-confirmed-open",
            cycle=1,
            iwm_after_pct=0.12,
            created="2026-10-09T02:15:00",
        )
    ]

    def loader(_settings, _run_ids, limit, session_dates=None):
        del session_dates
        return rows[:limit]

    assert report_script.run_report(_shadow_settings(), rows_loader=loader) == 0
    text = capsys.readouterr().out
    assert "session_date_diagnostics:" in text
    assert "run_id: shadow-2026-10-09-balanced-v3-confirmed-open" in text
    assert "first_raw_timestamp: 2026-10-09T02:15:00" in text
    assert "timestamp_semantics: UTC-naive" in text
    assert "first_et: 2026-10-08T22:15:00-04:00" in text
    assert "derived_dates: 2026-10-08" in text
    assert "generalization_status: insufficient_sessions" in text
