"""Checkpoint 2.16 — skip-opportunity analysis, virtual candidates and replay-only strategy variants (diagnostics only)."""

from __future__ import annotations

import inspect
import json
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import backend.shadow_mode.skip_opportunity_analysis as skip_mod
import backend.shadow_mode.strategy_variants as variants_mod
import backend.signals.tna_tza_signal_engine as engine_mod
from backend.config.settings import ConfigurationError, Settings, load_settings
from backend.db.base import Base
from backend.execution.execution_router import ExecutionRouter
from backend.risk.live_guard import LiveTradingBlockedError, assert_order_execution_allowed
from backend.risk.models import OrderIntent
from backend.shadow_mode.analytics import load_rows
from backend.shadow_mode.models import ShadowCycleRecord, compute_followup_outcome
from backend.shadow_mode.skip_opportunity_analysis import (
    analyze_skip_opportunities,
    broad_disagreement_diagnostics,
    classify_followup,
    virtual_candidate_labels,
)
from backend.shadow_mode.strategy_variants import (
    MODERATE,
    NO_BROAD_BLOCK,
    STRICT,
    VARIANTS,
    VariantConfigError,
    compare_variants,
    evaluate_variants,
    parse_variants,
)
from backend.signals import MarketSnapshot, SignalDirection, SignalEngineConfig, SignalReason, TnaTzaSignalEngine
from scripts import analyze_skip_opportunities as skip_script
from scripts import replay_signal_engine_versions as replay_script
from tests.test_checkpoint_215_signal_quality import (
    DB_PASSWORD,
    NOW,
    SECRETS,
    _after,
    _context,
    _dump,
    _loader,
    _settings,
    _store,
    level_only_bull,
    old_record,
    q,
    strong_bull,
    vix,
)

_NO_ENV = Path("/nonexistent/.env")
UP = {"IWM": 0.25, "TNA": 0.75, "TZA": -0.75, "SPY": 0.1, "QQQ": 0.1}
DOWN = {"IWM": -0.25, "TNA": -0.75, "TZA": 0.75, "SPY": -0.1, "QQQ": -0.1}
FLAT = {"IWM": 0.005, "TNA": 0.01, "TZA": -0.01}
MIXED = {"IWM": 0.25, "TNA": 0.01, "TZA": 0.75}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def divergence_bull(*, iwm_first=219.0, spy_first=570.0, qqq_first=490.0, pair=True, with_vix=True):
    """IWM strongly up (levels + window) while SPY/QQQ are below open/prev close: v2 -> broad_market_disagreement."""
    quotes = [
        q("TNA", 45.0, open_=43.0, prev=42.5, first=44.0 if pair else None),
        q("TZA", 10.0, open_=10.5, prev=10.6, first=10.2 if pair else None),
        q("IWM", 220.0, open_=218.0, prev=217.0, first=iwm_first, high=220.0, low=min(iwm_first, 220.0)),
        q("SPY", 570.0, open_=575.0, prev=576.0, first=spy_first),
        q("QQQ", 490.0, open_=495.0, prev=496.0, first=qqq_first),
    ]
    return quotes + ([vix(15.0, 15.5)] if with_vix else [])


def insufficient_bull():
    return [
        q("TNA", 45.0, open_=43.0, prev=42.5),
        q("TZA", 10.0, open_=10.5, prev=10.6),
        q("IWM", 220.0, open_=218.0, prev=217.0, first=220.0),
        q("SPY", 575.0, open_=575.0, prev=574.0, first=575.0),
        q("QQQ", 495.0, open_=495.0, prev=494.0, first=495.0),
    ]


def record(run_id, cycle, quotes, *, after_moves=None) -> ShadowCycleRecord:
    """A row logged by the current (live) v2 engine."""
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
                followup_seconds=60.0,
            )
        )
    return rec


OPEN_RUN = "shadow-2026-10-06-v2-quality-open"
MIDDAY_RUN = "shadow-2026-10-06-v2-quality-midday"


def skip_rows(run_id=OPEN_RUN):
    return [
        record(run_id, 1, divergence_bull(), after_moves=UP),  # broad block, later up -> blocked winner
        record(run_id, 2, divergence_bull(), after_moves=UP),  # blocked winner
        record(run_id, 3, divergence_bull(spy_first=571.0, qqq_first=491.0), after_moves=DOWN),  # avoided bad trade
        record(run_id, 4, divergence_bull(), after_moves=FLAT),  # flat -> correct skip
        record(run_id, 5, insufficient_bull(), after_moves=UP),  # insufficient strength, later up
        record(run_id, 6, insufficient_bull(), after_moves=MIXED),  # mixed -> correct skip
        record(run_id, 7, strong_bull(TNA=1.5), after_moves=DOWN),  # stale: data-quality skip
    ]


def _row(rec):
    return load_rows([rec])[0]


# ---------------------------------------------------------------------------
# Follow-up classification / virtual candidates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "moves,expected",
    [(UP, "bullish"), (DOWN, "bearish"), (FLAT, "flat"), (MIXED, "mixed"), ({"IWM": 0.0, "TNA": 0.05}, "bullish"),
     ({"IWM": 0.0, "TZA": 0.05}, "bearish"), ({"IWM": -0.04}, "bearish")],
)
def test_followup_classification(moves, expected):
    row = _row(record("r", 1, insufficient_bull(), after_moves=moves))
    assert classify_followup(row, min_move_pct=0.03) == expected


def test_followup_threshold_and_missing_followup():
    row = _row(record("r", 1, insufficient_bull(), after_moves={"IWM": 0.04, "TNA": 0.04}))
    assert classify_followup(row, min_move_pct=0.03) == "bullish"
    assert classify_followup(row, min_move_pct=0.05) == "flat"
    assert classify_followup(_row(record("r", 2, insufficient_bull())), min_move_pct=0.03) is None


@pytest.mark.parametrize(
    "moves,label",
    [(UP, "would_have_preferred_tna"), (DOWN, "would_have_preferred_tza"), (FLAT, "would_have_skipped"),
     (MIXED, "would_have_skipped")],
)
def test_virtual_candidate_labels(moves, label):
    labels = virtual_candidate_labels(_row(record("r", 1, divergence_bull(), after_moves=moves)), min_move_pct=0.03)
    flags = {k: labels[k] for k in ("would_have_preferred_tna", "would_have_preferred_tza", "would_have_skipped")}
    assert flags[label] is True
    assert sum(flags.values()) == 1
    assert labels["evidence_lean"] == "bullish"
    assert labels["quality_direction"] == "bullish"


def test_virtual_lean_matches_followup():
    up = virtual_candidate_labels(_row(record("r", 1, divergence_bull(), after_moves=UP)), min_move_pct=0.03)
    down = virtual_candidate_labels(_row(record("r", 1, divergence_bull(), after_moves=DOWN)), min_move_pct=0.03)
    flat = virtual_candidate_labels(_row(record("r", 1, divergence_bull(), after_moves=FLAT)), min_move_pct=0.03)
    assert (up["lean_matches_followup"], down["lean_matches_followup"], flat["lean_matches_followup"]) == (True, False, None)


# ---------------------------------------------------------------------------
# Skip opportunity analysis
# ---------------------------------------------------------------------------


def test_skip_opportunity_classification_and_counts():
    rows = load_rows(skip_rows() + [old_record(OPEN_RUN, 9, strong_bull(), after_moves=UP)])  # one trade row ignored
    a = analyze_skip_opportunities(rows, min_move_pct=0.03)
    assert a["total_rows"] == 8
    assert a["total_skips"] == 7
    assert a["skips_with_followup"] == 7
    assert a["actionable_skips"] == 6
    assert a["skip_reasons"] == {"broad_market_disagreement": 4, "insufficient_signal_strength": 2, "stale_market_data": 1}
    assert a["followup_direction"] == {
        "bullish_followup": 3, "bearish_followup": 2, "flat_followup": 1, "mixed_followup": 1, "no_followup": 0,
    }
    broad = a["opportunity_by_skip_reason"]["broad_market_disagreement"]
    assert (broad["skips"], broad["bullish"], broad["bearish"], broad["flat"]) == (4, 2, 1, 1)
    assert broad["opportunity_pct"] == 75.0
    assert a["missed_bullish_opportunities"]["count"] == 3
    assert a["missed_bullish_opportunities"]["by_skip_reason"] == {"broad_market_disagreement": 2, "insufficient_signal_strength": 1}
    assert a["missed_bullish_opportunities"]["evidence_leaned_same_way"] == 3
    assert a["missed_bearish_opportunities"]["count"] == 1  # stale DOWN row is a data-quality skip, not counted
    assert a["data_quality_skips_with_movement"] == 1
    assert a["virtual_candidates"] == {
        "would_have_preferred_tna": 3, "would_have_preferred_tza": 2, "would_have_skipped": 2, "applied_to_decisions": False,
    }
    assert a["writes_to_database"] is False and a["orders_submitted"] == 0


def test_skip_opportunity_groups_by_score_and_quality():
    a = analyze_skip_opportunities(load_rows(skip_rows()), min_move_pct=0.03)
    assert "bullish/70+" in a["opportunity_by_score_profile"]
    assert "bullish/40-59" in a["opportunity_by_score_profile"]
    assert sum(g["skips"] for g in a["opportunity_by_score_profile"].values()) == 6
    assert all(k.startswith("continuation_") for k in a["opportunity_by_quality_scores"])
    old_rows = load_rows([old_record("old", 1, insufficient_bull(), after_moves=UP)])
    for row in old_rows:
        score = json.loads(row["raw_score_json"])
        score.pop("quality")
        row["raw_score_json"] = json.dumps(score)
    assert "quality_not_evaluated" in analyze_skip_opportunities(old_rows)["opportunity_by_quality_scores"]


def test_skip_opportunity_examples():
    a = analyze_skip_opportunities(load_rows(skip_rows()), min_move_pct=0.03)
    broad_examples = a["broad_market_disagreement_moved_examples"]
    assert [e["cycle_number"] for e in broad_examples] == [1, 2, 3]
    assert all("broad" in e for e in broad_examples)
    assert [e["cycle_number"] for e in a["insufficient_signal_strength_moved_examples"]] == [5, 6]
    assert {e["followup_class"] for e in a["correct_skip_examples"]} == {"flat", "mixed"}


def test_broad_market_disagreement_diagnostics_row():
    d = broad_disagreement_diagnostics(_row(record("r", 1, divergence_bull(), after_moves=UP)), min_move_pct=0.03)
    assert d["iwm_direction"] == "up"
    assert d["iwm_window_direction"] == "up"
    assert d["tna_tza_window_confirmation"] == "confirming"
    assert (d["spy_level_direction"], d["qqq_level_direction"]) == ("down", "down")
    assert (d["spy_window_direction"], d["qqq_window_direction"]) == ("flat", "flat")
    assert d["spy_qqq_level_disagreed"] is True
    assert d["spy_qqq_window_disagreed"] is False
    assert d["iwm_followup_validated"] is True
    assert d["etf_followup_symbol"] == "TNA"
    assert d["etf_followup_validated"] is True
    assert d["blocked_trade_outcome"] == "blocked_winner"


def test_broad_market_disagreement_summary():
    b = analyze_skip_opportunities(load_rows(skip_rows()), min_move_pct=0.03)["broad_market_disagreement"]
    assert b["rows"] == 4
    assert b["blocked_winner"] == 2
    assert b["avoided_bad_trade"] == 1
    assert b["flat_or_unscored"] == 1
    assert b["iwm_followup_validated_iwm_direction"] == 2
    assert b["iwm_followup_contradicted_iwm_direction"] == 1
    assert b["etf_followup_validated"] == 2
    assert b["etf_followup_contradicted"] == 1
    assert b["spy_qqq_also_disagreed_in_window"] == 1
    assert b["tna_tza_window_confirmation"]["confirming"] == 4


def test_skip_analysis_empty():
    a = analyze_skip_opportunities([])
    assert a["total_skips"] == 0 and a["broad_market_disagreement"]["rows"] == 0


# ---------------------------------------------------------------------------
# Replay-only strategy variants
# ---------------------------------------------------------------------------


def _variant_decisions(quotes):
    rec = record("r", 1, quotes, after_moves=UP)
    return {k: v["decision"] for k, v in evaluate_variants(_row(rec)).items()}


@pytest.mark.parametrize(
    "quotes,expected",
    [
        (divergence_bull(), {"current_v2": "skip", STRICT: "bullish", MODERATE: "bullish", NO_BROAD_BLOCK: "bullish"}),
        (divergence_bull(spy_first=571.0, qqq_first=491.0),  # SPY/QQQ also falling in-window
         {"current_v2": "skip", STRICT: "skip", MODERATE: "skip", NO_BROAD_BLOCK: "bullish"}),
        (divergence_bull(spy_first=571.0),  # only SPY falling in-window
         {"current_v2": "skip", STRICT: "skip", MODERATE: "bullish", NO_BROAD_BLOCK: "bullish"}),
        (divergence_bull(with_vix=False), {"current_v2": "skip", STRICT: "skip", MODERATE: "bullish", NO_BROAD_BLOCK: "bullish"}),
        (divergence_bull(pair=False), {"current_v2": "skip", STRICT: "skip", MODERATE: "skip", NO_BROAD_BLOCK: "bullish"}),
        (divergence_bull(iwm_first=219.9),  # mild IWM window move
         {"current_v2": "skip", STRICT: "skip", MODERATE: "bullish", NO_BROAD_BLOCK: "skip"}),
        (strong_bull(), {"current_v2": "bullish", STRICT: "bullish", MODERATE: "bullish", NO_BROAD_BLOCK: "bullish"}),
        (level_only_bull(), {"current_v2": "skip", STRICT: "skip", MODERATE: "skip", NO_BROAD_BLOCK: "skip"}),
        (strong_bull(TNA=1.5), {"current_v2": "skip", STRICT: "skip", MODERATE: "skip", NO_BROAD_BLOCK: "skip"}),
    ],
)
def test_variant_decisions(quotes, expected):
    assert _variant_decisions(quotes) == expected


def test_divergence_variants_respect_vix_and_extreme_vix():
    quotes = divergence_bull(with_vix=False) + [vix(27.0, 26.0)]
    d = _variant_decisions(quotes)
    assert d[STRICT] == "skip" and d[MODERATE] == "skip"
    extreme = _variant_decisions(divergence_bull(with_vix=False) + [vix(40.0, 30.0)])
    assert set(extreme.values()) == {"skip"}


def test_divergence_variants_still_apply_pullback_gates():
    quotes = divergence_bull()
    quotes[2] = q("IWM", 219.5, open_=218.0, prev=217.0, first=219.0, high=220.5, low=219.0)  # fading
    d = _variant_decisions(quotes)
    assert d[STRICT] == "skip" and d[MODERATE] == "skip"


def test_variant_bearish_mirror():
    quotes = [
        q("TNA", 41.0, open_=43.0, prev=43.5, first=42.0),
        q("TZA", 11.0, open_=10.5, prev=10.4, first=10.8),
        q("IWM", 216.0, open_=218.0, prev=219.0, first=217.0, high=217.0, low=216.0),
        q("SPY", 580.0, open_=575.0, prev=574.0, first=580.0),
        q("QQQ", 500.0, open_=495.0, prev=494.0, first=500.0),
        vix(15.0, 15.5),
    ]
    assert _variant_decisions(quotes) == {"current_v2": "skip", STRICT: "bearish", MODERATE: "bearish", NO_BROAD_BLOCK: "bearish"}


def test_variants_produce_only_hypothetical_decisions_and_do_not_mutate_rows():
    rows = load_rows(skip_rows())
    before = json.dumps(rows, sort_keys=True, default=str)
    for row in rows:
        for result in evaluate_variants(row).values():
            assert result["hypothetical"] is True
    report = compare_variants(rows)
    assert json.dumps(rows, sort_keys=True, default=str) == before
    assert report["diagnostic_only"] is True
    assert report["applied_to_live"] is False
    assert report["writes_to_database"] is False
    assert report["orders_submitted"] == 0


def test_variants_do_not_change_live_signal_engine():
    snapshot = MarketSnapshot(quotes={x.symbol: x for x in divergence_bull()}, created_at=NOW)
    live = TnaTzaSignalEngine(wall_clock=lambda: NOW)
    before = live.decide(snapshot).to_dict()
    compare_variants(load_rows(skip_rows()))
    after = live.decide(snapshot).to_dict()
    assert before == after
    assert after["decision"] == "skip" and after["skip_reason"] == "broad_market_disagreement"
    cfg = SignalEngineConfig()
    assert (cfg.entry_score_threshold, cfg.opposing_score_max, cfg.min_score_gap) == (70.0, 40.0, 20.0)
    engine_source = inspect.getsource(engine_mod)
    for token in ("strategy_variants", "skip_opportunity", "shadow_mode"):
        assert token not in engine_source


def test_compare_variants_summary():
    rows = load_rows(skip_rows() + [old_record(OPEN_RUN, 8, level_only_bull(), after_moves=DOWN)])
    report = compare_variants(rows, VARIANTS)
    r = report["results"]
    assert report["old_stored"] == {"bullish_count": 1, "bearish_count": 0, "skip_count": 7}
    assert (r["current_v2"]["bullish_count"], r["current_v2"]["skip_count"]) == (0, 8)
    assert r["current_v2"]["false_signals_filtered"] == 1
    assert r[STRICT]["bullish_count"] == 3  # broad rows 1, 2 and 4 (row 3 has SPY/QQQ falling in-window)
    assert (r[STRICT]["correct_count"], r[STRICT]["incorrect_count"]) == (2, 0)
    assert r[STRICT]["correct_pct"] == 100.0
    assert r[NO_BROAD_BLOCK]["bullish_count"] == 4
    assert (r[NO_BROAD_BLOCK]["correct_count"], r[NO_BROAD_BLOCK]["incorrect_count"]) == (2, 1)
    assert r[NO_BROAD_BLOCK]["correct_pct"] == 66.7
    assert r[NO_BROAD_BLOCK]["new_trades_vs_old"] == 4
    assert r[NO_BROAD_BLOCK]["trades_added_vs_current_v2"] == 4
    for name in VARIANTS:
        assert r[name]["false_signals_filtered"] == 1  # the old level-only TNA stays filtered in every variant
        assert r[name]["missed_winners"] == 0
    assert r[STRICT]["trade_examples"][0]["current_v2_decision"] == "skip"
    assert len(report["row_labels"]) == 8
    assert {"would_have_preferred_tna", f"{STRICT}_decision"} <= set(report["row_labels"][0])


def test_parse_variants():
    assert parse_variants("all") == list(VARIANTS)
    assert parse_variants(STRICT) == ["current_v2", STRICT]
    assert parse_variants(None) == []
    with pytest.raises(VariantConfigError):
        parse_variants("loosen_everything")


# ---------------------------------------------------------------------------
# Scripts
# ---------------------------------------------------------------------------


def combined_rows():
    return skip_rows(OPEN_RUN) + skip_rows(MIDDAY_RUN)


def test_skip_script_text_output(capsys):
    code = skip_script.run_skip_analysis(_settings(), run_ids=[OPEN_RUN, MIDDAY_RUN], rows_loader=_loader(combined_rows()))
    out = capsys.readouterr().out
    assert code == 0
    for text in (
        "DIAGNOSTIC ONLY",
        "live thresholds unchanged (entry=70 opposing=40 gap=20); live signal logic unchanged",
        "production_order_execution: blocked",
        "total_skips: 14",
        "skips_with_followup: 14",
        "skip_reasons: broad_market_disagreement=8",
        "followup_direction: bullish_followup=6 bearish_followup=4 flat_followup=2 mixed_followup=2",
        "--- opportunity by skip reason ---",
        "--- opportunity by score profile",
        "--- opportunity by quality scores",
        "missed_bullish_opportunities: 6",
        "missed_bearish_opportunities: 2",
        "would_have_preferred_tna=6",
        "avoided_bad_trade: 2  blocked_winner: 4",
        "--- examples: broad_market_disagreement rows that later moved enough ---",
        "--- examples: insufficient_signal_strength rows that later moved enough ---",
        "--- examples: SKIP was correct (follow-up flat/choppy) ---",
        "orders_submitted: 0  writes_to_database: false",
    ):
        assert text in out, text


def test_skip_script_json_redacts_secrets(capsys):
    code = skip_script.run_skip_analysis(_settings(), json_output=True, rows_loader=_loader(combined_rows()))
    out = capsys.readouterr().out
    assert code == 0
    doc = json.loads(out)
    assert doc["diagnostic_only"] is True
    assert doc["writes_to_database"] is False and doc["orders_submitted"] == 0
    assert doc["live_thresholds_unchanged"] is True and doc["live_signal_logic_unchanged"] is True
    assert doc["skip_opportunities"]["total_skips"] == 14
    for secret in SECRETS:
        assert secret not in out


def test_skip_script_does_not_write_to_db(tmp_path, capsys):
    url = f"sqlite:///{tmp_path / 'shadow.db'}"
    engine = create_engine(url)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    _store(session, combined_rows())
    before = _dump(session)
    session.close()
    assert skip_script.run_skip_analysis(_settings(database_url=url), run_ids=[OPEN_RUN]) == 0
    assert "total_skips: 7" in capsys.readouterr().out
    check = sessionmaker(bind=create_engine(url))()
    assert _dump(check) == before
    check.close()


def test_skip_script_mocked_session_never_commits(capsys):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    repo = _store(session, skip_rows())
    session.commit = MagicMock(side_effect=AssertionError("must not commit"))
    session.add = MagicMock(side_effect=AssertionError("must not add"))
    code = skip_script.run_skip_analysis(
        _settings(), rows_loader=lambda s, run_ids, limit: repo.list_signals(run_ids=run_ids, limit=limit)
    )
    assert code == 0
    assert not session.new and not session.dirty and not session.deleted
    session.close()


def test_skip_script_db_error_redacts_secrets(capsys):
    def broken(*_a):
        raise RuntimeError(f"connect failed for {DB_PASSWORD}")

    assert skip_script.run_skip_analysis(_settings(), json_output=True, rows_loader=broken) == 2
    assert DB_PASSWORD not in capsys.readouterr().out


@pytest.mark.parametrize("kwargs", [{"limit": 0}, {"limit": 20000}, {"min_followup_move_pct": 0}])
def test_skip_script_rejects_invalid_args(kwargs):
    assert skip_script.run_skip_analysis(_settings(), rows_loader=_loader([]), **kwargs) == 2


@pytest.mark.parametrize(
    "settings", [_settings(tastytrade_env="production"), _settings(trading_mode="live"), _settings(live_trading_enabled=True)]
)
def test_skip_script_refuses_non_sandbox_execution(settings):
    assert skip_script.run_skip_analysis(settings, rows_loader=_loader(skip_rows())) == 2


def test_skip_script_main_parses_args(monkeypatch, capsys):
    monkeypatch.setattr(skip_script, "load_settings", lambda **_kw: _settings())
    monkeypatch.setattr(skip_script, "_default_rows_loader", _loader(combined_rows()))
    argv = ["--run-id", f"{OPEN_RUN},{MIDDAY_RUN}", "--limit", "500", "--min-followup-move-pct", "0.03", "--json"]
    assert skip_script.main(argv) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["run_ids"] == [OPEN_RUN, MIDDAY_RUN]
    assert doc["skip_opportunities"]["total_skips"] == 14


def test_replay_script_variants_text_output(capsys):
    code = replay_script.run_replay(
        _settings(), run_ids=[OPEN_RUN, MIDDAY_RUN], variants="all", rows_loader=_loader(combined_rows())
    )
    out = capsys.readouterr().out
    assert code == 0
    for text in (
        "=== DIAGNOSTIC ONLY: replay-only strategy variants ===",
        "no DB writes | no orders | live thresholds unchanged (entry=70 opposing=40 gap=20) | live signal logic unchanged",
        "--- variant: current_v2 (hypothetical) ---",
        f"--- variant: {STRICT} (hypothetical) ---",
        f"--- variant: {MODERATE} (hypothetical) ---",
        f"--- variant: {NO_BROAD_BLOCK} (hypothetical) ---",
        "false_signals_filtered=",
        "missed_winners=",
        "correct_pct=",
        "  trade: run=",
        "orders_submitted: 0  writes_to_database: false  applied_to_live: false",
    ):
        assert text in out, text


def test_replay_script_without_variants_is_unchanged(capsys):
    assert replay_script.run_replay(_settings(), rows_loader=_loader(skip_rows())) == 0
    assert "strategy variants" not in capsys.readouterr().out


def test_replay_script_variants_json_and_invalid_variant(capsys):
    code = replay_script.run_replay(
        _settings(), json_output=True, variants=f"{STRICT},{NO_BROAD_BLOCK}", rows_loader=_loader(skip_rows())
    )
    doc = json.loads(capsys.readouterr().out)
    assert code == 0
    assert doc["strategy_variants"]["variants"] == ["current_v2", STRICT, NO_BROAD_BLOCK]
    assert doc["live_signal_logic_unchanged"] is True
    assert replay_script.run_replay(_settings(), variants="yolo", rows_loader=_loader([])) == 2


def test_replay_script_variants_do_not_write_to_db(tmp_path, capsys):
    url = f"sqlite:///{tmp_path / 'shadow.db'}"
    engine = create_engine(url)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    _store(session, combined_rows())
    before = _dump(session)
    session.close()
    assert replay_script.run_replay(_settings(database_url=url), variants="all") == 0
    capsys.readouterr()
    check = sessionmaker(bind=create_engine(url))()
    assert _dump(check) == before
    check.close()


def test_replay_script_main_variants_arg(monkeypatch, capsys):
    monkeypatch.setattr(replay_script, "load_settings", lambda **_kw: _settings())
    monkeypatch.setattr(replay_script, "_default_rows_loader", _loader(combined_rows()))
    assert replay_script.main(["--run-id", f"{OPEN_RUN},{MIDDAY_RUN}", "--variants", "all", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["strategy_variants"]["rows_evaluated"] == 14


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


@pytest.mark.parametrize("module", [skip_mod, variants_mod, skip_script, replay_script])
def test_diagnostics_have_no_order_imports_or_execution_calls(module):
    source = inspect.getsource(module)
    for token in _FORBIDDEN:
        assert token not in source, f"{module.__name__} references {token}"


@pytest.mark.parametrize("module", [skip_mod, variants_mod, skip_script, replay_script])
def test_diagnostics_never_write_db_env_or_files(module):
    source = inspect.getsource(module)
    for token in (".commit(", ".add(", "update_followup", "log_signal", "os.environ", "setenv", "create_table=True", "open("):
        assert token not in source, f"{module.__name__}: {token}"


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


def test_live_engine_still_blocks_broad_disagreement():
    decision = TnaTzaSignalEngine(wall_clock=lambda: NOW).decide(
        MarketSnapshot(quotes={x.symbol: x for x in divergence_bull()}, created_at=NOW)
    )
    assert decision.decision is SignalDirection.SKIP
    assert decision.skip_reason is SignalReason.BROAD_MARKET_DISAGREEMENT
