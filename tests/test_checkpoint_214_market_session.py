"""Checkpoint 2.14 — Market Session Guard and Shadow Window Quality Filter (no orders)."""

from __future__ import annotations

import importlib.util
import inspect
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import sessionmaker

import backend.database  # noqa: F401 — legacy models share Base
import backend.db.models  # noqa: F401
import backend.market_session.models as session_models_mod
import backend.market_session.us_equity_session as session_mod
import backend.shadow_mode.analytics as analytics_mod
import backend.shadow_mode.report as report_mod
import backend.shadow_mode.runner as runner_mod
import backend.shadow_mode.session_quality as quality_mod
from backend.config.settings import ConfigurationError, Settings, load_settings
from backend.db.base import Base
from backend.db.models import ShadowSignalLog
from backend.execution.execution_router import ExecutionRouter
from backend.market_data.config import validate_market_data_settings
from backend.market_session import (
    MarketSessionGuard,
    NoHolidayCalendar,
    SessionConfigError,
    SessionGuardConfig,
    SessionLabel,
    USEquitySession,
    enough_time_remaining,
    evaluate_session_guard,
    get_session_status,
    parse_session_now_override,
)
from backend.repositories.shadow_signal_repository import (
    ShadowSignalRepository,
    ShadowTableMissingError,
    open_shadow_repository,
)
from backend.risk.live_guard import LiveTradingBlockedError, assert_order_execution_allowed
from backend.risk.models import OrderIntent, RiskContext
from backend.shadow_mode import ShadowModeRunner, ShadowRunConfig, ShadowSignalLogger
from backend.shadow_mode.models import SESSION_FIELDS
from backend.shadow_mode.session_quality import (
    effective_session_label,
    overall_market_window_quality,
    session_breakdown,
    split_bad_session_windows,
    window_quality,
)
from backend.signals.tna_tza_signal_engine import SignalEngineConfig
from scripts import analyze_shadow_signal_logs as analyze_script
from scripts import recommend_shadow_windows as recommend_script
from scripts import report_shadow_signal_logs as report_script
from scripts import run_shadow_signal_logger as shadow_script
from tests.test_checkpoint_212_shadow_mode import (
    BULL,
    SECRETS,
    FakeClock,
    FakeTokenProvider,
    StreamFactory,
    _engine,
    _run_cli,
    _settings,
    _stream,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
_NO_ENV = Path("/nonexistent/.env")
EDT = timezone(timedelta(hours=-4))


def et(hour, minute=0, day=2, month=10):
    """Wall-clock time in New York during EDT (2026-10-02 is a Friday, 10-03 a Saturday)."""
    return datetime(2026, month, day, hour, minute, tzinfo=EDT)


def iso(hour, minute=0, day=2) -> str:
    return et(hour, minute, day).isoformat()


@pytest.fixture
def db_session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


# ---------------------------------------------------------------------------
# Session labels
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "now, label",
    [
        (et(11, 0, day=3), SessionLabel.WEEKEND),  # Saturday
        (et(11, 0, day=4), SessionLabel.WEEKEND),  # Sunday
        (et(8, 0), SessionLabel.PRE_MARKET),
        (et(9, 29), SessionLabel.PRE_MARKET),
        (et(9, 30), SessionLabel.REGULAR_HOURS),
        (et(11, 0), SessionLabel.REGULAR_HOURS),
        (et(15, 39), SessionLabel.REGULAR_HOURS),
        (et(15, 40), SessionLabel.NEAR_CLOSE),
        (et(15, 59), SessionLabel.NEAR_CLOSE),
        (et(16, 0), SessionLabel.AFTER_HOURS),
        (et(18, 30), SessionLabel.AFTER_HOURS),
    ],
)
def test_session_labels(now, label):
    status = get_session_status(now)
    assert status.label == label
    assert status.is_regular_hours is (label in (SessionLabel.REGULAR_HOURS, SessionLabel.NEAR_CLOSE))
    assert status.is_near_close is (label == SessionLabel.NEAR_CLOSE)


def test_session_handles_utc_input_and_dst():
    # 2026-01-16 is EST (UTC-5): 14:45 UTC = 09:45 ET; 2026-07-16 is EDT: 13:45 UTC = 09:45 ET.
    winter = get_session_status(datetime(2026, 1, 16, 14, 45, tzinfo=timezone.utc))
    summer = get_session_status(datetime(2026, 7, 16, 13, 45, tzinfo=timezone.utc))
    assert winter.label == summer.label == SessionLabel.REGULAR_HOURS
    assert winter.minutes_to_close == summer.minutes_to_close == 375.0
    assert get_session_status(datetime(2026, 1, 16, 14, 15, tzinfo=timezone.utc)).label == SessionLabel.PRE_MARKET


@pytest.mark.parametrize(
    "utc",
    [
        datetime(2026, 3, 8, 6, 59, tzinfo=timezone.utc),
        datetime(2026, 3, 8, 7, 0, tzinfo=timezone.utc),
        datetime(2026, 11, 1, 5, 59, tzinfo=timezone.utc),
        datetime(2026, 11, 1, 6, 0, tzinfo=timezone.utc),
        datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc),
        datetime(2027, 1, 15, 12, 0, tzinfo=timezone.utc),
    ],
)
def test_builtin_dst_fallback_matches_zoneinfo(utc):
    from zoneinfo import ZoneInfo

    assert session_mod._us_eastern_fallback(utc).utcoffset() == utc.astimezone(ZoneInfo("America/New_York")).utcoffset()


def test_holiday_calendar_is_pluggable():
    class OneHoliday(NoHolidayCalendar):
        def holiday_name(self, day):
            return "Test Day" if day == et(11).date() else None

    status = USEquitySession(holiday_calendar=OneHoliday()).status(et(11))
    assert status.label == SessionLabel.CLOSED_UNKNOWN_HOLIDAY
    assert status.is_trading_day is False and status.holiday_name == "Test Day"
    decision = evaluate_session_guard(status, SessionGuardConfig(enforce=True), required_seconds=10)
    assert decision.blocked and decision.reason_code == "closed_unknown_holiday"

    class EarlyClose(NoHolidayCalendar):
        def early_close(self, day):
            return session_mod.time(13, 0)

    early = USEquitySession(holiday_calendar=EarlyClose()).status(et(12, 50))
    assert early.label == SessionLabel.NEAR_CLOSE and early.minutes_to_close == 10.0


def test_near_close_threshold_follows_min_minutes_before_close():
    assert get_session_status(et(15, 25), near_close_minutes=40).label == SessionLabel.NEAR_CLOSE
    assert get_session_status(et(15, 25), near_close_minutes=20).label == SessionLabel.REGULAR_HOURS


# ---------------------------------------------------------------------------
# Enough time before close + guard
# ---------------------------------------------------------------------------


def test_enough_time_before_close_calculation():
    status = get_session_status(et(15, 30))  # 30 min = 1800 s left
    assert status.minutes_to_close == 30.0 and status.seconds_to_close == 1800.0
    assert enough_time_remaining(status, 1800) is True
    assert enough_time_remaining(status, 1700) is True
    assert enough_time_remaining(status, 1801) is False
    assert enough_time_remaining(get_session_status(et(17, 0)), 1) is False
    assert enough_time_remaining(get_session_status(et(11, 0, day=3)), 1) is False


def _guard(now, required=100.0, **config):
    return evaluate_session_guard(get_session_status(now, near_close_minutes=config.get("min_minutes_before_close", 20)),
                                  SessionGuardConfig(**config), required_seconds=required)


def test_weekend_blocked_when_enforce_is_true():
    decision = _guard(et(11, 0, day=3), enforce=True)
    assert decision.blocked and not decision.passed
    assert decision.reason_code == "weekend" and decision.mode == "blocked"


def test_pre_market_blocked_when_enforce_is_true():
    assert _guard(et(9, 0), enforce=True).reason_code == "pre_market"
    assert _guard(et(9, 0), enforce=True).blocked


def test_after_hours_blocked_when_enforce_is_true():
    decision = _guard(et(17, 0), enforce=True)
    assert decision.blocked and decision.reason_code == "after_hours"
    allowed = _guard(et(17, 0), enforce=True, allow_after_hours=True)
    assert not allowed.blocked and allowed.passed and allowed.reason_code == "after_hours_allowed"
    assert allowed.warnings  # still clearly labelled
    assert _guard(et(11, 0, day=3), enforce=True, allow_after_hours=True).blocked  # weekend never allowed


def test_near_close_blocked_unless_allow_near_close():
    decision = _guard(et(15, 45), enforce=True)
    assert decision.blocked and decision.reason_code == "near_close"
    allowed = _guard(et(15, 45), enforce=True, allow_near_close=True)
    assert allowed.passed and not allowed.blocked and allowed.reason_code == "near_close_allowed"


def test_not_enough_time_before_close_blocked():
    # 15:35 -> 25 min (1500 s) left, outside the 20 min buffer, but a cycle needs 1800 s.
    decision = _guard(et(15, 35), required=1800, enforce=True)
    assert decision.blocked and decision.reason_code == "not_enough_time_before_close"
    assert _guard(et(15, 35), required=1400, enforce=True).passed
    # allow-near-close does not bypass the time check
    late = _guard(et(15, 58), required=300, enforce=True, allow_near_close=True)
    assert late.blocked and late.reason_code == "not_enough_time_before_close"


def test_regular_hours_pass():
    decision = _guard(et(11, 0), enforce=True)
    assert decision.passed and not decision.blocked and decision.reason_code == "ok" and not decision.warnings


@pytest.mark.parametrize("now", [et(11, 0, day=3), et(8, 0), et(15, 50), et(17, 0)])
def test_default_mode_warns_but_does_not_block(now):
    decision = _guard(now, enforce=False)
    assert decision.blocked is False
    assert decision.passed is False and decision.mode == "warn"
    assert decision.warnings and "warn only" in decision.reason


def test_record_fields_and_guard_config_validation():
    fields = _guard(et(15, 45), enforce=True, allow_near_close=True).record_fields()
    assert set(fields) == set(SESSION_FIELDS)
    assert fields["session_label"] == "near_close" and fields["session_is_near_close"] is True
    assert fields["session_minutes_to_close"] == 15.0
    with pytest.raises(SessionConfigError):
        SessionGuardConfig(min_minutes_before_close=-1).validate()
    with pytest.raises(SessionConfigError):
        SessionGuardConfig(min_minutes_before_close=500).validate()


def test_session_now_override_parsing():
    assert parse_session_now_override(None) is None
    assert parse_session_now_override("2026-10-02T15:45:00-04:00") == et(15, 45)
    assert parse_session_now_override("2026-10-02T19:45:00Z") == et(15, 45)
    for bad in ("2026-10-02T15:45:00", "not-a-date"):
        with pytest.raises(SessionConfigError):
            parse_session_now_override(bad)


def test_runner_market_hours_helper_uses_session_module():
    assert runner_mod.is_regular_market_hours(et(15, 50)) is True
    assert runner_mod.is_regular_market_hours(et(16, 10)) is False


# ---------------------------------------------------------------------------
# Runner guard + session metadata logging
# ---------------------------------------------------------------------------


def _sequence_clock(*times):
    values = list(times)

    def clock():
        return values.pop(0) if len(values) > 1 else values[0]

    return clock


def _guarded_runner(scripts, guard, *, cycles=1, repository=None):
    clock = FakeClock()
    events: list = []
    runner = ShadowModeRunner(
        validate_market_data_settings(_settings()),
        _engine(),
        ShadowSignalLogger(repository),
        ShadowRunConfig(cycles=cycles, signal_duration_seconds=4.0, pause_seconds=0.0, followup_seconds=0.0),
        run_id="shadow-session-test",
        provider=FakeTokenProvider(clock),
        stream_factory=StreamFactory(clock, scripts),
        sleep=lambda _s: None,
        on_event=lambda kind, payload: events.append((kind, payload)),
        session_guard=guard,
    )
    return runner, events


def test_session_metadata_is_logged_to_record_and_db(db_session):
    guard = MarketSessionGuard(SessionGuardConfig(enforce=True), clock=lambda: et(11, 0))
    runner, events = _guarded_runner([_stream(BULL)], guard, repository=ShadowSignalRepository(db_session))
    summary = runner.run()
    record = summary.records[0]
    assert record.session_label == "regular_hours"
    assert record.session_guard_passed is True and record.session_guard_reason.startswith("ok")
    assert record.to_dict()["session_minutes_to_close"] == 300.0

    row = db_session.query(ShadowSignalLog).one()
    assert row.session_label == "regular_hours"
    assert row.session_is_regular_hours is True and row.session_is_near_close is False
    assert row.session_minutes_to_close == 300.0
    assert row.session_guard_passed is True and row.session_guard_reason.startswith("ok")
    assert row.submitted is False and row.production_execution_blocked is True
    assert [k for k, _ in events if k == "session"]


def test_enforced_runner_stops_before_cycle_entering_near_close():
    guard = MarketSessionGuard(SessionGuardConfig(enforce=True), clock=_sequence_clock(et(15, 30), et(15, 41)))
    runner, events = _guarded_runner([_stream(BULL), _stream(BULL)], guard, cycles=2)
    summary = runner.run()
    assert summary.cycles_completed == 1
    assert summary.session_blocked is True and "near_close" in summary.session_block_reason
    assert summary.orders_submitted == 0
    assert any(kind == "session_blocked" for kind, _ in events)


def test_default_runner_logs_weekend_label_without_blocking(db_session):
    guard = MarketSessionGuard(SessionGuardConfig(enforce=False), clock=lambda: et(11, 0, day=3))
    runner, _ = _guarded_runner([_stream(BULL)], guard, repository=ShadowSignalRepository(db_session))
    summary = runner.run()
    assert summary.cycles_completed == 1 and summary.session_blocked is False
    row = db_session.query(ShadowSignalLog).one()
    assert row.session_label == "weekend"
    assert row.session_guard_passed is False and "warn only" in row.session_guard_reason


def test_runner_without_guard_leaves_session_fields_null(db_session):
    runner, _ = _guarded_runner([_stream(BULL)], None, repository=ShadowSignalRepository(db_session))
    runner.run()
    row = db_session.query(ShadowSignalLog).one()
    assert all(getattr(row, name) is None for name in SESSION_FIELDS)


# ---------------------------------------------------------------------------
# CLI guard
# ---------------------------------------------------------------------------


def _no_db(_settings):
    raise AssertionError("DB must not be opened when the session guard blocks")


@pytest.mark.parametrize(
    "override, code",
    [(iso(11, 0, day=3), "weekend"), (iso(8, 30), "pre_market"), (iso(17, 0), "after_hours"), (iso(15, 45), "near_close")],
)
def test_cli_enforce_blocks_bad_sessions_before_any_network(capsys, override, code):
    exit_code, out, factory, _ = _run_cli(
        capsys,
        [_stream(BULL)],
        cycles=1,
        with_db=True,
        repository_factory=_no_db,
        enforce_market_session=True,
        session_now_override=override,
    )
    assert exit_code == shadow_script.SESSION_BLOCKED_EXIT_CODE == 4
    assert factory.calls == 0
    assert f"session_label: {code}" in out
    assert "session_guard: blocked" in out
    assert "error_step: market_session" in out
    assert shadow_script.FAILED in out
    for secret in SECRETS:
        assert secret not in out


def test_cli_enforce_not_enough_time_before_close(capsys):
    exit_code, out, factory, _ = _run_cli(
        capsys,
        [],
        cycles=1,
        signal_duration_seconds=300.0,
        followup_seconds=900.0,
        pause_seconds=600.0,
        enforce_market_session=True,
        session_now_override=iso(15, 35),
    )
    assert exit_code == 4 and factory.calls == 0
    assert "not_enough_time_before_close" in out
    assert "seconds_needed_per_cycle: 1800" in out


def test_cli_allow_flags_let_enforced_run_proceed(capsys):
    code, out, factory, _ = _run_cli(
        capsys, [_stream(BULL)], cycles=1, enforce_market_session=True, allow_after_hours=True,
        session_now_override=iso(17, 0),
    )
    assert code == 0 and factory.calls == 1
    assert "session: after_hours" in out and "after_hours_allowed" in out

    code, out, factory, _ = _run_cli(
        capsys, [_stream(BULL)], cycles=1, enforce_market_session=True, allow_near_close=True,
        session_now_override=iso(15, 45),
    )
    assert code == 0 and factory.calls == 1
    assert "session: near_close" in out


def test_cli_default_mode_warns_does_not_block_and_logs_session(capsys, db_session):
    code, out, factory, _ = _run_cli(
        capsys,
        [_stream(BULL)],
        cycles=1,
        with_db=True,
        repository_factory=lambda _s: ShadowSignalRepository(db_session),
        session_now_override=iso(11, 0, day=3),
    )
    assert code == 0 and factory.calls == 1
    assert "--- market session" in out
    assert "session_label: weekend" in out
    assert "enforce_market_session: false" in out
    assert "session_guard: warn" in out
    assert "warning: market session warning: weekend" in out
    assert "session: weekend" in out
    assert "session_labels: weekend=1" in out
    row = db_session.query(ShadowSignalLog).one()
    assert row.session_label == "weekend" and row.session_guard_passed is False


def test_cli_always_prints_session_status_in_regular_hours(capsys):
    code, out, _, _ = _run_cli(capsys, [_stream(BULL)], cycles=1, session_now_override=iso(11, 0))
    assert code == 0
    assert "session_label: regular_hours" in out
    assert "session_minutes_to_close: 300.0" in out
    assert "session_guard: passed" in out
    assert "warning: outside regular US market hours" not in out


def test_cli_json_includes_session_and_redacts_secrets(capsys):
    code, out, _, _ = _run_cli(
        capsys, [_stream(BULL)], cycles=1, json_output=True, enforce_market_session=True,
        session_now_override=iso(11, 0),
    )
    assert code == 0
    doc = json.loads(out)
    assert doc["market_session"]["session_label"] == "regular_hours"
    assert doc["session_guard"]["enforce_market_session"] is True
    assert doc["cycles"][0]["session_label"] == "regular_hours"
    assert doc["cycles"][0]["session_guard_passed"] is True
    assert doc["run"]["orders_submitted"] == 0 and doc["run"]["session_blocked"] is False
    for secret in SECRETS:
        assert secret not in out

    code, out, _, _ = _run_cli(
        capsys, [], cycles=1, json_output=True, enforce_market_session=True, session_now_override=iso(11, 0, day=3)
    )
    doc = json.loads(out)
    assert code == 4 and doc["shadow_signal_logger"] == "failed" and doc["error"]["step"] == "market_session"
    for secret in SECRETS:
        assert secret not in out


@pytest.mark.parametrize("kwargs", [{"session_now_override": "2026-10-02T11:00:00"}, {"min_minutes_before_close": -5}])
def test_cli_invalid_session_args_exit_2(capsys, kwargs):
    code, out, factory, _ = _run_cli(capsys, [], cycles=1, **kwargs)
    assert code == 2 and factory.calls == 0


def test_cli_main_parses_session_args(monkeypatch):
    captured = {}
    monkeypatch.setattr(shadow_script, "load_settings", lambda **_kw: _settings())
    monkeypatch.setattr(shadow_script, "run_shadow", lambda settings, **kw: captured.update(kw) or 0)
    argv = [
        "--enforce-market-session", "--allow-near-close", "--allow-after-hours",
        "--min-minutes-before-close", "30", "--session-now-override", iso(11, 0),
    ]
    assert shadow_script.main(argv) == 0
    assert captured["enforce_market_session"] is True
    assert captured["allow_near_close"] is True and captured["allow_after_hours"] is True
    assert captured["min_minutes_before_close"] == 30.0
    assert captured["session_now_override"] == iso(11, 0)

    captured.clear()
    assert shadow_script.main([]) == 0
    assert captured["enforce_market_session"] is False  # default: warn only


# ---------------------------------------------------------------------------
# Migration 004 + legacy tables
# ---------------------------------------------------------------------------


def _migration(name):
    path = REPO_ROOT / "alembic" / "versions" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"migration_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _legacy_engine(url="sqlite://"):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    engine = create_engine(url)
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            _migration("003_shadow_signal_log").upgrade()
        conn.execute(text("INSERT INTO shadow_signal_log (run_id, cycle_number, decision) VALUES ('old-run', 1, 'skip')"))
    return engine


def test_migration_004_adds_nullable_session_columns_without_breaking_old_rows():
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    mod = _migration("004_shadow_session_metadata")
    assert mod.revision == "004_shadow_session_metadata"
    assert mod.down_revision == "003_shadow_signal_log"

    engine = _legacy_engine()
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            mod.upgrade()
            mod.upgrade()  # idempotent
        inspector = sa_inspect(conn)
        columns = {c["name"]: c for c in inspector.get_columns("shadow_signal_log")}
        assert set(columns) == set(ShadowSignalLog.__table__.columns.keys())
        assert all(columns[name]["nullable"] for name in SESSION_FIELDS)
        old = conn.execute(text("SELECT run_id, session_label, session_guard_passed FROM shadow_signal_log")).one()
        assert tuple(old) == ("old-run", None, None)

        with Operations.context(MigrationContext.configure(conn)):
            mod.downgrade()
        after = {c["name"] for c in sa_inspect(conn).get_columns("shadow_signal_log")}
        assert after == set(ShadowSignalLog.__table__.columns.keys()) - set(SESSION_FIELDS)
        assert conn.execute(text("SELECT COUNT(*) FROM shadow_signal_log")).scalar() == 1


def test_migration_004_skips_missing_table():
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            _migration("004_shadow_session_metadata").upgrade()
        assert "shadow_signal_log" not in sa_inspect(conn).get_table_names()


def test_open_shadow_repository_handles_legacy_table(tmp_path):
    url = f"sqlite:///{tmp_path / 'legacy.db'}"
    _legacy_engine(url).dispose()
    with pytest.raises(ShadowTableMissingError) as excinfo:
        open_shadow_repository(url, create_table=False)  # read-only path never alters the schema
    assert "alembic upgrade head" in str(excinfo.value) and "004" in str(excinfo.value)

    repo = open_shadow_repository(url, create_table=True)  # --with-db path adds nullable columns
    rows = repo.list_signals()
    assert len(rows) == 1 and rows[0].run_id == "old-run" and rows[0].session_label is None
    assert isinstance(open_shadow_repository(url, create_table=False), ShadowSignalRepository)


# ---------------------------------------------------------------------------
# Session quality analytics
# ---------------------------------------------------------------------------


def _row(run_id, cycle, *, label, fresh=True, decision="skip", age=None, created=None):
    created = created or (et(11, 0) + timedelta(minutes=cycle)).astimezone(timezone.utc).replace(tzinfo=None)
    quote_age = age if age is not None else (0.3 if fresh else 2.5)
    return {
        "id": cycle,
        "run_id": run_id,
        "cycle_number": cycle,
        "created_at": created.isoformat(),
        "decision": decision,
        "selected_symbol": None,
        "skip_reason": None if decision != "skip" else ("unclear_market_direction" if fresh else "stale_market_data"),
        "freshness_gate_passed": fresh,
        "bullish_score": 0.0,
        "bearish_score": 0.0,
        "confidence_score": 0.0,
        "submitted": False,
        "production_execution_blocked": True,
        "session_label": label,
        **{f"quote_age_{s}": quote_age for s in ("tna", "tza", "iwm", "spy", "qqq")},
    }


def close_run():
    """Like the real close run: 15 near-close cycles, 3 fresh (20%), 12 stale skips."""
    return [_row("close-run", i, label="near_close", fresh=i <= 3) for i in range(1, 16)]


def good_run():
    return [_row("good-run", 100 + i, label="regular_hours") for i in range(1, 11)]


def test_session_breakdown_by_label():
    breakdown = session_breakdown(close_run() + good_run())
    assert list(breakdown) == ["regular_hours", "near_close"]
    near = breakdown["near_close"]
    assert near["cycles"] == 15
    assert near["freshness_pass_pct"] == 20.0
    assert near["stale_skip_count"] == 12
    assert near["avg_quote_age_seconds"] == pytest.approx((3 * 0.3 + 12 * 2.5) / 15, abs=1e-3)
    assert near["window_quality"] == "bad"
    regular = breakdown["regular_hours"]
    assert regular["freshness_pass_pct"] == 100.0 and regular["stale_skip_count"] == 0
    assert regular["window_quality"] == "good"
    assert regular["label_sources"] == {"logged": 10}


def test_old_rows_get_label_derived_from_created_at():
    old = _row("old", 1, label=None, created=datetime(2026, 10, 2, 19, 50))  # 15:50 ET, naive UTC as stored
    assert effective_session_label(old) == ("near_close", "derived_from_created_at")
    weekend = _row("old", 2, label=None, created=datetime(2026, 10, 3, 15, 0))
    assert effective_session_label(weekend)[0] == "weekend"
    assert effective_session_label({"session_label": None}) == ("unknown", "unknown")


@pytest.mark.parametrize(
    "label, fresh, status, expected",
    [
        ("after_hours", 100.0, None, "bad"),
        ("weekend", 100.0, None, "bad"),
        ("pre_market", 100.0, None, "bad"),
        ("near_close", 20.0, None, "bad"),
        ("regular_hours", 65.0, None, "bad"),
        ("near_close", 100.0, None, "weak"),
        ("regular_hours", 80.0, None, "weak"),
        ("regular_hours", 100.0, "flat/choppy", "weak"),
        ("unknown", 100.0, None, "weak"),
        ("regular_hours", 100.0, "moving", "good"),
    ],
)
def test_window_quality_rules(label, fresh, status, expected):
    assert window_quality(label, fresh, market_window_status=status)[0] == expected


def test_overall_market_window_quality():
    assert overall_market_window_quality(close_run() + good_run())["market_window_quality"] == "bad"  # 52% fresh
    assert overall_market_window_quality(good_run())["market_window_quality"] == "good"
    weak = overall_market_window_quality(good_run(), market_window_status="flat/choppy")
    assert weak["market_window_quality"] == "weak" and "market flat/choppy" in weak["reasons"]
    assert overall_market_window_quality([])["market_window_quality"] == "bad"


def test_split_bad_session_windows_keeps_rows_unchanged():
    rows = close_run() + good_run() + [_row("ah-run", 200, label="after_hours")]
    snapshot = json.dumps(rows, sort_keys=True)
    kept, excluded = split_bad_session_windows(rows)
    assert {r["run_id"] for r in kept} == {"good-run"} and len(kept) == 10
    assert all(any(k is r for r in rows) for k in kept)
    assert {(w["run_id"], w["session_label"]) for w in excluded} == {("close-run", "near_close"), ("ah-run", "after_hours")}
    assert json.dumps(rows, sort_keys=True) == snapshot


def test_analytics_includes_session_breakdown_and_quality():
    analysis = analytics_mod.analyze_rows(close_run() + good_run())
    assert analysis["session_breakdown"]["near_close"]["stale_skip_count"] == 12
    assert analysis["market_window_quality"]["market_window_quality"] == "bad"
    runs = {r["run_id"]: r for r in analytics_mod.compare_runs(close_run() + good_run())}
    assert runs["close-run"]["market_window_quality"] == "bad"
    assert runs["close-run"]["session_labels"] == {"near_close": 15}
    assert runs["good-run"]["market_window_quality"] == "good"


def _loader(rows):
    return lambda _settings, _run_ids, _limit: list(rows)


def test_analytics_can_exclude_bad_windows(capsys):
    rows = close_run() + good_run()
    code = analyze_script.run_analysis(
        _settings(), json_output=True, simulate=True, exclude_bad_session_windows=True, rows_loader=_loader(rows)
    )
    doc = json.loads(capsys.readouterr().out)
    assert code == 0
    assert doc["exclude_bad_session_windows"] is True
    assert doc["rows_before_exclusion"] == 25 and doc["rows_after_exclusion"] == 10
    assert [w["run_id"] for w in doc["excluded_session_windows"]] == ["close-run"]
    assert doc["analysis"]["total_cycles"] == 10
    assert doc["analysis"]["run_ids"] == ["good-run"]
    assert doc["threshold_simulation"]["rows_evaluated"] == 10
    assert doc["threshold_simulation"]["orders_submitted"] == 0
    assert doc["threshold_simulation"]["writes_to_database"] is False
    for secret in SECRETS:
        assert secret not in json.dumps(doc)

    code = analyze_script.run_analysis(_settings(), exclude_bad_session_windows=True, rows_loader=_loader(rows))
    out = capsys.readouterr().out
    assert code == 0
    assert "--- excluded bad session windows ---" in out
    assert "excluded: run=close-run session=near_close cycles=15 fresh=20%" in out
    assert "rows_before_exclusion: 25  rows_after_exclusion: 10" in out
    assert "market_window_quality: good" in out


def test_analytics_without_exclusion_reports_bad_quality(capsys):
    code = analyze_script.run_analysis(_settings(), rows_loader=_loader(close_run() + good_run()))
    out = capsys.readouterr().out
    assert code == 0
    assert "--- session breakdown ---" in out
    assert "near_close: cycles=15" in out and "stale_skips=12" in out and "quality=bad" in out
    assert "market_window_quality: bad" in out
    assert "exclude_bad_session_windows: false" in out


def test_analytics_all_bad_windows_warns(capsys):
    code = analyze_script.run_analysis(_settings(), exclude_bad_session_windows=True, rows_loader=_loader(close_run()))
    out = capsys.readouterr().out
    assert code == 0 and "every window was rated bad" in out


def test_report_breakdown_and_exclusion(capsys, db_session):
    repo = ShadowSignalRepository(db_session)
    guard_ok = MarketSessionGuard(SessionGuardConfig(), clock=lambda: et(11, 0))
    guard_late = MarketSessionGuard(SessionGuardConfig(), clock=lambda: et(17, 0))
    for guard in (guard_ok, guard_late):
        runner, _ = _guarded_runner([_stream(BULL)], guard, repository=repo)
        runner.run()
    loader = lambda _s, run_id, limit: repo.list_signals(run_id=run_id, limit=limit)  # noqa: E731

    assert report_script.run_report(_settings(), rows_loader=loader) == 0
    out = capsys.readouterr().out
    assert "--- session breakdown ---" in out
    assert "regular_hours: cycles=1" in out and "after_hours: cycles=1" in out
    assert "market_window_quality:" in out

    assert report_script.run_report(_settings(), rows_loader=loader, exclude_bad_session_windows=True, json_output=True) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["rows_before_exclusion"] == 2 and doc["report"]["total_signals"] == 1
    assert doc["excluded_session_windows"][0]["session_label"] == "after_hours"
    assert doc["report"]["submitted_count"] == 0


@pytest.mark.parametrize("script", [analyze_script, report_script])
def test_cli_main_accepts_exclude_flag(script, monkeypatch):
    captured = {}
    monkeypatch.setattr(script, "load_settings", lambda **_kw: _settings())
    target = "run_analysis" if script is analyze_script else "run_report"
    monkeypatch.setattr(script, target, lambda settings, **kw: captured.update(kw) or 0)
    assert script.main(["--exclude-bad-session-windows"]) == 0
    assert captured["exclude_bad_session_windows"] is True


# ---------------------------------------------------------------------------
# Recommended windows helper
# ---------------------------------------------------------------------------


def test_recommend_shadow_windows_prints_windows_and_commands(capsys):
    assert recommend_script.main(["--now-override", iso(10, 0)]) == 0
    out = capsys.readouterr().out
    for window in ("09:45-10:30 ET", "11:00-14:30 ET", "15:00-15:30 ET"):
        assert window in out
    assert "avoid the last 20 minutes" in out
    assert "active_recommended_window: open_confirmation" in out
    assert "--enforce-market-session" in out and "--with-db" in out
    assert "--exclude-bad-session-windows" in out


def test_recommend_shadow_windows_json_and_cycle_fit(capsys):
    assert recommend_script.main(["--json", "--now-override", iso(11, 0, day=3)]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["session"]["session_label"] == "weekend"
    assert doc["active_recommended_window"] is None
    fits = {w["name"]: w["suggested_cycles"] for w in doc["recommended_windows"]}
    assert fits == {"open_confirmation": 27, "midday_sample": 100, "power_hour_early": 18}
    for w in doc["recommended_windows"]:
        assert w["suggested_cycles"] * doc["seconds_per_cycle"] - 10 <= w["minutes"] * 60
    assert doc["orders_submitted"] == 0
    assert recommend_script.main(["--now-override", "2026-10-02T10:00:00"]) == 2


def test_recommend_helper_loads_no_credentials():
    source = inspect.getsource(recommend_script)
    for token in ("load_settings", "DXLinkQuoteTokenProvider", "open_shadow_repository", "requests"):
        assert token not in source


# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------


_FORBIDDEN = (
    "order_executor",
    "OrderExecutor",
    "execution_router",
    "ExecutionRouter",
    "tastytrade_sandbox",
    "TastytradeSandboxAdapter",
    "sandbox_worker",
    "SandboxBotWorker",
    "submit_equity_order",
    "execute_order",
    "dry_run_equity_order",
)


@pytest.mark.parametrize(
    "module",
    [
        session_models_mod,
        session_mod,
        quality_mod,
        runner_mod,
        analytics_mod,
        report_mod,
        shadow_script,
        report_script,
        analyze_script,
        recommend_script,
    ],
)
def test_no_order_submission_path_is_introduced(module):
    source = inspect.getsource(module)
    for token in _FORBIDDEN:
        assert token not in source, f"{module.__name__} references {token}"


def test_session_guard_has_no_order_capability():
    guard = MarketSessionGuard(SessionGuardConfig(enforce=True), clock=lambda: et(11, 0))
    for name in ("execute", "submit", "place_order", "route", "_executor", "_adapter", "_router"):
        assert not hasattr(guard, name)


def test_signal_engine_thresholds_unchanged():
    config = SignalEngineConfig()
    assert (config.entry_score_threshold, config.opposing_score_max, config.min_score_gap) == (70.0, 40.0, 20.0)
    assert config.max_quote_age_seconds == 1.0
    for module in (session_models_mod, session_mod, quality_mod):
        source = inspect.getsource(module)
        assert "entry_score_threshold" not in source and "SignalEngineConfig" not in source


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
    checks = runner_mod.production_execution_block_checks()
    assert checks and all(checks.values())
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


def test_no_secrets_printed_by_session_paths(capsys, db_session):
    outputs = []
    for kwargs in (
        {"session_now_override": iso(11, 0)},
        {"session_now_override": iso(15, 45), "enforce_market_session": True},
        {"session_now_override": iso(11, 0, day=3), "json_output": True},
    ):
        _, out, _, _ = _run_cli(
            capsys, [_stream(BULL)], cycles=1, with_db=True,
            repository_factory=lambda _s: ShadowSignalRepository(db_session), **kwargs,
        )
        outputs.append(out)
    analyze_script.run_analysis(_settings(), exclude_bad_session_windows=True, rows_loader=_loader(close_run()))
    report_script.run_report(_settings(), exclude_bad_session_windows=True, rows_loader=_loader(good_run()))
    outputs.append(capsys.readouterr().out)
    for out in outputs:
        for secret in SECRETS:
            assert secret not in out
