"""
Replay comparison for the predictive continuation layer (Checkpoint 2.20).

Reads stored shadow rows in memory. Existing TNA/TZA candidates stay as they
were logged. The layer only marks which of those candidates it would keep.
Nothing is written back and no order is submitted.

orders_submitted: 0
writes_to_database: false
analytics_only: true
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping, Optional, Sequence

from backend.shadow_mode.analytics import score_hypothetical, score_strict_direction
from backend.shadow_mode.profile_replay import _ordered_rows, _remember_direction, replay_profile_row
from backend.signals.models import MarketSnapshot
from backend.signals.predictive_continuation import (
    ANALYTICS_ONLY,
    ORDERS_SUBMITTED,
    WRITES_TO_DATABASE,
    continuation_entry_block,
    evaluate_predictive_continuation,
)
from backend.signals.profiles import BALANCED_V3_SHADOW

TRADE_DECISIONS = frozenset({"bullish", "bearish"})
PREDICTIVE_NOTE = (
    "hypothetical predictive continuation filter on stored TNA/TZA candidates — "
    "analytics only, no orders, not written to the database"
)


def _json(raw: Any) -> Dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _snapshot(row: Mapping[str, Any]) -> Optional[MarketSnapshot]:
    payload = _json(row.get("raw_snapshot_json"))
    if not payload.get("quotes"):
        return None
    try:
        return MarketSnapshot.from_dict(payload)
    except (KeyError, TypeError, ValueError):
        return None


def _candidate_symbol(row: Mapping[str, Any]) -> Optional[str]:
    decision = str(row.get("decision") or "").lower()
    if decision not in TRADE_DECISIONS:
        return None
    symbol = str(row.get("selected_symbol") or "").upper()
    if symbol in {"TNA", "TZA"}:
        return symbol
    if decision == "bullish":
        return "TNA"
    if decision == "bearish":
        return "TZA"
    return None


def _accuracy(flags: Sequence[Optional[bool]]) -> Optional[float]:
    scored = [flag for flag in flags if flag is not None]
    if not scored:
        return None
    return round(sum(1 for flag in scored if flag) / len(scored) * 100.0, 1)


def _empty_report(note: str, *, source: str) -> Dict[str, Any]:
    return {
        "candidate_source": source,
        "candidate_count": 0,
        "correct_candidates": 0,
        "incorrect_candidates": 0,
        "passed_predictions": 0,
        "failed_predictions": 0,
        "accuracy_before_prediction": None,
        "accuracy_after_prediction": None,
        "false_signals_removed": 0,
        "good_signals_removed": 0,
        "missed_winners": 0,
        "orders_submitted": ORDERS_SUBMITTED,
        "writes_to_database": WRITES_TO_DATABASE,
        "analytics_only": ANALYTICS_ONLY,
        "note": note,
    }


def _accumulate(
    *,
    decision: str,
    symbol: str,
    correct: Optional[bool],
    meaningful: Optional[bool],
    passed: bool,
    totals: Dict[str, Any],
    before_flags: List[Optional[bool]],
    after_flags: List[Optional[bool]],
) -> None:
    del symbol
    totals["candidate_count"] += 1
    if correct is True:
        totals["correct_candidates"] += 1
    elif correct is False:
        totals["incorrect_candidates"] += 1
    before_flags.append(correct)
    if passed:
        totals["passed_predictions"] += 1
        after_flags.append(correct)
        return
    totals["failed_predictions"] += 1
    if correct is False:
        totals["false_signals_removed"] += 1
    if correct is True:
        totals["good_signals_removed"] += 1
    if meaningful is True:
        totals["missed_winners"] += 1


def _finalize(
    totals: Dict[str, Any],
    before_flags: Sequence[Optional[bool]],
    after_flags: Sequence[Optional[bool]],
    *,
    source: str,
    note: str,
) -> Dict[str, Any]:
    totals["candidate_source"] = source
    totals["accuracy_before_prediction"] = _accuracy(before_flags)
    totals["accuracy_after_prediction"] = _accuracy(after_flags)
    totals["orders_submitted"] = ORDERS_SUBMITTED
    totals["writes_to_database"] = WRITES_TO_DATABASE
    totals["analytics_only"] = ANALYTICS_ONLY
    totals["note"] = note
    return totals


def _compare_balanced_candidates(rows: Sequence[Mapping[str, Any]], *, min_move_pct: float) -> Dict[str, Any]:
    """Before: balanced_v3_shadow candidates. After: the ones continuation would keep."""
    note = (
        "hypothetical continuation filter on balanced_v3_shadow candidates — "
        "analytics only, no orders, not written to the database"
    )
    totals: Dict[str, Any] = _empty_report(note, source=BALANCED_V3_SHADOW)
    before_flags: List[Optional[bool]] = []
    after_flags: List[Optional[bool]] = []
    previous_by_run: Dict[str, Optional[str]] = {}
    last_taken_by_run: Dict[str, Optional[str]] = {}
    for row in _ordered_rows(rows):
        run_id = str(row.get("run_id") or "")
        before = replay_profile_row(
            row,
            BALANCED_V3_SHADOW,
            previous_direction=previous_by_run.get(run_id),
            last_taken_direction=last_taken_by_run.get(run_id),
            apply_continuation_filter=False,
        )
        decision = str(before.get("decision") or "skip") if before.get("replayed") else "skip"
        _remember_direction(previous_by_run, last_taken_by_run, run_id, decision)
        symbol = before.get("selected_symbol")
        if decision not in TRADE_DECISIONS or symbol not in {"TNA", "TZA"}:
            continue
        correct = score_strict_direction(decision, row)
        meaningful = score_hypothetical(decision, row, min_move_pct=min_move_pct)
        snapshot = _snapshot(row)
        result = evaluate_predictive_continuation(snapshot, symbol) if snapshot is not None else None
        vix_penalty = (before.get("profile_scores") or {}).get("vix_diagnostic") == "vix_penalty_applied"
        passed = continuation_entry_block(result, vix_penalty=vix_penalty) is None
        _accumulate(
            decision=decision,
            symbol=str(symbol),
            correct=correct,
            meaningful=meaningful,
            passed=passed,
            totals=totals,
            before_flags=before_flags,
            after_flags=after_flags,
        )
    return _finalize(totals, before_flags, after_flags, source=BALANCED_V3_SHADOW, note=note)


def compare_predictive_continuation(
    rows: Sequence[Mapping[str, Any]],
    *,
    min_move_pct: float,
    profile: Optional[str] = None,
) -> Dict[str, Any]:
    """Before: existing candidates. After: candidates continuation would still take."""
    if profile == BALANCED_V3_SHADOW:
        return _compare_balanced_candidates(rows, min_move_pct=min_move_pct)
    before_flags: List[Optional[bool]] = []
    after_flags: List[Optional[bool]] = []
    candidate_count = 0
    correct_candidates = 0
    incorrect_candidates = 0
    passed_predictions = 0
    failed_predictions = 0
    false_signals_removed = 0
    good_signals_removed = 0
    missed_winners = 0

    for row in rows:
        symbol = _candidate_symbol(row)
        if symbol is None:
            continue
        candidate_count += 1
        decision = str(row.get("decision") or "").lower()
        correct = score_strict_direction(decision, row)
        meaningful = score_hypothetical(decision, row, min_move_pct=min_move_pct)
        if correct is True:
            correct_candidates += 1
        elif correct is False:
            incorrect_candidates += 1
        before_flags.append(correct)

        snapshot = _snapshot(row)
        result = evaluate_predictive_continuation(snapshot, symbol) if snapshot is not None else None
        passed = bool(result and result.prediction_passed)
        if passed:
            passed_predictions += 1
            after_flags.append(correct)
        else:
            failed_predictions += 1
            if correct is False:
                false_signals_removed += 1
            if correct is True:
                good_signals_removed += 1
            if meaningful is True:
                missed_winners += 1

    totals = {
        "candidate_count": candidate_count,
        "correct_candidates": correct_candidates,
        "incorrect_candidates": incorrect_candidates,
        "passed_predictions": passed_predictions,
        "failed_predictions": failed_predictions,
        "false_signals_removed": false_signals_removed,
        "good_signals_removed": good_signals_removed,
        "missed_winners": missed_winners,
    }
    return _finalize(totals, before_flags, after_flags, source="stored_decisions", note=PREDICTIVE_NOTE)
