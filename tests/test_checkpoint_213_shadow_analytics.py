"""Checkpoint 2.13 — Shadow analytics and offline threshold calibration (analytics only)."""

from __future__ import annotations

import inspect
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import backend.database  # noqa: F401
import backend.db.models  # noqa: F401
import backend.shadow_mode.analytics as analytics
from backend.config.settings import ConfigurationError, Settings, load_settings
from backend.db.base import Base
from backend.db.models import ShadowSignalLog
from backend.execution.execution_router import ExecutionRouter
from backend.repositories.shadow_signal_repository import ShadowSignalRepository
from backend.risk.live_guard import LiveTradingBlockedError, assert_order_execution_allowed
from backend.risk.models import OrderIntent, RiskContext
from backend.shadow_mode.analytics import (
    AnalyticsConfigError,
    ThresholdSet,
    analyze_market_window,
    analyze_rows,
    build_threshold_grid,
    compare_runs,
    load_rows,
    replay_decision,
    score_hypothetical,
    simulate_thresholds,
)
from backend.shadow_mode.models import ShadowCycleRecord, compute_followup_outcome
from backend.signals.models import MarketSnapshot, SymbolQuote
from backend.signals.tna_tza_signal_engine import TnaTzaSignalEngine
from scripts import analyze_shadow_signal_logs as script

_NO_ENV = Path("/nonexistent/.env")
NOW = datetime(2026, 10, 2, 15, 0, 0, tzinfo=timezone.utc)
MD_SECRET = "mdsecret-ZZZZ-9876543210-qwerty"
MD_REFRESH = "mdrefresh-AAAA-1111222233334444-token"
SANDBOX_SECRET = "sandbox-secret-00000000000"
SANDBOX_REFRESH = "sandbox-refresh-000000000000"
DB_PASSWORD = "dbpass-SECRET-7777"
SECRETS = (MD_SECRET, MD_REFRESH, SANDBOX_SECRET, SANDBOX_REFRESH, DB_PASSWORD)


def _settings(**overrides) -> Settings:
    base = dict(
        trading_mode="sandbox",
        tastytrade_env="sandbox",
        live_trading_enabled=False,
        tastytrade_client_secret=SANDBOX_SECRET,
        tastytrade_refresh_token=SANDBOX_REFRESH,
        tastytrade_market_data_client_secret=MD_SECRET,
        tastytrade_market_data_refresh_token=MD_REFRESH,
        database_url=f"postgresql://bot:{DB_PASSWORD}@localhost:5432/bot",
    )
    base.update(overrides)
    return Settings(**base)


# ---------------------------------------------------------------------------
# Row fixtures built from real Signal Engine decisions
# ---------------------------------------------------------------------------


def q(symbol, mid, *, open_=None, prev=None, first=None, age=0.2) -> SymbolQuote:
    return SymbolQuote(
        symbol=symbol,
        bid=round(mid - 0.01, 4),
        ask=round(mid + 0.01, 4),
        day_open=open_,
        prev_close=prev,
        first_mid=first,
        quote_age_seconds=age,
        quote_updates=10,
    )


def strong_bull(**ages):
    return [
        q("TNA", 45.0, open_=43.0, prev=42.5, age=ages.get("TNA", 0.2)),
        q("TZA", 10.0, open_=10.5, prev=10.6),
        q("IWM", 220.0, open_=218.0, prev=217.0, first=219.0),
        q("SPY", 580.0, open_=575.0, prev=574.0),
        q("QQQ", 500.0, open_=495.0, prev=494.0),
    ]


def near_miss_bull():
    """IWM fully bullish (60 points), SPY/QQQ/TNA/TZA neutral -> 60: SKIP at entry 70, TNA at entry 60."""
    return [
        q("TNA", 43.0, open_=43.0, prev=42.5),
        q("TZA", 10.5, open_=10.5, prev=10.6),
        q("IWM", 220.0, open_=218.0, prev=217.0, first=219.0),
        q("SPY", 575.0, open_=575.0, prev=574.0),
        q("QQQ", 495.0, open_=495.0, prev=494.0),
    ]


def near_miss_bear():
    return [
        q("TNA", 43.0, open_=43.0, prev=43.5),
        q("TZA", 10.5, open_=10.5, prev=10.4),
        q("IWM", 216.0, open_=218.0, prev=219.0, first=217.0),
        q("SPY", 575.0, open_=575.0, prev=576.0),
        q("QQQ", 495.0, open_=495.0, prev=496.0),
    ]


def choppy():
    return [
        q("TNA", 43.0, open_=42.9, prev=43.4),
        q("TZA", 10.5, open_=10.48, prev=10.4),
        q("IWM", 218.0, open_=217.5, prev=218.6, first=218.0),
        q("SPY", 580.0, open_=575.0, prev=574.0),
        q("QQQ", 490.0, open_=495.0, prev=496.0),
    ]


def _after(mids) -> MarketSnapshot:
    return MarketSnapshot(
        quotes={s: SymbolQuote(s, bid=m - 0.01, ask=m + 0.01, quote_age_seconds=0.3, quote_updates=3) for s, m in mids.items()}
    )


def record(run_id, cycle, quotes, *, after_moves=None, followup=60.0) -> ShadowCycleRecord:
    """after_moves: {symbol: pct move} applied to the decision mids for the follow-up snapshot."""
    snapshot = MarketSnapshot(quotes={x.symbol: x for x in quotes}, created_at=NOW + timedelta(minutes=cycle))
    decision = TnaTzaSignalEngine(wall_clock=lambda: snapshot.created_at).decide(snapshot)
    rec = ShadowCycleRecord.from_decision(run_id=run_id, cycle_number=cycle, decision=decision, snapshot=snapshot)
    if after_moves is not None:
        mids_after = {s: rec.mids[s] * (1 + after_moves.get(s, 0.0) / 100) for s in rec.mids if rec.mids[s]}
        rec.apply_followup(
            compute_followup_outcome(
                decision=rec.decision,
                selected_symbol=rec.selected_symbol,
                mids_before=rec.mids,
                after=_after(mids_after),
                followup_seconds=followup,
            )
        )
    return rec


UP = {"IWM": 0.25, "TNA": 0.75, "TZA": -0.75, "SPY": 0.1, "QQQ": 0.1}
DOWN = {"IWM": -0.25, "TNA": -0.75, "TZA": 0.75, "SPY": -0.1, "QQQ": -0.1}
FLAT = {"IWM": 0.005, "TNA": 0.01, "TZA": -0.01}


def mixed_rows(run_id="run-a"):
    return [
        record(run_id, 1, near_miss_bull(), after_moves=UP),  # hypothetical TNA at 60 -> correct
        record(run_id, 2, near_miss_bear(), after_moves=UP),  # hypothetical TZA at 60 -> false signal
        record(run_id, 3, strong_bull(TNA=1.5), after_moves=UP),  # stale -> always SKIP
        record(run_id, 4, choppy(), after_moves=FLAT),  # unclear
    ]


@pytest.fixture
def db_session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _store(session, records):
    repo = ShadowSignalRepository(session)
    for rec in records:
        repo.log_signal(rec)
        if rec.followup:
            repo.update_followup(rec.db_id, rec.followup)
    return repo


def _dump(session):
    columns = [c.name for c in ShadowSignalLog.__table__.columns]
    return [
        {c: getattr(row, c) for c in columns}
        for row in session.query(ShadowSignalLog).order_by(ShadowSignalLog.id).all()
    ]


# ---------------------------------------------------------------------------
# Actual-run analysis
# ---------------------------------------------------------------------------


def test_analytics_counts_skip_reasons_correctly():
    rows = load_rows(mixed_rows())
    rows += [
        {"run_id": "x", "decision": "skip", "skip_reason": "stale_market_data", "freshness_gate_passed": False},
        {"run_id": "x", "decision": "skip", "skip_reason": "insufficient_signal_strength", "freshness_gate_passed": True},
        {"run_id": "x", "decision": "bullish", "selected_symbol": "TNA", "freshness_gate_passed": True},
    ]
    summary = analyze_rows(rows)
    assert summary["total_cycles"] == 7
    assert summary["decisions"] == {"bullish": 1, "bearish": 0, "skip": 6}
    assert summary["skip_reasons"] == {
        "insufficient_signal_strength": 3,
        "stale_market_data": 2,
        "unclear_market_direction": 1,
    }
    assert summary["freshness_pass_pct"] == pytest.approx(5 / 7 * 100, abs=0.1)


def test_analytics_reports_quote_ages_and_moves_after_skips():
    summary = analyze_rows(load_rows(mixed_rows()))
    assert summary["average_quote_age_seconds"]["TNA"] == pytest.approx((0.2 * 3 + 1.5) / 4, abs=1e-3)
    after = summary["after_skips"]
    assert after["skips_with_followup"] == 4
    assert after["avg_iwm_move_pct"] == pytest.approx((0.25 * 3 + 0.005) / 4, abs=1e-3)
    assert after["avg_tna_move_pct"] == pytest.approx((0.75 * 3 + 0.01) / 4, abs=1e-3)
    assert after["avg_tza_move_pct"] == pytest.approx((-0.75 * 3 - 0.01) / 4, abs=1e-3)
    assert after["enough_movement_after_skip"] == 3
    assert summary["score_profile"]["near_miss_cycles"] == 2
    assert summary["submitted_count"] == 0


def test_flat_market_detection_works():
    rows = load_rows([record("flat", i, choppy(), after_moves=FLAT) for i in range(1, 6)])
    window = analyze_market_window(rows, min_move_pct=0.03)
    assert window["market_window_status"] == "flat/choppy"
    assert window["recommendation"] == "collect more data during stronger movement before tuning"
    assert window["flat_cycles"] == 5 and window["enough_movement_cycles"] == 0
    assert "stronger movement" in analyze_rows(rows)["diagnosis"]


def test_moving_market_and_insufficient_data():
    moving = load_rows([record("m", i, near_miss_bull(), after_moves=UP) for i in range(1, 4)])
    assert analyze_market_window(moving, min_move_pct=0.03)["market_window_status"] == "moving"
    no_followup = load_rows([record("n", 1, choppy())])
    assert analyze_market_window(no_followup, min_move_pct=0.03)["market_window_status"] == "insufficient_data"


def test_reversing_moves_are_choppy():
    rows = load_rows(
        [record("c", i, choppy(), after_moves=UP if i % 2 else DOWN) for i in range(1, 7)]
    )
    assert analyze_market_window(rows, min_move_pct=0.03)["market_window_status"] == "flat/choppy"


def test_min_followup_move_threshold_controls_flatness():
    rows = load_rows([record("m", i, near_miss_bull(), after_moves=UP) for i in range(1, 4)])
    assert analyze_market_window(rows, min_move_pct=0.5)["market_window_status"] == "flat/choppy"


def test_compare_runs():
    rows = load_rows(
        mixed_rows("run-a")
        + [record("run-b", i, choppy(), after_moves=FLAT) for i in range(5, 8)]
    )
    runs = {r["run_id"]: r for r in compare_runs(rows)}
    assert set(runs) == {"run-a", "run-b"}
    assert runs["run-a"]["cycles"] == 4 and runs["run-b"]["cycles"] == 3
    assert runs["run-b"]["market_window_status"] == "flat/choppy"
    assert runs["run-b"]["top_skip_reason"] == "unclear_market_direction"


# ---------------------------------------------------------------------------
# Threshold simulation
# ---------------------------------------------------------------------------


def _grid(entries=(60, 65, 70), opposings=(30, 40, 50), gaps=(10, 15, 20)):
    return build_threshold_grid(list(entries), list(opposings), list(gaps))


def test_grid_rejects_unsafe_combinations():
    safe, rejected = _grid()
    assert len(safe) == 21 and len(rejected) == 6
    assert all(r["opposing_score_max"] > r["entry_score_threshold"] - 20 for r in rejected)
    assert all(t.safety_problem() is None for t in safe)


@pytest.mark.parametrize(
    "entries, opposings, gaps",
    [([55], [30], [20]), ([70], [-1], [20]), ([70], [40], [5]), ([101], [40], [20])],
)
def test_candidate_thresholds_cannot_go_below_safe_minimums(entries, opposings, gaps):
    with pytest.raises(AnalyticsConfigError):
        build_threshold_grid(entries, opposings, gaps)


def test_simulate_refuses_unsafe_threshold_set():
    with pytest.raises(AnalyticsConfigError):
        simulate_thresholds([], [ThresholdSet(50.0, 20.0, 20.0)])
    with pytest.raises(AnalyticsConfigError):
        simulate_thresholds([], [ThresholdSet(70.0, 55.0, 20.0)])


def test_replay_reproduces_logged_decisions():
    rows = load_rows(mixed_rows())
    safe, rejected = _grid()
    sim = simulate_thresholds(rows, safe, rejected=rejected)
    assert sim["replay_mismatches_with_logged_thresholds"] == 0
    assert sim["baseline"]["is_current_default"] is True
    assert sim["baseline"]["skip_count"] == 4


def test_simulation_finds_near_miss_signals_and_false_signals():
    rows = load_rows(mixed_rows())
    safe, _ = _grid()
    sim = simulate_thresholds(rows, safe)
    by_label = {r["label"]: r for r in sim["results"]}
    loose = by_label["entry=60 opposing=40 gap=20"]
    assert loose["bullish_count"] == 1 and loose["bearish_count"] == 1 and loose["skip_count"] == 2
    assert loose["scored_count"] == 2
    assert loose["direction_correct_count"] == 1 and loose["direction_incorrect_count"] == 1
    example = loose["false_signal_examples"][0]
    assert example["hypothetical_symbol"] == "TZA" and example["cycle_number"] == 2
    assert loose["skip_reasons"]["stale_market_data"] == 1
    strict = by_label["entry=70 opposing=40 gap=20"]
    assert strict["bullish_count"] == 0 and strict["bearish_count"] == 0
    for result in sim["results"]:
        assert result["skip_reasons"].get("stale_market_data") == 1  # stale never becomes a trade
        assert result["replay_methods"] == {"snapshot_replay": 4}


def test_best_candidate_is_diagnostic_only_and_safe():
    rows = load_rows(
        [record("b", i, near_miss_bull(), after_moves=UP) for i in range(1, 6)]
        + [record("b", 6, near_miss_bear(), after_moves=UP)]
    )
    safe, rejected = _grid()
    sim = simulate_thresholds(rows, safe, rejected=rejected, market_window_status="moving")
    best = sim["best_candidate"]
    assert best["label"] == "entry=60 opposing=30 gap=20"
    assert best["correct_pct"] == pytest.approx(83.3)
    assert best["diagnostic_only"] is True and best["applied"] is False
    assert "not applied" in best["note"]
    assert ThresholdSet(**best["thresholds"]).safety_problem() is None
    assert sim["writes_to_database"] is False and sim["orders_submitted"] == 0


def test_best_candidate_requires_enough_samples_and_warns_on_flat_market():
    rows = load_rows(mixed_rows())
    safe, _ = _grid()
    sim = simulate_thresholds(rows, safe, market_window_status="flat/choppy")
    assert sim["best_candidate"]["label"] is None
    assert "insufficient" in sim["best_candidate"]["reason"]
    assert "do not tune" in sim["best_candidate"]["warning"]


def test_flat_followup_is_not_scored():
    row = load_rows([record("f", 1, near_miss_bull(), after_moves=FLAT)])[0]
    assert score_hypothetical("bullish", row, min_move_pct=0.03) is None
    assert score_hypothetical("bullish", row, min_move_pct=0.001) is True
    assert score_hypothetical("skip", row, min_move_pct=0.03) is None


def test_stored_score_fallback_without_snapshot():
    row = load_rows([record("s", 1, near_miss_bull(), after_moves=UP)])[0]
    row["raw_snapshot_json"] = None
    result = replay_decision(row, ThresholdSet(60.0, 40.0, 20.0))
    assert result["method"] == "stored_scores" and result["decision"] == "bullish"
    stale = load_rows([record("s", 2, strong_bull(TNA=1.5))])[0]
    stale["raw_snapshot_json"] = "not json"
    result = replay_decision(stale, ThresholdSet(60.0, 30.0, 10.0))
    assert result["decision"] == "skip" and result["skip_reason"] == "stale_market_data"


def test_simulation_does_not_mutate_rows():
    rows = load_rows(mixed_rows())
    before = json.dumps(rows, sort_keys=True, default=str)
    safe, _ = _grid()
    simulate_thresholds(rows, safe)
    analyze_rows(rows)
    assert json.dumps(rows, sort_keys=True, default=str) == before


def test_threshold_simulation_does_not_write_to_db(db_session, capsys):
    repo = _store(db_session, mixed_rows())
    before = _dump(db_session)
    db_session.commit = MagicMock(side_effect=AssertionError("analytics must not commit"))
    db_session.add = MagicMock(side_effect=AssertionError("analytics must not add"))
    code = script.run_analysis(
        _settings(),
        simulate=True,
        rows_loader=lambda s, run_ids, limit: repo.list_signals(run_ids=run_ids, limit=limit),
    )
    assert code == 0
    assert not db_session.new and not db_session.dirty and not db_session.deleted
    assert _dump(db_session) == before
    assert "DIAGNOSTIC ONLY" in capsys.readouterr().out


def test_cli_reads_real_sqlite_file_without_writing(tmp_path, capsys):
    url = f"sqlite:///{tmp_path / 'shadow.db'}"
    engine = create_engine(url)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    _store(session, mixed_rows("run-a") + [record("run-b", 9, choppy(), after_moves=FLAT)])
    before = _dump(session)
    session.close()

    code = script.run_analysis(_settings(database_url=url), run_ids=["run-a", "run-c"], simulate=True)
    out = capsys.readouterr().out
    assert code == 0
    assert "total_cycles: 4" in out
    assert "warning: run_id not found: run-c" in out
    assert "run-b" not in out.split("--- run comparison ---")[1].split("---")[0]

    check = sessionmaker(bind=create_engine(url))()
    assert _dump(check) == before
    check.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _loader(records):
    return lambda s, run_ids, limit: [r for r in records if not run_ids or r.run_id in run_ids][:limit]


def test_cli_text_report_sections(capsys):
    records = mixed_rows("shadow-a") + [record("shadow-b", i, choppy(), after_moves=FLAT) for i in range(5, 8)]
    code = script.run_analysis(_settings(), simulate=True, rows_loader=_loader(records))
    out = capsys.readouterr().out
    assert code == 0
    for text in (
        "production_order_execution: blocked",
        "total_cycles: 7",
        "skip_reasons:",
        "freshness_pass_pct:",
        "average_quote_age_seconds:",
        "avg_iwm_move_pct:",
        "avg_tna_move_pct:",
        "avg_tza_move_pct:",
        "enough_movement_after_skip:",
        "market_window_status:",
        "recommendation:",
        "--- run comparison ---",
        "shadow-a: cycles=4",
        "shadow-b: cycles=3",
        "rejected_unsafe: entry=60 opposing=50",
        "entry=70 opposing=40 gap=20 [current default]",
        "--- false signal examples (hypothetical) ---",
        "best_candidate:",
        "orders_submitted: 0  writes_to_database: false",
    ):
        assert text in out, text


def test_cli_multiple_run_ids_compare(capsys):
    records = (
        mixed_rows("shadow-2026-10-02-a")
        + [record("shadow-2026-10-02-b", i, choppy(), after_moves=FLAT) for i in range(5, 7)]
        + [record("shadow-2026-10-02-c", i, near_miss_bull(), after_moves=UP) for i in range(7, 9)]
    )
    code = script.run_analysis(
        _settings(),
        run_ids=script._parse_run_ids("shadow-2026-10-02-a,shadow-2026-10-02-c"),
        json_output=True,
        rows_loader=_loader(records),
    )
    doc = json.loads(capsys.readouterr().out)
    assert code == 0
    assert [r["run_id"] for r in doc["run_comparison"]] == ["shadow-2026-10-02-a", "shadow-2026-10-02-c"]
    assert doc["analysis"]["total_cycles"] == 6
    assert doc["threshold_simulation"] is None


def test_cli_json_output_redacts_secrets(capsys):
    code = script.run_analysis(_settings(), simulate=True, json_output=True, rows_loader=_loader(mixed_rows()))
    out = capsys.readouterr().out
    assert code == 0
    doc = json.loads(out)
    assert doc["analytics_only"] is True
    assert doc["execution"]["production_order_execution"] == "blocked"
    assert doc["threshold_simulation"]["best_candidate"]["applied"] is False
    for secret in SECRETS:
        assert secret not in out


def test_cli_db_error_redacts_secrets(capsys):
    def broken(*_a):
        raise RuntimeError(f"connect failed for {DB_PASSWORD}")

    assert script.run_analysis(_settings(), json_output=True, rows_loader=broken) == 2
    out = capsys.readouterr().out
    assert DB_PASSWORD not in out


@pytest.mark.parametrize(
    "kwargs",
    [
        {"min_entry": "50,70"},
        {"min_score_gap": "5"},
        {"max_opposing": "-10"},
        {"min_entry": "abc"},
    ],
)
def test_cli_rejects_unsafe_candidates(kwargs, capsys):
    code = script.run_analysis(_settings(), simulate=True, rows_loader=_loader(mixed_rows()), **kwargs)
    assert code == 2
    assert "safe minimums" in capsys.readouterr().out


@pytest.mark.parametrize("kwargs", [{"limit": 0}, {"limit": 20000}, {"min_followup_move_pct": 0}])
def test_cli_rejects_invalid_args(kwargs):
    assert script.run_analysis(_settings(), rows_loader=_loader([]), **kwargs) == 2


@pytest.mark.parametrize(
    "settings", [_settings(tastytrade_env="production"), _settings(trading_mode="live"), _settings(live_trading_enabled=True)]
)
def test_cli_refuses_non_sandbox_execution(settings, capsys):
    assert script.run_analysis(settings, rows_loader=_loader(mixed_rows())) == 2


def test_cli_empty_database_is_ok(capsys):
    assert script.run_analysis(_settings(), simulate=True, rows_loader=_loader([])) == 0
    out = capsys.readouterr().out
    assert "total_cycles: 0" in out
    assert "market_window_status: insufficient_data" in out


def test_cli_main_parses_args(monkeypatch, capsys):
    monkeypatch.setattr(script, "load_settings", lambda **_kw: _settings())
    monkeypatch.setattr(script, "_default_rows_loader", _loader(mixed_rows("shadow-x")))
    assert script.main(["--run-id", "shadow-x", "--simulate-thresholds", "--min-entry", "60,70", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["run_ids"] == ["shadow-x"]
    assert doc["threshold_simulation"]["sets_evaluated"] == 15  # 2*3*3 minus 3 unsafe (60/50)
    assert len(doc["threshold_simulation"]["rejected_unsafe_sets"]) == 3
    assert script.main(["--simulate-thresholds", "--min-entry", "55"]) == 2


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
    "DXLinkStreamClient",
)


@pytest.mark.parametrize("module", [analytics, script])
def test_threshold_simulation_never_submits_orders(module):
    source = inspect.getsource(module)
    for token in _FORBIDDEN:
        assert token not in source, f"{module.__name__} references {token}"


def test_analytics_never_writes_db_env_or_files():
    source = inspect.getsource(analytics)
    for token in (".commit(", ".add(", "update_followup", "log_signal", "os.environ", "open(", "setenv", "write("):
        assert token not in source, token
    script_source = inspect.getsource(script)
    for token in (".commit(", ".add(", "update_followup", "log_signal", "os.environ", "setenv", "create_table=True"):
        assert token not in script_source, token


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


def test_snapshot_round_trip():
    snapshot = MarketSnapshot(quotes={x.symbol: x for x in near_miss_bull()}, created_at=NOW)
    rebuilt = MarketSnapshot.from_dict(json.loads(json.dumps(snapshot.to_dict())))
    assert rebuilt.created_at == NOW
    for symbol, quote in snapshot.quotes.items():
        assert rebuilt.get(symbol) == quote
