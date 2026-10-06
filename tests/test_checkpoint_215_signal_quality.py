"""Checkpoint 2.15 — Signal Engine quality gates (anti-chop / continuation) and offline version replay."""

from __future__ import annotations

import inspect
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import backend.database  # noqa: F401
import backend.db.models  # noqa: F401
import backend.shadow_mode.engine_replay as engine_replay
import backend.signals.quality_gates as quality_gates
import backend.signals.tna_tza_signal_engine as engine_mod
from backend.config.settings import ConfigurationError, Settings, load_settings
from backend.db.base import Base
from backend.db.models import ShadowSignalLog
from backend.execution.execution_router import ExecutionRouter
from backend.market_data.stream_models import SymbolStreamState
from backend.repositories.shadow_signal_repository import ShadowSignalRepository
from backend.risk.live_guard import LiveTradingBlockedError, assert_order_execution_allowed
from backend.risk.models import OrderIntent, RiskContext
from backend.shadow_mode.engine_replay import LOW_EXPECTED_MOVE_TAG, compare_engine_versions, replay_row
from backend.shadow_mode.analytics import load_rows
from backend.shadow_mode.models import ShadowCycleRecord, compute_followup_outcome
from backend.signals import (
    QUALITY_FIELDS,
    QUALITY_GATE_REASONS,
    SIGNAL_ENGINE_VERSION,
    MarketSnapshot,
    SignalDirection,
    SignalEngineConfig,
    SignalReason,
    SymbolQuote,
    TnaTzaSignalEngine,
)
from scripts import replay_signal_engine_versions as script

_NO_ENV = Path("/nonexistent/.env")
NOW = datetime(2026, 10, 5, 14, 0, 0, tzinfo=timezone.utc)
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


def _engine(**overrides) -> TnaTzaSignalEngine:
    return TnaTzaSignalEngine(SignalEngineConfig(**overrides), wall_clock=lambda: NOW)


# ---------------------------------------------------------------------------
# Snapshot fixtures
# ---------------------------------------------------------------------------


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
    return SymbolQuote(symbol="VIX", last=level, prev_close=prev, quote_age_seconds=age, quote_updates=3, diagnostic_only=True)


def snap(quotes, *extra, at=NOW) -> MarketSnapshot:
    return MarketSnapshot(quotes={x.symbol: x for x in [*quotes, *extra]}, created_at=at)


def strong_bull(**ages):
    """IWM +0.46% in-window, SPY/QQQ up in-window and vs open, TNA up / TZA down."""
    return [
        q("TNA", 45.0, open_=43.0, prev=42.5, first=44.0, age=ages.get("TNA", 0.2)),
        q("TZA", 10.0, open_=10.5, prev=10.6, first=10.2),
        q("IWM", 220.0, open_=218.0, prev=217.0, first=219.0, high=220.0, low=219.0),
        q("SPY", 580.0, open_=575.0, prev=574.0, first=579.0),
        q("QQQ", 500.0, open_=495.0, prev=494.0, first=499.0),
    ]


def strong_bear():
    return [
        q("TNA", 41.0, open_=43.0, prev=43.5, first=42.0),
        q("TZA", 11.0, open_=10.5, prev=10.4, first=10.8),
        q("IWM", 216.0, open_=218.0, prev=219.0, first=217.0, high=217.0, low=216.0),
        q("SPY", 570.0, open_=575.0, prev=576.0, first=571.0),
        q("QQQ", 490.0, open_=495.0, prev=496.0, first=491.0),
    ]


def level_only_bull():
    """Above open / previous close everywhere (v1 score 80) but zero IWM window momentum."""
    return [
        q("TNA", 45.0, open_=43.0, prev=42.5),
        q("TZA", 10.0, open_=10.5, prev=10.6),
        q("IWM", 220.0, open_=218.0, prev=217.0, first=220.0),
        q("SPY", 580.0, open_=575.0, prev=574.0, first=580.0),
        q("QQQ", 500.0, open_=495.0, prev=494.0, first=500.0),
    ]


def mild_bull(*, spy_first=580.0, qqq_first=500.0):
    """IWM +0.045% in-window (above 0.03 momentum, below 2x): mild continuation."""
    return [
        q("TNA", 45.0, open_=43.0, prev=42.5),
        q("TZA", 10.0, open_=10.5, prev=10.6),
        q("IWM", 220.0, open_=218.0, prev=217.0, first=219.9),
        q("SPY", 580.0, open_=575.0, prev=574.0, first=spy_first),
        q("QQQ", 500.0, open_=495.0, prev=494.0, first=qqq_first),
    ]


def pullback_bull():
    """Levels bullish, but IWM slipped 0.045% during the window."""
    quotes = level_only_bull()
    quotes[2] = q("IWM", 220.0, open_=218.0, prev=217.0, first=220.1, high=220.1, low=220.0)
    return quotes


def fading_bull():
    """IWM ran +0.68% to 220.5 then gave back 67% of it: still up 0.23%, but fading."""
    quotes = strong_bull()
    quotes[2] = q("IWM", 219.5, open_=218.0, prev=217.0, first=219.0, high=220.5, low=219.0)
    return quotes


def overextended_bull():
    """IWM 1.6% above open with only +0.036% fresh window move."""
    quotes = strong_bull()
    quotes[2] = q("IWM", 221.5, open_=218.0, prev=217.0, first=221.42, high=221.5, low=221.42)
    return quotes


def stalling_bull():
    """IWM 0.9% above open but the whole window ranged < 0.015%."""
    quotes = strong_bull()
    quotes[2] = q("IWM", 220.0, open_=218.0, prev=217.0, first=219.99, high=220.01, low=219.99)
    return quotes


def pair_strong_bull():
    """IWM barely moved in-window, but TNA +2.3% / TZA -2% in-window and SPY/QQQ confirm."""
    quotes = strong_bull()
    quotes[2] = q("IWM", 220.0, open_=218.0, prev=217.0, first=219.956)
    return quotes


# ---------------------------------------------------------------------------
# Quality gates
# ---------------------------------------------------------------------------


def test_strong_bullish_continuation_still_returns_tna():
    decision = _engine().decide(snap(strong_bull()))
    assert decision.decision is SignalDirection.BULLISH
    assert decision.selected_symbol == "TNA"
    assert decision.skip_reason is None
    assert decision.quality.quality_gate_passed is True
    assert decision.quality.quality_gate_reason is None
    assert decision.quality.continuation_score == 100.0
    assert decision.quality.confirmation_score == 100.0
    assert decision.quality.pullback_risk_score == 0.0
    assert decision.confidence_score == decision.bullish_score == 100.0
    assert "quality gates passed" in decision.explanation


def test_strong_bearish_continuation_still_returns_tza():
    decision = _engine().decide(snap(strong_bear()))
    assert decision.decision is SignalDirection.BEARISH
    assert decision.selected_symbol == "TZA"
    assert decision.quality.quality_gate_passed is True
    assert decision.quality.direction == "bearish"
    assert decision.quality.metrics["iwm_window_move_pct"] > 0  # signed: positive = with the candidate


def test_level_only_bullish_returns_weak_continuation():
    decision = _engine().decide(snap(level_only_bull()))
    assert decision.bullish_score >= 70  # v1 would have selected TNA here
    assert decision.decision is SignalDirection.SKIP
    assert decision.skip_reason is SignalReason.WEAK_CONTINUATION
    assert decision.selected_symbol is None
    assert decision.freshness_gate_passed is True
    assert decision.quality.quality_gate_passed is False
    assert decision.quality.quality_gate_reason == "weak_continuation"
    assert decision.explanation.startswith("quality gate weak_continuation")


def test_level_only_bearish_returns_weak_continuation():
    quotes = strong_bear()
    quotes[2] = q("IWM", 216.0, open_=218.0, prev=219.0, first=216.0)
    quotes[0] = q("TNA", 41.0, open_=43.0, prev=43.5)
    quotes[1] = q("TZA", 11.0, open_=10.5, prev=10.4)
    decision = _engine().decide(snap(quotes))
    assert decision.skip_reason is SignalReason.WEAK_CONTINUATION


def test_mild_bullish_with_flat_spy_qqq_returns_choppy_confirmation():
    decision = _engine().decide(snap(mild_bull()))
    assert decision.bullish_score >= 70
    assert decision.skip_reason is SignalReason.CHOPPY_CONFIRMATION
    assert decision.quality.quality_gate_reason == "choppy_confirmation"
    assert decision.quality.chop_risk_score >= 25


def test_mild_bullish_with_mixed_spy_qqq_returns_choppy_confirmation():
    decision = _engine().decide(snap(mild_bull(spy_first=579.0, qqq_first=501.0)))
    assert decision.skip_reason is SignalReason.CHOPPY_CONFIRMATION


def test_broad_confirmation_must_support_mild_move():
    supported = _engine().decide(snap(mild_bull(spy_first=579.0, qqq_first=499.0)))
    assert supported.decision is SignalDirection.BULLISH
    assert supported.quality.confirmation_score == 100.0
    unsupported = _engine().decide(snap(mild_bull(spy_first=580.0, qqq_first=499.0)))
    assert unsupported.skip_reason is SignalReason.CHOPPY_CONFIRMATION


def test_spy_qqq_both_disagreeing_still_broad_market_disagreement():
    quotes = strong_bull()
    quotes[3] = q("SPY", 570.0, open_=575.0, prev=576.0, first=571.0)
    quotes[4] = q("QQQ", 490.0, open_=495.0, prev=496.0, first=491.0)
    decision = _engine().decide(snap(quotes))
    assert decision.skip_reason is SignalReason.BROAD_MARKET_DISAGREEMENT
    assert decision.quality.evaluated is True
    assert decision.quality.quality_gate_reason == "no_candidate"


def test_pullback_against_candidate_returns_pullback_risk():
    decision = _engine().decide(snap(pullback_bull()))
    assert decision.bullish_score >= 70
    assert decision.skip_reason is SignalReason.PULLBACK_RISK
    assert decision.quality.pullback_risk_score > 0


def test_fading_window_returns_pullback_risk():
    decision = _engine().decide(snap(fading_bull()))
    assert decision.skip_reason is SignalReason.PULLBACK_RISK
    assert decision.quality.metrics["iwm_window_retrace_ratio"] >= 0.6
    assert "fading" in decision.explanation


def test_partial_retrace_penalizes_confidence_but_passes():
    quotes = strong_bull()
    quotes[2] = q("IWM", 220.0, open_=218.0, prev=217.0, first=219.0, high=220.9, low=219.0)
    decision = _engine().decide(snap(quotes))
    assert decision.decision is SignalDirection.BULLISH
    assert 0 < decision.quality.confidence_penalty <= 25
    assert decision.confidence_score == pytest.approx(decision.bullish_score - decision.quality.confidence_penalty)
    assert any("pullback risk" in w for w in decision.warnings)


def test_pullback_penalty_can_drop_below_entry_and_skip():
    quotes = strong_bull()
    quotes[2] = q("IWM", 219.9, open_=218.0, prev=217.0, first=219.0, high=221.0, low=219.0)
    decision = _engine().decide(snap(quotes, vix(27.0, 25.0)))  # VIX high + rising: 100 - 25 = 75
    assert decision.bullish_score == 75.0
    assert decision.skip_reason is SignalReason.PULLBACK_RISK
    assert "pullback penalty" in decision.explanation


def test_overextended_without_acceleration_returns_overextended_or_stalling():
    decision = _engine().decide(snap(overextended_bull()))
    assert decision.skip_reason is SignalReason.OVEREXTENDED_OR_STALLING
    assert decision.quality.metrics["iwm_extension_pct"] >= 1.5


def test_overextended_with_strong_acceleration_still_trades():
    quotes = strong_bull()
    quotes[2] = q("IWM", 221.5, open_=218.0, prev=217.0, first=220.0, high=221.5, low=220.0)
    assert _engine().decide(snap(quotes)).decision is SignalDirection.BULLISH


def test_stalling_window_returns_overextended_or_stalling():
    decision = _engine().decide(snap(stalling_bull()))
    assert decision.skip_reason is SignalReason.OVEREXTENDED_OR_STALLING
    assert "stalling" in decision.explanation


def test_strong_tna_tza_pair_rescues_small_iwm_move():
    decision = _engine().decide(snap(pair_strong_bull()))
    assert decision.decision is SignalDirection.BULLISH
    assert decision.quality.metrics["iwm_window_move_pct"] < 0.03


def test_stale_data_still_skips_before_quality_scoring():
    with patch.object(engine_mod, "assess_quality", side_effect=AssertionError("quality must not run")):
        decision = _engine().decide(snap(level_only_bull()[:1] + [q("TZA", 10.0, age=1.5)] + level_only_bull()[2:]))
    assert decision.skip_reason is SignalReason.STALE_MARKET_DATA
    assert decision.freshness_gate_passed is False
    assert decision.quality.evaluated is False
    assert decision.quality.quality_gate_reason == "not_evaluated"
    assert decision.quality.continuation_score is None


def test_missing_quote_still_skips_before_quality_scoring():
    decision = _engine().decide(snap(strong_bull()[:4]))
    assert decision.skip_reason is SignalReason.MISSING_REQUIRED_QUOTE
    assert decision.quality.evaluated is False


def test_vix_handling_unchanged():
    assert _engine().decide(snap(strong_bull(), vix(40.0, 30.0))).skip_reason is SignalReason.HIGH_VOLATILITY
    calm = _engine().decide(snap(strong_bull(), vix(15.0, 15.5)))
    elevated = _engine().decide(snap(strong_bull(), vix(27.0, 24.0)))
    assert calm.decision is elevated.decision is SignalDirection.BULLISH
    assert elevated.confidence_score == calm.confidence_score - 25.0
    assert {p.name for p in elevated.score_breakdown.penalties} >= {"vix_high", "vix_rising"}
    stale_vix = _engine().decide(snap(strong_bull(), vix(15.0, 15.5, age=120.0)))
    assert stale_vix.decision is SignalDirection.BULLISH


def test_thresholds_unchanged():
    cfg = SignalEngineConfig()
    assert (cfg.entry_score_threshold, cfg.opposing_score_max, cfg.min_score_gap) == (70.0, 40.0, 20.0)
    assert cfg.max_quote_age_seconds == 1.0
    assert cfg.momentum_threshold_pct == 0.03


def test_gates_never_create_or_flip_a_trade():
    for quotes in (level_only_bull(), mild_bull(), pullback_bull(), fading_bull(), overextended_bull(), stalling_bull()):
        with patch.object(engine_mod, "gate_failure", return_value=None):
            v1 = _engine().decide(snap(quotes))
        v2 = _engine().decide(snap(quotes))
        assert v1.decision is SignalDirection.BULLISH
        assert v2.decision is SignalDirection.SKIP
        assert v2.skip_reason in {SignalReason(r) for r in QUALITY_GATE_REASONS}


# ---------------------------------------------------------------------------
# Models / diagnostics
# ---------------------------------------------------------------------------


def test_new_skip_reasons_exist():
    assert {r.value for r in SignalReason} >= {
        "weak_continuation",
        "choppy_confirmation",
        "pullback_risk",
        "overextended_or_stalling",
    }
    assert QUALITY_GATE_REASONS == {"weak_continuation", "choppy_confirmation", "pullback_risk", "overextended_or_stalling"}


@pytest.mark.parametrize(
    "quotes",
    [
        strong_bull(),
        level_only_bull(),
        strong_bull(TNA=2.0),
        strong_bull()[:4],
        [q("TNA", 43.0, open_=43.0), q("TZA", 10.5, open_=10.5), q("IWM", 218.0, open_=218.0, prev=218.0, first=218.0),
         q("SPY", 575.0, open_=575.0), q("QQQ", 495.0, open_=495.0)],
    ],
)
def test_every_decision_includes_diagnostic_scores(quotes):
    data = _engine().decide(snap(quotes)).to_dict()
    for name in ("continuation_score", "confirmation_score", "chop_risk_score", "pullback_risk_score",
                 "quality_gate_passed", "quality_gate_reason"):
        assert name in data, name
    assert set(QUALITY_FIELDS) <= set(data)
    assert isinstance(data["quality_gate_passed"], bool)
    assert data["engine_version"] == SIGNAL_ENGINE_VERSION
    json.dumps(data)


def test_scored_skip_carries_scores_with_no_candidate_reason():
    quotes = level_only_bull()
    quotes[3] = q("SPY", 575.0, open_=575.0, prev=574.0)
    quotes[4] = q("QQQ", 495.0, open_=495.0, prev=494.0)
    quotes[0] = q("TNA", 43.0, open_=43.0, prev=42.5)
    quotes[1] = q("TZA", 10.5, open_=10.5, prev=10.6)
    decision = _engine().decide(snap(quotes))
    assert decision.skip_reason is SignalReason.INSUFFICIENT_SIGNAL_STRENGTH
    assert decision.quality.evaluated is True
    assert decision.quality.quality_gate_passed is False
    assert decision.quality.quality_gate_reason == "no_candidate"
    assert decision.quality.continuation_score is not None


def test_stream_tracks_window_high_and_low():
    sym = SymbolStreamState("IWM")
    for i, (bid, ask) in enumerate([(219.98, 220.02), (220.48, 220.52), (219.48, 219.52), (219.98, 220.02)]):
        sym.apply({"eventType": "Quote", "bidPrice": bid, "askPrice": ask}, received_at=float(i), wall_time=NOW)
    assert sym.first_mid == pytest.approx(220.0)
    assert sym.window_high_mid == pytest.approx(220.5)
    assert sym.window_low_mid == pytest.approx(219.5)
    snapshot = MarketSnapshot.from_stream_state(SimpleNamespace(symbols={"IWM": sym}), now=3.0, created_at=NOW)
    assert snapshot.get("IWM").window_high_mid == pytest.approx(220.5)
    assert snapshot.get("IWM").window_low_mid == pytest.approx(219.5)


def test_snapshot_round_trip_keeps_window_fields():
    snapshot = snap(fading_bull())
    rebuilt = MarketSnapshot.from_dict(json.loads(json.dumps(snapshot.to_dict())))
    assert rebuilt.get("IWM").window_high_mid == 220.5
    assert _engine().decide(rebuilt).skip_reason is SignalReason.PULLBACK_RISK


def test_old_snapshot_without_window_fields_still_gated():
    data = snap(level_only_bull()).to_dict()
    for quote in data["quotes"].values():
        quote.pop("window_high_mid")
        quote.pop("window_low_mid")
    decision = _engine().decide(MarketSnapshot.from_dict(data))
    assert decision.skip_reason is SignalReason.WEAK_CONTINUATION
    assert decision.quality.metrics["iwm_window_retrace_ratio"] is None


def test_shadow_record_stores_quality_in_raw_score_json():
    snapshot = snap(level_only_bull())
    decision = _engine().decide(snapshot)
    rec = ShadowCycleRecord.from_decision(run_id="r", cycle_number=1, decision=decision, snapshot=snapshot)
    score = json.loads(rec.raw_score_json)
    assert score["engine_version"] == SIGNAL_ENGINE_VERSION
    assert score["quality"]["quality_gate_reason"] == "weak_continuation"
    assert rec.skip_reason == "weak_continuation"


# ---------------------------------------------------------------------------
# Offline replay (old stored decision vs new engine)
# ---------------------------------------------------------------------------

UP = {"IWM": 0.25, "TNA": 0.75, "TZA": -0.75, "SPY": 0.1, "QQQ": 0.1}
DOWN = {"IWM": -0.25, "TNA": -0.75, "TZA": 0.75, "SPY": -0.1, "QQQ": -0.1}
FLAT = {"IWM": 0.005, "TNA": 0.01, "TZA": -0.01}


def _after(mids) -> MarketSnapshot:
    return MarketSnapshot(
        quotes={s: SymbolQuote(s, bid=m - 0.01, ask=m + 0.01, quote_age_seconds=0.3, quote_updates=3) for s, m in mids.items()}
    )


def old_record(run_id, cycle, quotes, *, after_moves=None) -> ShadowCycleRecord:
    """A row as the pre-2.15 engine would have logged it (quality gates disabled)."""
    snapshot = MarketSnapshot(quotes={x.symbol: x for x in quotes}, created_at=NOW + timedelta(minutes=cycle))
    with patch.object(engine_mod, "gate_failure", return_value=None):
        decision = TnaTzaSignalEngine(wall_clock=lambda: snapshot.created_at).decide(snapshot)
    rec = ShadowCycleRecord.from_decision(run_id=run_id, cycle_number=cycle, decision=decision, snapshot=snapshot)
    if rec.decision != "skip":
        rec.confidence_score = max(decision.bullish_score, decision.bearish_score)  # v1 had no pullback penalty
    if after_moves is not None:
        mids_after = {s: rec.mids[s] * (1 + after_moves.get(s, 0.0) / 100) for s in rec.mids if rec.mids[s]}
        rec.apply_followup(
            compute_followup_outcome(
                decision=rec.decision,
                selected_symbol=rec.selected_symbol,
                mids_before=rec.mids,
                after=_after(mids_after),
                followup_seconds=60.0,
            )
        )
    return rec


def replay_rows(run_id="shadow-2026-10-05-open-confirmation"):
    return [
        old_record(run_id, 1, level_only_bull(), after_moves=DOWN),  # old TNA, wrong -> filtered
        old_record(run_id, 2, pullback_bull(), after_moves=DOWN),  # old TNA, wrong -> filtered
        old_record(run_id, 3, strong_bull(), after_moves=UP),  # old TNA, right -> preserved
        old_record(run_id, 4, mild_bull(), after_moves=UP),  # old TNA, right -> missed winner
        old_record(run_id, 5, strong_bull(TNA=1.5), after_moves=UP),  # stale skip stays skip
    ]


def test_replay_compares_old_and_new_decisions():
    result = compare_engine_versions(load_rows(replay_rows()), min_move_pct=0.03)
    assert result["old"]["bullish_count"] == 4
    assert result["old"]["skip_count"] == 1
    assert result["new"]["bullish_count"] == 1
    assert result["new"]["skip_count"] == 4
    assert result["false_signals_filtered"] == 2
    assert result["good_signals_preserved"] == 1
    assert result["missed_winners"] == 1
    assert result["false_signals_kept"] == 0
    assert result["new_signals_from_old_skips"] == 0
    assert result["old"]["correct_pct"] == 50.0
    assert result["new"]["hypothetical_correct_pct"] == 100.0
    assert result["filtered_by_reason"] == {"choppy_confirmation": 1, "pullback_risk": 1, "weak_continuation": 1}
    assert result["new"]["skip_reasons"]["stale_market_data"] == 1
    examples = result["filtered_false_tna_examples"]
    assert [e["new_skip_reason"] for e in examples] == ["weak_continuation", "pullback_risk"]
    assert all(e["old_decision"] == "bullish" and e["old_correct"] is False for e in examples)
    assert result["writes_to_database"] is False
    assert result["orders_submitted"] == 0


def test_replay_does_not_mutate_rows():
    rows = load_rows(replay_rows())
    before = json.dumps(rows, sort_keys=True, default=str)
    compare_engine_versions(rows)
    assert json.dumps(rows, sort_keys=True, default=str) == before


def test_replay_row_without_snapshot_keeps_stored_decision():
    row = load_rows(replay_rows())[0]
    row["raw_snapshot_json"] = None
    assert replay_row(row)["replayed"] is False
    result = compare_engine_versions([row])
    assert result["rows_not_replayed"] == 1
    assert result["new"]["bullish_count"] == 1


def test_expected_move_tag_is_diagnostic_only():
    rows = load_rows([old_record("run-flat", i, strong_bull(), after_moves=FLAT) for i in range(1, 8)])
    result = compare_engine_versions(rows, min_move_pct=0.03)
    em = result["expected_move_diagnostic"]
    assert em["tagged_rows"] == 7
    assert em["applied_to_decisions"] is False
    assert result["new"]["bullish_count"] == 7  # tag never changes a decision
    few = compare_engine_versions(rows[:3], min_move_pct=0.03)
    assert few["expected_move_diagnostic"]["tagged_rows"] == 0  # needs >= 5 similar rows
    assert LOW_EXPECTED_MOVE_TAG == "low_expected_move"


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
    return [{c: getattr(row, c) for c in columns} for row in session.query(ShadowSignalLog).order_by(ShadowSignalLog.id).all()]


def test_replay_script_does_not_write_to_db(db_session, capsys):
    repo = _store(db_session, replay_rows())
    before = _dump(db_session)
    db_session.commit = MagicMock(side_effect=AssertionError("replay must not commit"))
    db_session.add = MagicMock(side_effect=AssertionError("replay must not add"))
    code = script.run_replay(
        _settings(), rows_loader=lambda s, run_ids, limit: repo.list_signals(run_ids=run_ids, limit=limit)
    )
    assert code == 0
    assert not db_session.new and not db_session.dirty and not db_session.deleted
    assert _dump(db_session) == before
    assert "false_signals_filtered: 2" in capsys.readouterr().out


def test_replay_script_reads_real_sqlite_file_without_writing(tmp_path, capsys):
    url = f"sqlite:///{tmp_path / 'shadow.db'}"
    engine = create_engine(url)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    _store(
        session,
        replay_rows("shadow-2026-10-05-open-confirmation") + replay_rows("shadow-2026-10-05-midday-sample"),
    )
    before = _dump(session)
    session.close()

    code = script.run_replay(
        _settings(database_url=url),
        run_ids=["shadow-2026-10-05-open-confirmation", "shadow-2026-10-05-midday-sample"],
        json_output=True,
    )
    doc = json.loads(capsys.readouterr().out)
    assert code == 0
    assert doc["comparison"]["rows_evaluated"] == 10
    assert doc["comparison"]["old"]["bullish_count"] == 8
    assert doc["comparison"]["new"]["bullish_count"] == 2
    check = sessionmaker(bind=create_engine(url))()
    assert _dump(check) == before
    check.close()


def _loader(records):
    return lambda s, run_ids, limit: [r for r in records if not run_ids or r.run_id in run_ids][:limit]


def test_replay_script_text_output(capsys):
    code = script.run_replay(_settings(), rows_loader=_loader(replay_rows()))
    out = capsys.readouterr().out
    assert code == 0
    for text in (
        "production_order_execution: blocked",
        "old_bullish: 4  old_bearish: 0  old_skip: 1",
        "new_bullish: 1  new_bearish: 0  new_skip: 4",
        "false_signals_filtered: 2",
        "good_signals_preserved: 1",
        "missed_winners: 1",
        "new_hypothetical_correct: 1/1 (100%)",
        "--- examples: filtered false TNA signals ---",
        "[weak_continuation]",
        "--- examples: missed winners ---",
        "expected_move_diagnostic:",
        "orders_submitted: 0  writes_to_database: false",
    ):
        assert text in out, text


def test_replay_script_json_redacts_secrets(capsys):
    code = script.run_replay(_settings(), json_output=True, rows_loader=_loader(replay_rows()))
    out = capsys.readouterr().out
    assert code == 0
    doc = json.loads(out)
    assert doc["analytics_only"] is True
    assert doc["writes_to_database"] is False
    assert doc["orders_submitted"] == 0
    assert doc["execution"]["production_order_execution"] == "blocked"
    for secret in SECRETS:
        assert secret not in out


def test_replay_script_db_error_redacts_secrets(capsys):
    def broken(*_a):
        raise RuntimeError(f"connect failed for {DB_PASSWORD}")

    assert script.run_replay(_settings(), json_output=True, rows_loader=broken) == 2
    assert DB_PASSWORD not in capsys.readouterr().out


@pytest.mark.parametrize("kwargs", [{"limit": 0}, {"limit": 20000}, {"min_followup_move_pct": 0}])
def test_replay_script_rejects_invalid_args(kwargs):
    assert script.run_replay(_settings(), rows_loader=_loader([]), **kwargs) == 2


@pytest.mark.parametrize(
    "settings", [_settings(tastytrade_env="production"), _settings(trading_mode="live"), _settings(live_trading_enabled=True)]
)
def test_replay_script_refuses_non_sandbox_execution(settings):
    assert script.run_replay(settings, rows_loader=_loader(replay_rows())) == 2


def test_replay_script_main_parses_args(monkeypatch, capsys):
    records = replay_rows("shadow-2026-10-05-open-confirmation") + replay_rows("shadow-2026-10-05-midday-sample")
    monkeypatch.setattr(script, "load_settings", lambda **_kw: _settings())
    monkeypatch.setattr(script, "_default_rows_loader", _loader(records))
    argv = ["--run-id", "shadow-2026-10-05-midday-sample", "--limit", "500", "--json", "--min-followup-move-pct", "0.03"]
    assert script.main(argv) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["run_ids"] == ["shadow-2026-10-05-midday-sample"]
    assert doc["comparison"]["rows_evaluated"] == 5
    assert doc["comparison"]["min_followup_move_pct"] == 0.03


def test_replay_script_empty_is_ok(capsys):
    assert script.run_replay(_settings(), rows_loader=_loader([])) == 0
    assert "rows_evaluated: 0" in capsys.readouterr().out


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


@pytest.mark.parametrize("module", [quality_gates, engine_mod, engine_replay, script])
def test_replay_and_gates_never_submit_orders(module):
    source = inspect.getsource(module)
    for token in _FORBIDDEN:
        assert token not in source, f"{module.__name__} references {token}"


def test_replay_never_writes_db_env_or_files():
    for module in (engine_replay, script):
        source = inspect.getsource(module)
        for token in (".commit(", ".add(", "update_followup", "log_signal", "os.environ", "setenv", "create_table=True"):
            assert token not in source, f"{module.__name__}: {token}"
    assert "open(" not in inspect.getsource(engine_replay)


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
