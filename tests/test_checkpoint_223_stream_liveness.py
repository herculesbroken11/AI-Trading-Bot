"""Checkpoint 2.23 — feed liveness is separate from a quiet quote. No trading changes."""

from __future__ import annotations

import inspect

from backend.shadow_mode.models import compute_followup_outcome
from backend.shadow_mode.report import summarize_shadow_logs
from backend.shadow_mode.runner import ShadowRunSummary, production_execution_block_checks
from backend.shadow_mode.stream_readiness import StreamHealthState, assess_stream_health, summarize_liveness
from backend.signals.candidate_engine_v3 import CandidateEngineV3
from backend.signals.models import MarketSnapshot, SymbolQuote
from backend.signals.tna_tza_signal_engine import TnaTzaSignalEngine
from tests.test_checkpoint_217a_balanced_shadow_profile import snap, window_bull
from tests.test_checkpoint_218_stream_readiness import _FORBIDDEN

_FRESH = {"IWM": 0.2, "SPY": 0.2, "QQQ": 0.2, "TNA": 0.2, "TZA": 0.2}


def _quote(symbol: str, mid: float, age: float, updates: int = 4) -> SymbolQuote:
    return SymbolQuote(symbol, bid=mid - 0.01, ask=mid + 0.01, quote_age_seconds=age, quote_updates=updates)


def test_quiet_tza_stays_evaluable_while_indexes_update():
    health = assess_stream_health({**_FRESH, "TZA": 1.5})
    assert health.state == StreamHealthState.QUIET_SYMBOL
    assert health.quiet_symbols == ("TZA",)
    assert health.evaluable is True
    assert health.cycle_blocked is False
    assert health.feed_reconnect_reason is None


def test_one_etf_at_2_5_seconds_is_not_stalled():
    default = assess_stream_health({**_FRESH, "TZA": 2.5})
    tighter = assess_stream_health({**_FRESH, "TZA": 2.5}, policies={"TZA": (2.0, 5.0)})
    assert default.state in (StreamHealthState.QUIET_SYMBOL, StreamHealthState.DEGRADED)
    assert tighter.state == StreamHealthState.DEGRADED
    assert default.state != StreamHealthState.STALLED
    assert tighter.feed_reconnect_reason is None
    assert tighter.evaluable is True


def test_required_symbol_past_five_seconds_blocks_the_cycle():
    health = assess_stream_health({**_FRESH, "IWM": 5.4})
    assert "IWM" in health.hard_stale_symbols
    assert health.cycle_blocked is True
    assert health.evaluable is False
    assert health.state != StreamHealthState.STALLED


def test_three_required_symbols_past_five_seconds_stall_and_reconnect():
    health = assess_stream_health({"TNA": 6.0, "TZA": 6.2, "IWM": 5.5, "SPY": 0.2, "QQQ": 0.2})
    assert health.state == StreamHealthState.STALLED
    assert health.feed_reconnect_reason == "feed_stalled"
    assert health.cycle_blocked is True


def test_disconnected_socket_is_stalled():
    health = assess_stream_health(_FRESH, connected=False)
    assert health.state == StreamHealthState.STALLED
    assert health.connected is False
    assert health.feed_reconnect_reason == "websocket_disconnected"


def test_unchanged_price_with_live_events_is_a_valid_zero_move():
    after = MarketSnapshot(
        quotes={
            "TNA": _quote("TNA", 45.0, 0.2),
            "TZA": _quote("TZA", 10.0, 0.2),
            "IWM": _quote("IWM", 220.0, 0.3),
            "SPY": _quote("SPY", 580.0, 0.2),
            "QQQ": _quote("QQQ", 500.0, 0.2),
        }
    )
    outcome = compute_followup_outcome(
        decision="bullish",
        selected_symbol="TNA",
        mids_before={"TNA": 45.0, "IWM": 220.0},
        after=after,
        followup_seconds=60.0,
    )
    assert outcome.selected_symbol_move_pct == 0.0
    assert outcome.iwm_move_pct == 0.0
    assert outcome.direction_was_correct is False
    assert "unscored_due_to_stale_followup" not in outcome.outcome_note


def test_no_market_events_invalidate_the_followup():
    outcome = compute_followup_outcome(
        decision="bullish",
        selected_symbol="TNA",
        mids_before={"TNA": 45.0, "IWM": 220.0},
        after=MarketSnapshot(quotes={}),
        followup_seconds=60.0,
    )
    assert outcome.direction_was_correct is None
    assert "unscored_due_to_stale_followup" in outcome.outcome_note


def test_balanced_v3_shadow_decision_is_unchanged():
    import backend.signals.candidate_engine_v3 as engine_mod

    decision = CandidateEngineV3(mode="shadow").decide(snap(*window_bull()))
    assert decision.decision.value == "bullish"
    assert decision.selected_symbol == "TNA"
    source = inspect.getsource(engine_mod)
    assert "assess_stream_health" not in source
    assert "StreamHealthState" not in source


def test_conservative_v2_is_unchanged():
    import backend.signals.tna_tza_signal_engine as engine_mod

    engine = TnaTzaSignalEngine()
    assert engine.config.entry_score_threshold == 70
    source = inspect.getsource(engine_mod)
    assert "assess_stream_health" not in source
    assert "balanced_v3_shadow" not in source


def test_liveness_modules_do_not_import_execution():
    import backend.shadow_mode.stream_readiness as readiness
    import backend.shadow_mode.report as report

    for module in (readiness, report):
        source = inspect.getsource(module)
        for token in _FORBIDDEN:
            assert token not in source


def test_production_execution_stays_blocked_and_orders_stay_zero():
    checks = production_execution_block_checks()
    assert all(checks.values())
    summary = ShadowRunSummary(run_id="shadow-223", cycles_requested=1)
    assert summary.orders_submitted == 0
    rows = [{"decision": "skip", "freshness_gate_passed": True, **{f"quote_age_{s.lower()}": 0.2 for s in _FRESH}}]
    report = summarize_shadow_logs(rows)
    assert report["feed_health"]["orders_submitted"] == 0
    liveness = summarize_liveness(rows)
    assert liveness["writes_to_database"] is False
    assert liveness["new_liveness_valid_for_evaluation"] is True


def test_october_15_quiet_cycles_are_not_a_stalled_feed():
    """Diagnostic only. These ages are the logged stream_not_ready cycles; rows are not rewritten."""
    logged = [
        {"TNA": 0.669, "TZA": 1.667, "IWM": 0.364, "SPY": 0.264, "QQQ": 0.364},
        {"TNA": 1.916, "TZA": 1.916, "IWM": 0.157, "SPY": 0.057, "QQQ": 0.157},
        {"TNA": 0.322, "TZA": 2.825, "IWM": 0.023, "SPY": 0.423, "QQQ": 0.023},
        {"TNA": 0.043, "TZA": 0.543, "IWM": 1.213, "SPY": 1.114, "QQQ": 0.218},
        {"TNA": 0.616, "TZA": 1.107, "IWM": 0.267, "SPY": 0.168, "QQQ": 0.168},
        {"TNA": 0.235, "TZA": 1.234, "IWM": 0.435, "SPY": 0.335, "QQQ": 0.335},
        {"TNA": 0.143, "TZA": 0.043, "IWM": 0.319, "SPY": 1.219, "QQQ": 0.319},
    ]
    rows = []
    for ages in logged:
        row = {"freshness_gate_passed": False, "skip_reason": "stream_not_ready"}
        row.update({f"quote_age_{symbol.lower()}": age for symbol, age in ages.items()})
        rows.append(row)
        health = assess_stream_health(ages)
        assert health.state == StreamHealthState.QUIET_SYMBOL
        assert health.evaluable is True
    for _ in range(23):
        row = {"freshness_gate_passed": True, "skip_reason": "insufficient_signal_strength"}
        row.update({f"quote_age_{symbol.lower()}": 0.3 for symbol in _FRESH})
        rows.append(row)
    liveness = summarize_liveness(rows)
    legacy = summarize_shadow_logs(rows)
    assert legacy["freshness_gate_pass_pct"] == 76.7
    assert legacy["stream_not_ready_count"] == 7
    assert legacy["valid_for_signal_evaluation"] is False
    assert liveness["quiet_symbol_cycles"] == 7
    assert liveness["hard_stale_cycles"] == 0
    assert liveness["feed_stalled_cycles"] == 0
    assert liveness["new_liveness_valid_for_evaluation"] is True
