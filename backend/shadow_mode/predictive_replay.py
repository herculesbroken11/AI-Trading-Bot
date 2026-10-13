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
from backend.signals.models import MarketSnapshot
from backend.signals.predictive_continuation import (
    ANALYTICS_ONLY,
    ORDERS_SUBMITTED,
    WRITES_TO_DATABASE,
    evaluate_predictive_continuation,
)

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


def compare_predictive_continuation(
    rows: Sequence[Mapping[str, Any]],
    *,
    min_move_pct: float,
) -> Dict[str, Any]:
    """Before: stored candidates. After: the same candidates that pass continuation."""
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

    return {
        "candidate_count": candidate_count,
        "correct_candidates": correct_candidates,
        "incorrect_candidates": incorrect_candidates,
        "passed_predictions": passed_predictions,
        "failed_predictions": failed_predictions,
        "accuracy_before_prediction": _accuracy(before_flags),
        "accuracy_after_prediction": _accuracy(after_flags),
        "false_signals_removed": false_signals_removed,
        "good_signals_removed": good_signals_removed,
        "missed_winners": missed_winners,
        "orders_submitted": ORDERS_SUBMITTED,
        "writes_to_database": WRITES_TO_DATABASE,
        "analytics_only": ANALYTICS_ONLY,
        "note": PREDICTIVE_NOTE,
    }
