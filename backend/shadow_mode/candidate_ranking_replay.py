"""
Replay comparison for candidate quality ranking (Checkpoint 2.21).

Reads stored shadow rows in memory. balanced_v3_shadow candidates stay as the
engine decided them. Ranking is an experimental label on those candidates.
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
from backend.signals.candidate_quality_ranker import (
    ANALYTICS_ONLY,
    ORDERS_SUBMITTED,
    WRITES_TO_DATABASE,
    rank_candidate,
)
from backend.signals.models import MarketSnapshot
from backend.signals.profiles import BALANCED_V3_SHADOW

TRADE_DECISIONS = frozenset({"bullish", "bearish"})
RANKING_NOTE = (
    "hypothetical quality ranking of balanced_v3_shadow candidates — "
    "analytics only, no orders, not written to the database"
)
SEPARATION_GAP = 8.0
_EXAMPLE_LIMIT = 3


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


def _accuracy(correct: int, incorrect: int) -> Optional[float]:
    scored = correct + incorrect
    if scored == 0:
        return None
    return round(correct / scored * 100.0, 1)


def _mean(values: Sequence[float]) -> Optional[float]:
    if not values:
        return None
    return round(sum(values) / len(values), 1)


def _what_if(correct: Optional[bool]) -> str:
    if correct is True:
        return "would_have_been_correct"
    if correct is False:
        return "would_have_been_incorrect"
    return "unscored"


def _tier_bucket() -> Dict[str, Any]:
    return {"count": 0, "correct": 0, "incorrect": 0, "accuracy": None, "hypothetical_correct": 0, "hypothetical_incorrect": 0}


def _empty_summary(run_id: Optional[str]) -> Dict[str, Any]:
    return {
        "run_id": run_id,
        "candidate_source": BALANCED_V3_SHADOW,
        "candidate_count": 0,
        "tier_a_count": 0,
        "tier_b_count": 0,
        "tier_c_count": 0,
        "rejected_count": 0,
        "accepted_ranked_candidates": 0,
        "rejected_ranked_candidates": 0,
        "correct_before_ranking": 0,
        "incorrect_before_ranking": 0,
        "accuracy_before_ranking": None,
        "correct_after_ranking": 0,
        "incorrect_after_ranking": 0,
        "accuracy_after_ranking": None,
        "false_signals_removed": 0,
        "good_signals_removed": 0,
        "false_signals_kept": 0,
        "good_signals_preserved": 0,
        "missed_winners": 0,
        "precision_by_tier": {"A": _tier_bucket(), "B": _tier_bucket(), "C": _tier_bucket()},
        "average_quality_score_correct": None,
        "average_quality_score_incorrect": None,
        "quality_separation": "unavailable",
        "candidate_retention_pct": None,
        "labeled_candidates": 0,
        "orders_submitted": ORDERS_SUBMITTED,
        "writes_to_database": WRITES_TO_DATABASE,
        "analytics_only": ANALYTICS_ONLY,
    }


def _collect(rows: Sequence[Mapping[str, Any]], *, min_move_pct: float) -> List[Dict[str, Any]]:
    """Engine candidates, then an experimental rank. The engine decision is not changed."""
    items: List[Dict[str, Any]] = []
    previous_by_run: Dict[str, Optional[str]] = {}
    last_taken_by_run: Dict[str, Optional[str]] = {}
    for row in _ordered_rows(rows):
        run_id = str(row.get("run_id") or "")
        replayed = replay_profile_row(
            row,
            BALANCED_V3_SHADOW,
            previous_direction=previous_by_run.get(run_id),
            last_taken_direction=last_taken_by_run.get(run_id),
            apply_continuation_filter=False,
        )
        decision = str(replayed.get("decision") or "skip") if replayed.get("replayed") else "skip"
        _remember_direction(previous_by_run, last_taken_by_run, run_id, decision)
        symbol = replayed.get("selected_symbol")
        if decision not in TRADE_DECISIONS or symbol not in {"TNA", "TZA"}:
            continue
        snapshot = _snapshot(row)
        if snapshot is None:
            continue
        score = (replayed.get("profile_scores") or {}).get("candidate_score", replayed.get("candidate_score"))
        ranked = rank_candidate(snapshot, str(symbol), candidate_score=None if score is None else float(score))
        if ranked is None:
            continue
        correct = score_strict_direction(decision, row)
        meaningful = score_hypothetical(decision, row, min_move_pct=min_move_pct)
        accepted = bool(ranked.rank_passed)
        record = {
            "run_id": run_id,
            "cycle_number": row.get("cycle_number"),
            "decision": decision,
            "selected_symbol": symbol,
            "candidate_score": score,
            "accepted": accepted,
            "correct": correct,
            "meaningful": meaningful,
            "actual_followup_correct": correct,
            "what_if_result": None if accepted else _what_if(correct),
        }
        record.update(ranked.to_dict())
        items.append(record)
    return items


def _summarize(items: Sequence[Mapping[str, Any]], *, run_id: Optional[str]) -> Dict[str, Any]:
    summary = _empty_summary(run_id)
    correct_scores: List[float] = []
    incorrect_scores: List[float] = []
    examples: Dict[str, List[Dict[str, Any]]] = {
        "false_signal_correctly_rejected": [],
        "winner_incorrectly_rejected": [],
        "loser_incorrectly_accepted": [],
        "winner_correctly_accepted": [],
    }
    tier_counts = {"A": 0, "B": 0, "C": 0, "REJECT": 0}
    for item in items:
        summary["candidate_count"] += 1
        tier = str(item.get("quality_tier") or "C")
        tier_counts[tier] = tier_counts.get(tier, 0) + 1
        accepted = bool(item.get("accepted"))
        correct = item.get("correct")
        if accepted:
            summary["accepted_ranked_candidates"] += 1
        else:
            summary["rejected_ranked_candidates"] += 1
        if correct is True:
            summary["correct_before_ranking"] += 1
            correct_scores.append(float(item["quality_score"]))
            summary["labeled_candidates"] += 1
        elif correct is False:
            summary["incorrect_before_ranking"] += 1
            incorrect_scores.append(float(item["quality_score"]))
            summary["labeled_candidates"] += 1
        if accepted and correct is True:
            summary["correct_after_ranking"] += 1
            summary["good_signals_preserved"] += 1
        elif accepted and correct is False:
            summary["incorrect_after_ranking"] += 1
            summary["false_signals_kept"] += 1
        elif not accepted and correct is False:
            summary["false_signals_removed"] += 1
        elif not accepted and correct is True:
            summary["good_signals_removed"] += 1
        if not accepted and item.get("meaningful") is True:
            summary["missed_winners"] += 1
        bucket = summary["precision_by_tier"].get(tier)
        if bucket is not None:
            bucket["count"] += 1
            if correct is True:
                bucket["correct"] += 1
                bucket["hypothetical_correct"] += 1
            elif correct is False:
                bucket["incorrect"] += 1
                bucket["hypothetical_incorrect"] += 1
        kind = None
        if not accepted and correct is False:
            kind = "false_signal_correctly_rejected"
        elif not accepted and correct is True:
            kind = "winner_incorrectly_rejected"
        elif accepted and correct is False:
            kind = "loser_incorrectly_accepted"
        elif accepted and correct is True:
            kind = "winner_correctly_accepted"
        if kind is not None and len(examples[kind]) < _EXAMPLE_LIMIT:
            examples[kind].append(dict(item))
    summary["tier_a_count"] = tier_counts.get("A", 0)
    summary["tier_b_count"] = tier_counts.get("B", 0)
    summary["tier_c_count"] = tier_counts.get("C", 0)
    summary["rejected_count"] = tier_counts.get("REJECT", 0)
    summary["accuracy_before_ranking"] = _accuracy(summary["correct_before_ranking"], summary["incorrect_before_ranking"])
    summary["accuracy_after_ranking"] = _accuracy(summary["correct_after_ranking"], summary["incorrect_after_ranking"])
    for bucket in summary["precision_by_tier"].values():
        bucket["accuracy"] = _accuracy(bucket["correct"], bucket["incorrect"])
    summary["average_quality_score_correct"] = _mean(correct_scores)
    summary["average_quality_score_incorrect"] = _mean(incorrect_scores)
    correct_avg = summary["average_quality_score_correct"]
    incorrect_avg = summary["average_quality_score_incorrect"]
    if correct_avg is not None and incorrect_avg is not None:
        summary["quality_separation"] = "weak" if incorrect_avg + SEPARATION_GAP >= correct_avg else "distinct"
    if summary["candidate_count"]:
        summary["candidate_retention_pct"] = round(
            summary["accepted_ranked_candidates"] / summary["candidate_count"] * 100.0,
            1,
        )
    summary["examples"] = examples
    summary["candidates"] = [dict(item) for item in items]
    return summary


def _generalization(per_run: Sequence[Mapping[str, Any]]) -> str:
    improved = 0
    worsened = 0
    compared = 0
    for report in per_run:
        before = report.get("accuracy_before_ranking")
        after = report.get("accuracy_after_ranking")
        if before is None or after is None:
            continue
        compared += 1
        if after > before:
            improved += 1
        elif after < before:
            worsened += 1
    if improved and worsened:
        return "inconsistent"
    if improved >= 2 and worsened == 0:
        return "promising"
    return "insufficient_runs"


def compare_candidate_ranking(
    rows: Sequence[Mapping[str, Any]],
    *,
    min_move_pct: float,
    minimum_evaluation_candidates: int = 20,
) -> Dict[str, Any]:
    """Per-run and combined experimental ranks. Does not change engine decisions."""
    if minimum_evaluation_candidates < 1:
        raise ValueError("minimum_evaluation_candidates must be >= 1")
    items = _collect(rows, min_move_pct=min_move_pct)
    by_run: Dict[str, List[Dict[str, Any]]] = {}
    for item in items:
        by_run.setdefault(str(item.get("run_id") or ""), []).append(item)
    per_run = [_summarize(group, run_id=run_id) for run_id, group in by_run.items()]
    combined = _summarize(items, run_id=None)
    labeled = int(combined["labeled_candidates"])
    return {
        "candidate_source": BALANCED_V3_SHADOW,
        "per_run": per_run,
        "combined": combined,
        "total_candidates": combined["candidate_count"],
        "total_accepted": combined["accepted_ranked_candidates"],
        "overall_accuracy_before": combined["accuracy_before_ranking"],
        "overall_accuracy_after": combined["accuracy_after_ranking"],
        "false_signals_removed": combined["false_signals_removed"],
        "good_signals_removed": combined["good_signals_removed"],
        "candidate_retention_pct": combined["candidate_retention_pct"],
        "generalization_status": _generalization(per_run),
        "minimum_evaluation_candidates": minimum_evaluation_candidates,
        "sample_size_status": "insufficient" if labeled < minimum_evaluation_candidates else "adequate",
        "labeled_candidates": labeled,
        "orders_submitted": ORDERS_SUBMITTED,
        "writes_to_database": WRITES_TO_DATABASE,
        "analytics_only": ANALYTICS_ONLY,
        "note": RANKING_NOTE,
    }
