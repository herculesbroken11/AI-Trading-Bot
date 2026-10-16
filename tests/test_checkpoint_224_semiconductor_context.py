"""Checkpoint 2.24 — SOXL/SOXS context is analytics only and cannot change a decision."""

from __future__ import annotations

import inspect

import backend.execution.execution_router as execution_router
import backend.execution.order_executor as order_executor
import backend.signals.candidate_engine_v3 as v3_mod
import backend.signals.tna_tza_signal_engine as v2_mod
from backend.shadow_mode.runner import ShadowRunSummary, production_execution_block_checks
from backend.shadow_mode.semiconductor_context import (
    context_from_row,
    evaluate_semiconductor_context,
    shadow_stream_symbols,
    summarize_semiconductor,
)
from backend.signals.candidate_engine_v3 import CandidateEngineV3
from backend.signals.models import REQUIRED_SIGNAL_SYMBOLS, MarketSnapshot, SymbolQuote
from backend.signals.tna_tza_signal_engine import TnaTzaSignalEngine
from tests.test_checkpoint_217a_balanced_shadow_profile import q, snap, window_bull
from tests.test_checkpoint_218_stream_readiness import _FORBIDDEN


def _semi(soxl_first: float, soxl_mid: float, soxs_first: float, soxs_mid: float, *, age: float = 0.2):
    return [
        q("SOXL", soxl_mid, first=soxl_first, age=age),
        q("SOXS", soxs_mid, first=soxs_first, age=age),
    ]


def _bullish_snapshot(*extra: SymbolQuote) -> MarketSnapshot:
    return snap(*window_bull(), *extra)


def test_semiconductor_quotes_do_not_change_the_tna_decision():
    engine = CandidateEngineV3(mode="shadow")
    plain = engine.decide(_bullish_snapshot())
    opposed = engine.decide(_bullish_snapshot(*_semi(40.0, 39.0, 20.0, 21.0)))
    assert plain.decision == opposed.decision
    assert plain.selected_symbol == opposed.selected_symbol == "TNA"
    assert plain.confidence_score == opposed.confidence_score
    assert plain.bullish_score == opposed.bullish_score
    assert plain.bearish_score == opposed.bearish_score
    assert plain.explanation == opposed.explanation
    assert "SOXL" not in inspect.getsource(v3_mod)
    assert "SOXS" not in inspect.getsource(v3_mod)


def test_missing_semiconductor_data_does_not_block_the_strategy():
    engine = CandidateEngineV3(mode="shadow")
    plain = _bullish_snapshot()
    stale_snapshot = _bullish_snapshot(*_semi(40.0, 41.0, 20.0, 19.0, age=6.0))
    missing = engine.decide(plain)
    stale = engine.decide(stale_snapshot)
    assert missing.decision == stale.decision
    assert missing.selected_symbol == "TNA"
    assert missing.freshness_gate_passed is True
    context = evaluate_semiconductor_context(stale_snapshot, decision="bullish", selected_symbol="TNA")
    assert context["semiconductor_direction"] == "UNAVAILABLE"
    assert context["semiconductor_agreement"] == "UNAVAILABLE"
    empty = evaluate_semiconductor_context(plain, decision="bullish", selected_symbol="TNA")
    assert empty["semiconductor_direction"] == "UNAVAILABLE"


def test_agreement_follows_the_candidate_direction_only_as_a_label():
    bullish = _bullish_snapshot()
    agree = evaluate_semiconductor_context(
        snap(*window_bull(), *_semi(40.0, 41.0, 20.0, 19.0)),
        decision="bullish",
        selected_symbol="TNA",
    )
    disagree = evaluate_semiconductor_context(
        snap(*window_bull(), *_semi(40.0, 39.0, 20.0, 21.0)),
        decision="bullish",
        selected_symbol="TNA",
    )
    bear_agree = evaluate_semiconductor_context(
        snap(*window_bull(), *_semi(40.0, 39.0, 20.0, 21.0)),
        decision="bearish",
        selected_symbol="TZA",
    )
    bear_disagree = evaluate_semiconductor_context(
        snap(*window_bull(), *_semi(40.0, 41.0, 20.0, 19.0)),
        decision="bearish",
        selected_symbol="TZA",
    )
    mixed = evaluate_semiconductor_context(
        snap(*window_bull(), *_semi(40.0, 41.0, 20.0, 21.0)),
        decision="bullish",
        selected_symbol="TNA",
    )
    assert agree["semiconductor_direction"] == "BULLISH"
    assert agree["semiconductor_agreement"] == "AGREE"
    assert disagree["semiconductor_agreement"] == "DISAGREE"
    assert bear_agree["semiconductor_agreement"] == "AGREE"
    assert bear_disagree["semiconductor_agreement"] == "DISAGREE"
    assert mixed["semiconductor_direction"] == "MIXED"
    assert mixed["semiconductor_agreement"] == "MIXED"
    assert agree["semiconductor_pair_spread_pct"] == round(agree["soxl_window_move_pct"] - agree["soxs_window_move_pct"], 4)
    skip = evaluate_semiconductor_context(bullish, decision="skip", selected_symbol=None)
    assert skip["semiconductor_agreement"] is None


def test_historical_rows_without_semiconductor_fields_stay_unavailable():
    row = {
        "decision": "bullish",
        "selected_symbol": "TNA",
        "raw_snapshot_json": snap(*window_bull()).to_dict(),
        "direction_was_correct": True,
    }
    context = context_from_row(row)
    assert context["semiconductor_direction"] == "UNAVAILABLE"
    assert context["soxl_window_move_pct"] is None
    assert context["soxs_window_move_pct"] is None
    summary = summarize_semiconductor(
        [{"semiconductor_context": context, "direction_correct_60s": True}]
    )
    assert summary["UNAVAILABLE"]["count"] == 1
    assert summary["strategy_impact"] == "none"
    assert summary["orders_submitted"] == 0


def test_disagreement_block_is_counterfactual_only():
    qqq = q("QQQ", 101.0, first=100.0)
    agree = evaluate_semiconductor_context(
        snap(qqq, *_semi(40.0, 41.0, 20.0, 19.0)),
        decision="bullish",
        selected_symbol="TNA",
    )
    disagree = evaluate_semiconductor_context(
        snap(qqq, *_semi(40.0, 39.0, 20.0, 21.0)),
        decision="bullish",
        selected_symbol="TNA",
    )
    summary = summarize_semiconductor(
        [
            {"semiconductor_context": agree, "direction_correct_60s": True},
            {"semiconductor_context": disagree, "direction_correct_60s": False},
        ]
    )
    blocked = summary["if_semiconductor_disagreement_had_blocked_candidate"]
    assert summary["winners_with_semiconductor_agreement"] == 1
    assert summary["losers_with_semiconductor_disagreement"] == 1
    assert blocked["winners_removed"] == 0
    assert blocked["losers_removed"] == 1
    assert blocked["candidate_retention_pct"] == 50.0
    assert summary["qqq_overlap"]["soxl_vs_qqq_agreement_count"] == 1
    assert summary["qqq_overlap"]["soxs_inverse_vs_qqq_agreement_count"] == 1


def test_semiconductor_analytics_are_not_an_execution_path():
    import backend.shadow_mode.semiconductor_context as context_mod

    source = inspect.getsource(context_mod)
    for token in _FORBIDDEN:
        assert token not in source
    for module in (order_executor, execution_router):
        text = inspect.getsource(module)
        assert "semiconductor_context" not in text
        assert "SOXL" not in text
    assert "SOXL" not in inspect.getsource(v2_mod)
    engine = TnaTzaSignalEngine()
    assert engine.config.entry_score_threshold == 70
    assert "SOXL" not in REQUIRED_SIGNAL_SYMBOLS
    symbols = shadow_stream_symbols(("TNA", "TZA", "IWM", "SPY", "QQQ", "VIX"))
    assert symbols[:6] == ["TNA", "TZA", "IWM", "SPY", "QQQ", "VIX"]
    assert symbols[-2:] == ["SOXL", "SOXS"]
    checks = production_execution_block_checks()
    assert all(checks.values())
    assert ShadowRunSummary(run_id="shadow-224", cycles_requested=1).orders_submitted == 0
