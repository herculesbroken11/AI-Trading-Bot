"""Checkpoint 2.12 — Shadow Mode Signal Logger (observation / logging only, no orders)."""

from __future__ import annotations

import importlib.util
import inspect
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

import backend.database  # noqa: F401 — legacy models share Base
import backend.db.models  # noqa: F401
import backend.repositories.shadow_signal_repository as repo_mod
import backend.shadow_mode.logger as logger_mod
import backend.shadow_mode.models as models_mod
import backend.shadow_mode.report as report_mod
import backend.shadow_mode.runner as runner_mod
from backend.config.settings import ConfigurationError, Settings, load_settings
from backend.db.base import Base
from backend.db.models import ShadowSignalLog
from backend.execution.execution_router import ExecutionRouter
from backend.market_data.config import validate_market_data_settings
from backend.market_data.dxlink_stream import DXLinkStreamClient, QuoteToken, StreamConnectionClosed
from backend.market_data.stream_models import DEFAULT_EVENT_FIELDS
from backend.market_data.tastytrade_market_data import MarketDataError
from backend.repositories.shadow_signal_repository import (
    ShadowSignalRepository,
    ShadowTableMissingError,
    open_shadow_repository,
    safe_database_label,
)
from backend.risk.live_guard import LiveTradingBlockedError, assert_order_execution_allowed
from backend.risk.models import OrderIntent, RiskContext
from backend.shadow_mode import (
    MAX_CYCLES,
    ShadowCycleRecord,
    ShadowModeRunner,
    ShadowRunConfig,
    ShadowSafetyError,
    ShadowSignalLogger,
    compute_followup_outcome,
    summarize_shadow_logs,
)
from backend.signals.models import MarketSnapshot, SymbolQuote
from backend.signals.tna_tza_signal_engine import SignalEngineConfig, TnaTzaSignalEngine
from scripts import report_shadow_signal_logs as report_script
from scripts import run_shadow_signal_logger as shadow_script

REPO_ROOT = Path(__file__).resolve().parents[1]
_NO_ENV = Path("/nonexistent/.env")
MD_CLIENT_ID = "mdcid-1234567890-abcdef"
MD_SECRET = "mdsecret-ZZZZ-9876543210-qwerty"
MD_REFRESH = "mdrefresh-AAAA-1111222233334444-token"
SANDBOX_SECRET = "sandbox-secret-00000000000"
SANDBOX_REFRESH = "sandbox-refresh-000000000000"
QUOTE_TOKEN = "dxquotetoken-CCCC-0000111122223333-secret"
DB_PASSWORD = "dbpass-SECRET-7777"
SECRETS = (MD_CLIENT_ID, MD_SECRET, MD_REFRESH, SANDBOX_SECRET, SANDBOX_REFRESH, QUOTE_TOKEN, DB_PASSWORD)
DXLINK_URL = "wss://tasty-openapi-ws.dxfeed.com/realtime"
NOW = datetime(2026, 10, 2, 15, 0, 0, tzinfo=timezone.utc)


def _settings(**overrides) -> Settings:
    base = dict(
        trading_mode="sandbox",
        tastytrade_env="sandbox",
        live_trading_enabled=False,
        tastytrade_client_id="sandbox-cid-000000000000",
        tastytrade_client_secret=SANDBOX_SECRET,
        tastytrade_refresh_token=SANDBOX_REFRESH,
        tastytrade_market_data_client_id=MD_CLIENT_ID,
        tastytrade_market_data_client_secret=MD_SECRET,
        tastytrade_market_data_refresh_token=MD_REFRESH,
        tastytrade_market_data_scopes="read",
        database_url=f"postgresql://bot:{DB_PASSWORD}@localhost:5432/bot",
    )
    base.update(overrides)
    return Settings(**base)


def _engine() -> TnaTzaSignalEngine:
    return TnaTzaSignalEngine(SignalEngineConfig(), wall_clock=lambda: NOW)


@pytest.fixture
def db_session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


# ---------------------------------------------------------------------------
# Fake DXLink server
# ---------------------------------------------------------------------------


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds

    def sleep(self, seconds: float) -> None:
        self.t += seconds


class FakeConnection:
    def __init__(self, clock: FakeClock, incoming: list) -> None:
        self.clock = clock
        self.incoming = list(incoming)

    def send(self, message: str) -> None:
        json.loads(message)

    def recv(self, timeout: float) -> str:
        if not self.incoming:
            self.clock.advance(timeout)
            raise TimeoutError
        delay, payload = self.incoming[0]
        if delay > timeout:
            self.clock.advance(timeout)
            self.incoming[0] = (delay - timeout, payload)
            raise TimeoutError
        self.clock.advance(delay)
        self.incoming.pop(0)
        return json.dumps(payload)

    def close(self) -> None:
        pass


class FakeTokenProvider:
    IS_MARKET_DATA_ONLY = True

    def __init__(self, clock: FakeClock, errors=None) -> None:
        self.clock = clock
        self.errors = list(errors or [])
        self.calls = 0

    def get_token(self, *, force_refresh: bool = False) -> QuoteToken:
        self.calls += 1
        if self.errors:
            exc = self.errors.pop(0)
            if exc is not None:
                raise exc
        return QuoteToken(
            token=QUOTE_TOKEN,
            dxlink_url=DXLINK_URL,
            level="api",
            issued_at=NOW,
            expires_at=NOW + timedelta(hours=24),
            fetched_at=self.clock(),
        )

    def invalidate(self) -> None:
        pass


def _handshake() -> list:
    return [
        (0, {"type": "SETUP", "channel": 0, "version": "1.0", "keepaliveTimeout": 60}),
        (0, {"type": "AUTH_STATE", "channel": 0, "state": "UNAUTHORIZED"}),
        (0, {"type": "AUTH_STATE", "channel": 0, "state": "AUTHORIZED"}),
        (0, {"type": "CHANNEL_OPENED", "channel": 3, "service": "FEED", "parameters": {"contract": "AUTO"}}),
        (0, {"type": "FEED_CONFIG", "channel": 3, "dataFormat": "COMPACT", "eventFields": DEFAULT_EVENT_FIELDS}),
    ]


BULL = {
    "refs": {"TNA": (43.0, 42.5), "TZA": (10.5, 10.6), "IWM": (218.0, 217.0), "SPY": (575.0, 574.0), "QQQ": (495.0, 494.0)},
    "start": {"TNA": 44.0, "TZA": 10.2, "IWM": 219.0, "SPY": 579.0, "QQQ": 499.0},
    "end": {"TNA": 45.0, "TZA": 10.0, "IWM": 220.0, "SPY": 580.0, "QQQ": 500.0},
}
BEAR = {
    "refs": {"TNA": (43.0, 43.5), "TZA": (10.5, 10.4), "IWM": (218.0, 219.0), "SPY": (575.0, 576.0), "QQQ": (495.0, 496.0)},
    "start": {"TNA": 42.0, "TZA": 10.8, "IWM": 217.0, "SPY": 571.0, "QQQ": 491.0},
    "end": {"TNA": 41.0, "TZA": 11.0, "IWM": 216.0, "SPY": 570.0, "QQQ": 490.0},
}
CHOPPY = {
    "refs": {"TNA": (42.9, 43.4), "TZA": (10.48, 10.4), "IWM": (217.5, 218.6), "SPY": (575.0, 574.0), "QQQ": (495.0, 496.0)},
    "start": {"TNA": 43.0, "TZA": 10.5, "IWM": 218.0, "SPY": 580.0, "QQQ": 490.0},
    "end": {"TNA": 43.0, "TZA": 10.5, "IWM": 218.0, "SPY": 580.0, "QQQ": 490.0},
}


def _summary_frame(refs) -> dict:
    flat: list = []
    for symbol, (open_, prev) in refs.items():
        flat += ["Summary", symbol, open_, open_ * 1.01, open_ * 0.99, prev]
    flat += ["Summary", "VIX", 15.5, 15.5, 15.5, 15.5]
    return {"type": "FEED_DATA", "channel": 3, "data": ["Summary", flat]}


def _quote_frame(prices) -> dict:
    flat: list = []
    for symbol, mid in prices.items():
        flat += ["Quote", symbol, round(mid - 0.01, 4), round(mid + 0.01, 4), 100, 200]
    return {"type": "FEED_DATA", "channel": 3, "data": ["Quote", flat]}


def _stream(market=BULL, seconds=4.0, every=0.2, stop_at=None) -> list:
    messages: list = [
        (0.05, _summary_frame(market["refs"])),
        (0.05, {"type": "FEED_DATA", "channel": 3, "data": ["Trade", ["Trade", "VIX", 15.0, 0, 0]]}),
    ]
    elapsed = 0.1
    steps = int(seconds / every)
    for i in range(steps):
        frac = i / max(steps - 1, 1)
        if stop_at is not None and elapsed + every > stop_at:
            break
        prices = {s: market["start"][s] + (market["end"][s] - market["start"][s]) * frac for s in market["start"]}
        messages.append((every, _quote_frame(prices)))
        elapsed += every
    return messages


def _flat(prices, seconds=4.0, refs=BULL["refs"]) -> list:
    return _stream({"refs": refs, "start": prices, "end": prices}, seconds=seconds)


class StreamFactory:
    def __init__(self, clock: FakeClock, scripts: list) -> None:
        self.clock = clock
        self.scripts = list(scripts)
        self.calls = 0

    def __call__(self, provider, symbols):
        self.calls += 1
        script = self.scripts.pop(0)
        connection = FakeConnection(self.clock, _handshake() + script)
        return DXLinkStreamClient(
            provider,
            symbols=symbols,
            connect_factory=lambda _url, _timeout: connection,
            clock=self.clock,
            wall_clock=lambda: NOW,
            sleep=self.clock.sleep,
        )


def _runner(scripts, *, cycles=1, followup=0.0, pause=0.0, repository=None, errors=None, duration=4.0):
    clock = FakeClock()
    factory = StreamFactory(clock, scripts)
    sleeps: list = []
    signal_logger = ShadowSignalLogger(repository)
    runner = ShadowModeRunner(
        validate_market_data_settings(_settings()),
        _engine(),
        signal_logger,
        ShadowRunConfig(cycles=cycles, signal_duration_seconds=duration, pause_seconds=pause, followup_seconds=followup),
        run_id="shadow-test",
        provider=FakeTokenProvider(clock, errors),
        stream_factory=factory,
        sleep=sleeps.append,
    )
    return runner, factory, signal_logger, sleeps


# ---------------------------------------------------------------------------
# Logging decisions
# ---------------------------------------------------------------------------


def test_bullish_decision_logged(db_session):
    repo = ShadowSignalRepository(db_session)
    runner, _, signal_logger, _ = _runner([_stream(BULL)], repository=repo)
    summary = runner.run()
    assert summary.cycles_completed == 1 and summary.orders_submitted == 0
    row = db_session.query(ShadowSignalLog).one()
    assert row.run_id == "shadow-test" and row.cycle_number == 1
    assert row.decision == "bullish" and row.selected_symbol == "TNA"
    assert row.confidence_score >= 70 and row.bearish_score <= 40
    assert row.freshness_gate_passed is True
    assert row.submitted is False and row.production_execution_blocked is True
    for symbol in ("tna", "tza", "iwm", "spy", "qqq"):
        assert getattr(row, f"quote_age_{symbol}") is not None
        assert getattr(row, f"{symbol}_mid") is not None
    assert row.vix_last == 15.0
    assert json.loads(row.raw_snapshot_json)["quotes"]["IWM"]["first_mid"] == pytest.approx(219.0)
    assert json.loads(row.raw_score_json)["score_breakdown"]["bullish_score"] == row.bullish_score
    assert signal_logger.records[0].db_id == row.id


def test_bearish_decision_logged(db_session):
    runner, _, _, _ = _runner([_stream(BEAR)], repository=ShadowSignalRepository(db_session))
    runner.run()
    row = db_session.query(ShadowSignalLog).one()
    assert row.decision == "bearish" and row.selected_symbol == "TZA"
    assert row.submitted is False


def test_skip_decision_logged(db_session):
    runner, _, _, _ = _runner([_stream(CHOPPY)], repository=ShadowSignalRepository(db_session))
    runner.run()
    row = db_session.query(ShadowSignalLog).one()
    assert row.decision == "skip" and row.selected_symbol is None
    assert row.skip_reason == "unclear_market_direction"
    assert row.confidence_score == 0
    assert row.freshness_gate_passed is True


def test_stale_quote_logged_as_skip_and_run_continues(db_session):
    runner, factory, _, _ = _runner(
        [_stream(BULL, seconds=6.0, stop_at=2.0), _stream(BULL, seconds=6.0)],
        cycles=2,
        duration=6.0,
        repository=ShadowSignalRepository(db_session),
    )
    summary = runner.run()
    rows = db_session.query(ShadowSignalLog).order_by(ShadowSignalLog.cycle_number).all()
    assert [r.decision for r in rows] == ["skip", "bullish"]
    assert rows[0].skip_reason == "stale_market_data"
    assert rows[0].freshness_gate_passed is False
    assert rows[0].raw_score_json and json.loads(rows[0].raw_score_json)["score_breakdown"] is None
    assert summary.errors == [] and factory.calls == 2


# ---------------------------------------------------------------------------
# Follow-up outcomes
# ---------------------------------------------------------------------------


def test_followup_movement_recorded(db_session):
    after = {"TNA": 45.9, "TZA": 9.8, "IWM": 220.5, "SPY": 581.0, "QQQ": 501.0}
    runner, factory, signal_logger, _ = _runner(
        [_stream(BULL), _flat(after, seconds=3.0)],
        followup=3.0,
        repository=ShadowSignalRepository(db_session),
    )
    runner.run()
    assert factory.calls == 2
    row = db_session.query(ShadowSignalLog).one()
    assert row.followup_seconds == 3.0
    assert row.tna_mid_after == pytest.approx(45.9)
    assert row.iwm_mid_after == pytest.approx(220.5)
    assert row.qqq_mid_after == pytest.approx(501.0)
    expected = (45.9 - row.tna_mid) / row.tna_mid * 100
    assert row.selected_symbol_move_pct == pytest.approx(expected, rel=1e-6)
    assert row.iwm_move_pct > 0
    assert row.direction_was_correct is True
    assert "correct" in row.outcome_note
    assert signal_logger.records[0].followup is not None


def test_selected_symbol_move_pct_calculated_correctly():
    after = MarketSnapshot(
        quotes={
            "TNA": SymbolQuote("TNA", bid=45.89, ask=45.91, quote_age_seconds=0.2, quote_updates=3),
            "IWM": SymbolQuote("IWM", bid=219.99, ask=220.01, quote_age_seconds=0.2, quote_updates=3),
        }
    )
    outcome = compute_followup_outcome(
        decision="bullish",
        selected_symbol="TNA",
        mids_before={"TNA": 45.0, "IWM": 222.0},
        after=after,
        followup_seconds=60.0,
    )
    assert outcome.selected_symbol_move_pct == pytest.approx(2.0)
    assert outcome.iwm_move_pct == pytest.approx((220.0 - 222.0) / 222.0 * 100)
    assert outcome.direction_was_correct is True  # TNA rose even though IWM fell


def _after(**mids) -> MarketSnapshot:
    return MarketSnapshot(
        quotes={s: SymbolQuote(s, bid=m - 0.01, ask=m + 0.01, quote_age_seconds=0.3, quote_updates=2) for s, m in mids.items()}
    )


def test_skip_followup_does_not_mark_correct_or_incorrect():
    outcome = compute_followup_outcome(
        decision="skip",
        selected_symbol=None,
        mids_before={"TNA": 45.0, "TZA": 10.0, "IWM": 220.0},
        after=_after(TNA=46.0, TZA=9.8, IWM=221.0),
        followup_seconds=60.0,
    )
    assert outcome.direction_was_correct is None
    assert outcome.selected_symbol_move_pct is None
    assert outcome.iwm_move_pct == pytest.approx(100 / 220, abs=1e-6)
    assert outcome.mids_after["TNA"] == pytest.approx(46.0)
    assert outcome.outcome_note.startswith("skip: movement recorded only")


@pytest.mark.parametrize(
    "decision, symbol, after, expected",
    [
        ("bullish", "TNA", {"TNA": 44.0, "IWM": 221.0}, True),  # IWM rose
        ("bullish", "TNA", {"TNA": 44.0, "IWM": 219.0}, False),
        ("bearish", "TZA", {"TZA": 10.5, "IWM": 221.0}, True),  # TZA rose
        ("bearish", "TZA", {"TZA": 9.5, "IWM": 219.0}, True),  # IWM fell
        ("bearish", "TZA", {"TZA": 9.5, "IWM": 221.0}, False),
        ("bearish", "TZA", {"TZA": 10.0, "IWM": 220.0}, False),  # flat is not a move
    ],
)
def test_direction_rules(decision, symbol, after, expected):
    outcome = compute_followup_outcome(
        decision=decision,
        selected_symbol=symbol,
        mids_before={"TNA": 45.0, "TZA": 10.0, "IWM": 220.0},
        after=_after(**after),
        followup_seconds=30.0,
    )
    assert outcome.direction_was_correct is expected


def test_stale_or_missing_followup_is_not_scored():
    stale = MarketSnapshot(
        quotes={
            "TNA": SymbolQuote("TNA", bid=43.99, ask=44.01, quote_age_seconds=30.0, quote_updates=2),
            "IWM": SymbolQuote("IWM", bid=218.99, ask=219.01, quote_age_seconds=30.0, quote_updates=2),
        }
    )
    outcome = compute_followup_outcome(
        decision="bullish",
        selected_symbol="TNA",
        mids_before={"TNA": 45.0, "IWM": 220.0},
        after=stale,
        followup_seconds=60.0,
    )
    assert outcome.direction_was_correct is None
    assert outcome.mids_after["TNA"] is None
    assert "stale" in outcome.outcome_note
    none_outcome = compute_followup_outcome(
        decision="bearish", selected_symbol="TZA", mids_before={}, after=None, followup_seconds=60.0
    )
    assert none_outcome.direction_was_correct is None


def test_followup_transient_failure_recorded_and_run_continues():
    err = MarketDataError("drop", step="connect", reason="connection_failed")
    # Token calls: cycle 1 signal (collect + connect), then cycle 1 follow-up fails.
    runner, factory, signal_logger, _ = _runner(
        [_stream(BULL), _stream(BULL), _flat(BULL["end"], seconds=3.0)], cycles=2, followup=3.0, errors=[None, None, err]
    )
    summary = runner.run()
    assert summary.aborted is False and summary.cycles_completed == 2
    assert [e.reason for e in summary.errors] == ["connection_failed"]
    first, second = signal_logger.records
    assert "follow-up collection failed (connection_failed)" in first.followup.outcome_note
    assert first.followup.direction_was_correct is None
    assert second.followup is not None and "follow-up collection failed" not in second.followup.outcome_note


# ---------------------------------------------------------------------------
# Bounded loop, errors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cycles", [0, -1, MAX_CYCLES + 1])
def test_cycles_bounds_prevent_infinite_loop(cycles):
    with pytest.raises(ConfigurationError):
        ShadowRunConfig(cycles=cycles).validate()


def test_runner_executes_exactly_n_cycles_and_pauses_between():
    runner, factory, signal_logger, sleeps = _runner([_stream(BULL)] * 3, cycles=3, pause=7.0)
    summary = runner.run()
    assert summary.cycles_completed == 3
    assert factory.calls == 3
    assert sleeps == [7.0, 7.0]
    assert [r.cycle_number for r in signal_logger.records] == [1, 2, 3]


def test_runner_loop_is_bounded_by_range():
    source = inspect.getsource(runner_mod.ShadowModeRunner.run)
    assert "for cycle in range(1, self._run.cycles + 1)" in source
    assert "while" not in source


@pytest.mark.parametrize("reason", ["unauthorized", "auth_failed", "rate_limited", "dxlink_url_blocked", "read_only_required"])
def test_fatal_market_data_error_stops_run(reason):
    err = MarketDataError("x", step="quote_token", reason=reason)
    runner, factory, signal_logger, _ = _runner([_stream(BULL)] * 3, cycles=3, errors=[err])
    summary = runner.run()
    assert summary.aborted is True and summary.abort_reason == reason
    assert factory.calls == 0 and signal_logger.records == []


def test_transient_error_continues_to_next_cycle():
    err = MarketDataError("x", step="connect", reason="network_error")
    runner, factory, signal_logger, _ = _runner([_stream(BULL)], cycles=2, errors=[err])
    summary = runner.run()
    assert summary.aborted is False
    assert len(summary.errors) == 1 and summary.errors[0].fatal is False
    assert summary.cycles_completed == 1 and signal_logger.records[0].cycle_number == 2


# ---------------------------------------------------------------------------
# Database / repository / migration
# ---------------------------------------------------------------------------


def test_db_rejects_submitted_true(db_session):
    db_session.add(ShadowSignalLog(run_id="r", cycle_number=1, decision="skip", submitted=True))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()
    db_session.add(ShadowSignalLog(run_id="r", cycle_number=1, decision="skip", production_execution_blocked=False))
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_record_cannot_be_submitted():
    record = _record("bullish", "TNA")
    with pytest.raises(ShadowSafetyError):
        ShadowCycleRecord(**{**record.__dict__, "submitted": True})
    with pytest.raises(ShadowSafetyError):
        ShadowCycleRecord(**{**record.__dict__, "production_execution_blocked": False})
    record.submitted = True
    with pytest.raises(ShadowSafetyError):
        ShadowSignalLogger().log(record)


def test_migration_003_creates_matching_table():
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    path = REPO_ROOT / "alembic" / "versions" / "003_shadow_signal_log.py"
    spec = importlib.util.spec_from_file_location("migration_003", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.revision == "003_shadow_signal_log"
    assert mod.down_revision == "002_orders_limit_price"

    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            mod.upgrade()
            mod.upgrade()  # idempotent
        columns = {c["name"] for c in sa_inspect(conn).get_columns("shadow_signal_log")}
    # Session metadata columns are added by migration 004 (Checkpoint 2.14).
    assert columns == set(ShadowSignalLog.__table__.columns.keys()) - set(models_mod.SESSION_FIELDS)


def test_open_shadow_repository_creates_or_requires_table(tmp_path):
    url = f"sqlite:///{tmp_path / 'shadow.db'}"
    with pytest.raises(ShadowTableMissingError):
        open_shadow_repository(url, create_table=False)
    repo = open_shadow_repository(url, create_table=True)
    assert repo.list_signals() == []
    assert isinstance(open_shadow_repository(url, create_table=False), ShadowSignalRepository)


def test_safe_database_label_hides_password():
    label = safe_database_label(f"postgresql://bot:{DB_PASSWORD}@db:5432/bot")
    assert DB_PASSWORD not in label and "***" in label


def test_db_write_failure_is_warning_not_crash():
    repo = MagicMock()
    repo.log_signal.side_effect = RuntimeError("db down")
    runner, _, signal_logger, _ = _runner([_stream(BULL)], repository=repo)
    summary = runner.run()
    assert summary.cycles_completed == 1
    assert signal_logger.warnings and "DB log failed" in signal_logger.warnings[0]


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _record(decision, symbol, *, skip_reason=None, confidence=0.0, correct=None, cycle=1, ages=0.2, run_id="r1"):
    record = ShadowCycleRecord(
        run_id=run_id,
        cycle_number=cycle,
        created_at=NOW + timedelta(minutes=cycle),
        decision=decision,
        selected_symbol=symbol,
        confidence_score=confidence,
        bullish_score=confidence if decision == "bullish" else 0.0,
        bearish_score=confidence if decision == "bearish" else 0.0,
        skip_reason=skip_reason,
        market_regime="mixed",
        explanation="test",
        quote_ages={s: ages for s in models_mod.TRACKED_SYMBOLS},
        mids={s: 100.0 for s in models_mod.TRACKED_SYMBOLS},
        vix_last=15.0,
        freshness_gate_passed=skip_reason not in ("stale_market_data", "missing_required_quote"),
        raw_snapshot_json="{}",
        raw_score_json="{}",
    )
    if correct is not None or decision == "skip":
        record.apply_followup(
            models_mod.FollowupOutcome(
                followup_seconds=60.0,
                mids_after={s: 101.0 for s in models_mod.TRACKED_SYMBOLS},
                selected_symbol_move_pct=1.0 if symbol else None,
                iwm_move_pct=1.0,
                direction_was_correct=correct,
                outcome_note="t",
            )
        )
    return record


def _report_records():
    return [
        _record("bullish", "TNA", confidence=100, correct=True, cycle=1),
        _record("bullish", "TNA", confidence=80, correct=False, cycle=2),
        _record("bearish", "TZA", confidence=90, correct=True, cycle=3),
        _record("skip", None, skip_reason="stale_market_data", cycle=4, ages=2.0),
        _record("skip", None, skip_reason="missing_required_quote", cycle=5),
        _record("skip", None, skip_reason="unclear_market_direction", cycle=6),
    ]


def test_report_summary_counts_decisions_correctly():
    report = summarize_shadow_logs(_report_records())
    assert report["total_signals"] == 6
    assert report["bullish_count"] == 2
    assert report["bearish_count"] == 1
    assert report["skip_count"] == 3
    assert report["stale_or_missing_data_skip_count"] == 2
    assert report["unclear_market_skip_count"] == 1
    assert report["average_confidence_trade_signals"] == 90.0
    assert report["average_confidence_all"] == 45.0
    assert report["average_quote_age_seconds"]["TNA"] == pytest.approx((0.2 * 5 + 2.0) / 6, abs=1e-3)
    outcomes = report["outcomes"]
    assert outcomes["scored"] == 3 and outcomes["correct"] == 2 and outcomes["incorrect"] == 1
    assert outcomes["correct_pct"] == 66.7
    assert outcomes["bullish"]["scored"] == 2 and outcomes["bearish"]["correct"] == 1
    assert report["submitted_count"] == 0
    assert report["production_execution_blocked_all"] is True
    assert report["latest_decisions"][0]["cycle_number"] == 6


def test_report_from_database_rows(db_session, capsys):
    repo = ShadowSignalRepository(db_session)
    for record in _report_records():
        repo.log_signal(record)
        if record.followup:
            repo.update_followup(record.db_id, record.followup)
    repo.log_signal(_record("bullish", "TNA", confidence=75, run_id="other"))

    code = report_script.run_report(
        _settings(), run_id="r1", limit=100, rows_loader=lambda s, run_id, limit: repo.list_signals(run_id=run_id, limit=limit)
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "total_signals: 6" in out
    assert "bullish_count: 2" in out and "bearish_count: 1" in out and "skip_count: 3" in out
    assert "stale_or_missing_data_skip_count: 2" in out
    assert "unclear_market_skip_count: 1" in out
    assert "scored: 3  correct: 2  incorrect: 1" in out
    assert "--- latest decisions" in out

    code = report_script.run_report(
        _settings(), limit=100, json_output=True,
        rows_loader=lambda s, run_id, limit: repo.list_signals(run_id=run_id, limit=limit),
    )
    doc = json.loads(capsys.readouterr().out)
    assert doc["report"]["total_signals"] == 7
    assert doc["report"]["submitted_count"] == 0


def test_report_empty_and_db_errors(capsys):
    assert report_script.run_report(_settings(), rows_loader=lambda *a: []) == 0
    assert "total_signals: 0" in capsys.readouterr().out

    def broken(*_a):
        raise ShadowTableMissingError("shadow_signal_log table not found; run: alembic upgrade head")

    assert report_script.run_report(_settings(), rows_loader=broken) == 2
    assert "alembic upgrade head" in capsys.readouterr().out

    def down(*_a):
        raise RuntimeError(f"could not connect with {DB_PASSWORD}")

    assert report_script.run_report(_settings(), rows_loader=down, json_output=True) == 2
    out = capsys.readouterr().out
    assert DB_PASSWORD not in out and json.loads(out)["exit_code"] == 2


# ---------------------------------------------------------------------------
# CLI runner script
# ---------------------------------------------------------------------------


def _run_cli(capsys, scripts, *, errors=None, settings=None, **kwargs):
    clock = FakeClock()
    factory = StreamFactory(clock, scripts)
    sleeps: list = []
    kwargs.setdefault("signal_duration_seconds", 4.0)
    kwargs.setdefault("followup_seconds", 0.0)
    kwargs.setdefault("pause_seconds", 0.0)
    code = shadow_script.run_shadow(
        settings or _settings(),
        token_provider_factory=lambda _cfg: FakeTokenProvider(clock, errors),
        stream_factory=factory,
        sleep=sleeps.append,
        run_id=kwargs.pop("run_id", "shadow-cli-test"),
        **kwargs,
    )
    return code, capsys.readouterr().out, factory, sleeps


def test_cli_without_db_runs_in_memory(capsys):
    def no_db(_settings):
        raise AssertionError("DB must not be opened without --with-db")

    code, out, factory, _ = _run_cli(capsys, [_stream(BULL), _stream(CHOPPY)], cycles=2, repository_factory=no_db)
    assert code == 0
    assert factory.calls == 2
    assert "database_logging: disabled (memory only)" in out
    assert "logged: memory only" in out
    assert "decision: TNA (bullish)" in out
    assert "decision: SKIP (skip)" in out
    assert "orders_submitted: 0" in out
    assert shadow_script.COMPLETED in out


def test_cli_with_db_logs_and_updates_followup(capsys, db_session):
    after = {"TNA": 44.5, "TZA": 10.1, "IWM": 219.5, "SPY": 579.0, "QQQ": 499.0}
    code, out, _, _ = _run_cli(
        capsys,
        [_stream(BULL), _flat(after, seconds=3.0)],
        cycles=1,
        followup_seconds=3.0,
        with_db=True,
        repository_factory=lambda _s: ShadowSignalRepository(db_session),
    )
    assert code == 0
    row = db_session.query(ShadowSignalLog).one()
    assert row.run_id == "shadow-cli-test"
    assert row.direction_was_correct is False
    assert row.selected_symbol_move_pct < 0
    assert "logged: db id=" in out
    assert "direction_was_correct: false" in out


def test_cli_json_output_single_redacted_document(capsys):
    code, out, _, _ = _run_cli(capsys, [_stream(BULL), _stream(BEAR)], cycles=2, json_output=True)
    assert code == 0
    doc = json.loads(out)
    assert doc["shadow_signal_logger"] == "completed"
    assert [c["decision"] for c in doc["cycles"]] == ["bullish", "bearish"]
    assert all(c["submitted"] is False and c["production_execution_blocked"] is True for c in doc["cycles"])
    assert doc["run"]["orders_submitted"] == 0
    assert doc["execution"]["production_order_execution"] == "blocked"
    assert doc["summary"]["bullish_count"] == 1 and doc["summary"]["bearish_count"] == 1
    for secret in SECRETS:
        assert secret not in out


def test_cli_text_output_has_no_secrets(capsys, db_session):
    _, out, _, _ = _run_cli(
        capsys, [_stream(BULL)], cycles=1, with_db=True, repository_factory=lambda _s: ShadowSignalRepository(db_session)
    )
    for secret in SECRETS:
        assert secret not in out


def test_cli_stale_cycle_skips_and_continues(capsys):
    code, out, factory, _ = _run_cli(
        capsys,
        [_stream(BULL, seconds=6.0, stop_at=2.0), _stream(BULL, seconds=6.0)],
        cycles=2,
        signal_duration_seconds=6.0,
    )
    assert code == 0
    assert "skip_reason: stale_market_data" in out
    assert "decision: TNA (bullish)" in out
    assert factory.calls == 2


@pytest.mark.parametrize("reason, expected", [("unauthorized", 1), ("auth_failed", 1), ("rate_limited", 3)])
def test_cli_auth_failure_exits_clearly(capsys, reason, expected):
    err = MarketDataError("x", step="oauth", reason=reason)
    code, out, factory, _ = _run_cli(capsys, [_stream(BULL)] * 3, cycles=3, errors=[err])
    assert code == expected
    assert f"run stopped: {reason}" in out
    assert shadow_script.FAILED in out
    assert factory.calls == 0


def test_cli_all_transient_failures_exit_1(capsys):
    err = MarketDataError("x", step="connect", reason="timeout")
    code, out, _, _ = _run_cli(capsys, [], cycles=2, errors=[err, err])
    assert code == 1
    assert "no cycle collected market data" in out


@pytest.mark.parametrize(
    "settings",
    [
        _settings(tastytrade_market_data_refresh_token=""),
        _settings(tastytrade_market_data_client_secret=SANDBOX_SECRET),
        _settings(tastytrade_env="production"),
        _settings(trading_mode="live"),
    ],
)
def test_cli_config_failure_exits_2(capsys, settings):
    code, out, factory, _ = _run_cli(capsys, [], settings=settings)
    assert code == 2
    assert factory.calls == 0
    for secret in SECRETS:
        assert secret not in out


def test_cli_db_open_failure_exits_2(capsys):
    def broken(_settings):
        raise RuntimeError(f"connect failed {DB_PASSWORD}")

    code, out, factory, _ = _run_cli(capsys, [_stream(BULL)], with_db=True, repository_factory=broken)
    assert code == 2 and factory.calls == 0
    assert DB_PASSWORD not in out


@pytest.mark.parametrize("argv", [["--cycles", "0"], ["--cycles", "1000"], ["--signal-duration-seconds", "0"], ["--followup-seconds", "-1"]])
def test_cli_main_rejects_unbounded_args(argv, monkeypatch, capsys):
    monkeypatch.setattr(shadow_script, "load_settings", lambda **_kw: _settings())
    assert shadow_script.main(argv) == 2


def test_cli_requires_core_symbols(capsys):
    code, out, _, _ = _run_cli(capsys, [], symbols=["TNA", "TZA", "VIX"])
    assert code == 2 and "IWM" in out


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
    "module", [models_mod, logger_mod, runner_mod, report_mod, repo_mod, shadow_script, report_script]
)
def test_shadow_mode_has_no_order_executor_dependency(module):
    source = inspect.getsource(module)
    for token in _FORBIDDEN:
        assert token not in source, f"{module.__name__} references {token}"


def test_shadow_runner_cannot_submit_orders():
    runner, _, _, _ = _runner([_stream(BULL)])
    assert runner.IS_SHADOW_ONLY is True
    for name in ("execute", "submit", "place_order", "route", "_executor", "_adapter", "_router"):
        assert not hasattr(runner, name)
    summary = runner.run()
    assert summary.orders_submitted == 0
    assert all(r.submitted is False for r in summary.records)


def test_production_execution_block_checks_pass():
    checks = runner_mod.production_execution_block_checks()
    assert checks and all(checks.values())


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


def test_market_hours_helper():
    assert runner_mod.is_regular_market_hours(datetime(2026, 10, 2, 15, 0, tzinfo=timezone.utc)) is True  # 11:00 ET Fri
    assert runner_mod.is_regular_market_hours(datetime(2026, 10, 3, 15, 0, tzinfo=timezone.utc)) is False  # Saturday
    assert runner_mod.is_regular_market_hours(datetime(2026, 10, 2, 21, 0, tzinfo=timezone.utc)) is False  # 17:00 ET
