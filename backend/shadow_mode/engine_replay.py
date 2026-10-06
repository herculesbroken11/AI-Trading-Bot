"""
Offline Signal Engine version comparison (Checkpoint 2.15) — analytics only.

Replays stored shadow_signal_log snapshots through the current Signal Engine
(v2 quality gates) and compares the result with the decision that was logged at
the time. Nothing is written back to the database, no setting is changed and
this module has no broker / order / execution access.

Old outcome: the stored direction_was_correct (falls back to the hypothetical
scoring below when the stored value is missing). New outcome: the same
deterministic follow-up scoring used by the 2.13 threshold simulation.

The expected-move tag is a diagnostic only: it never changes a decision.
"""

from __future__ import annotations

import json
from dataclasses import fields, replace
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from backend.shadow_mode.analytics import (
    DEFAULT_MIN_FOLLOWUP_MOVE_PCT,
    followup_move,
    score_hypothetical,
)
from backend.signals.models import SIGNAL_ENGINE_VERSION, MarketSnapshot, SignalDirection
from backend.signals.tna_tza_signal_engine import SignalEngineConfig, TnaTzaSignalEngine

TRADE_DECISIONS = (SignalDirection.BULLISH.value, SignalDirection.BEARISH.value)
SKIP = SignalDirection.SKIP.value
MAX_EXAMPLES = 10
EXPECTED_MOVE_MIN_SIMILAR = 5
EXPECTED_MOVE_TINY_MAJORITY = 0.5
LOW_EXPECTED_MOVE_TAG = "low_expected_move"
REPLAY_LABEL = "analytics only — new decisions are hypothetical, never written to the database, no orders"

_CONFIG_FIELDS = {f.name for f in fields(SignalEngineConfig) if f.name != "required_symbols"}


def _json(raw: Any) -> Dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def engine_config_for_row(row: Mapping[str, Any]) -> SignalEngineConfig:
    """Rebuild the engine config from the thresholds stored with the row (defaults otherwise)."""
    stored = _json(row.get("raw_score_json")).get("thresholds") or {}
    overrides = {k: float(v) for k, v in stored.items() if k in _CONFIG_FIELDS and v is not None}
    return replace(SignalEngineConfig(), **overrides).validate()


def replay_row(row: Mapping[str, Any]) -> Dict[str, Any]:
    """New-engine decision for one stored row. Never mutates the row or touches the DB."""
    snapshot_data = _json(row.get("raw_snapshot_json"))
    if not snapshot_data.get("quotes"):
        return {"replayed": False, "reason": "no stored snapshot"}
    try:
        snapshot = MarketSnapshot.from_dict(snapshot_data)
        engine = TnaTzaSignalEngine(engine_config_for_row(row), wall_clock=lambda: snapshot.created_at)
        decision = engine.decide(snapshot)
    except (KeyError, TypeError, ValueError) as exc:
        return {"replayed": False, "reason": f"snapshot replay failed ({type(exc).__name__})"}
    quality = decision.quality.to_dict()
    return {
        "replayed": True,
        "decision": decision.decision.value,
        "selected_symbol": decision.selected_symbol,
        "skip_reason": decision.skip_reason.value if decision.skip_reason else None,
        "confidence_score": decision.confidence_score,
        "bullish_score": decision.bullish_score,
        "bearish_score": decision.bearish_score,
        "quality": quality,
    }


def _band(score: Optional[float]) -> Optional[int]:
    if score is None:
        return None
    return 0 if score < 100 / 3 else 1 if score < 200 / 3 else 2


def _bucket(quality: Mapping[str, Any]) -> Optional[Tuple[str, int, int]]:
    if not quality.get("evaluated"):
        return None
    cont, conf = _band(quality.get("continuation_score")), _band(quality.get("confirmation_score"))
    if quality.get("direction") is None or cont is None or conf is None:
        return None
    return (str(quality["direction"]), cont, conf)


def expected_move_tags(items: Sequence[Dict[str, Any]], *, min_move_pct: float) -> None:
    """
    Diagnostic tag per replayed candidate: low_expected_move when most similar
    historical conditions (same direction + continuation/confirmation bands,
    leave-one-out, >= 5 rows with follow-up) had an IWM follow-up move smaller
    than min_move_pct. Mutates only the in-memory items; never affects decisions.
    """
    buckets: Dict[Tuple[str, int, int], List[Tuple[int, float]]] = {}
    for idx, item in enumerate(items):
        key = item.get("_bucket")
        move = item.get("_iwm_followup")
        if key is not None and move is not None:
            buckets.setdefault(key, []).append((idx, abs(move)))
    for idx, item in enumerate(items):
        key = item.get("_bucket")
        similar = [m for j, m in buckets.get(key, []) if j != idx] if key is not None else []
        tiny = sum(1 for m in similar if m < min_move_pct)
        tag = None
        if len(similar) >= EXPECTED_MOVE_MIN_SIMILAR and tiny / len(similar) > EXPECTED_MOVE_TINY_MAJORITY:
            tag = LOW_EXPECTED_MOVE_TAG
        item["expected_move"] = {
            "similar_rows": len(similar),
            "tiny_followup_rows": tiny,
            "tiny_followup_pct": round(tiny / len(similar) * 100.0, 1) if similar else None,
            "tag": tag,
            "applied_to_decision": False,
        }


def _pct(part: int, whole: int) -> Optional[float]:
    return round(part / whole * 100.0, 1) if whole else None


def _example(item: Mapping[str, Any]) -> Dict[str, Any]:
    q = item["new"].get("quality") or {}
    m = q.get("metrics") or {}
    return {
        "run_id": item["run_id"],
        "cycle_number": item["cycle_number"],
        "created_at": item["created_at"],
        "old_decision": item["old_decision"],
        "old_symbol": item["old_symbol"],
        "old_confidence": item["old_confidence"],
        "old_correct": item["old_correct"],
        "new_decision": item["new"].get("decision"),
        "new_skip_reason": item["new"].get("skip_reason"),
        "continuation_score": q.get("continuation_score"),
        "confirmation_score": q.get("confirmation_score"),
        "chop_risk_score": q.get("chop_risk_score"),
        "pullback_risk_score": q.get("pullback_risk_score"),
        "iwm_window_move_pct": m.get("iwm_window_move_pct"),
        "iwm_extension_pct": m.get("iwm_extension_pct"),
        "selected_followup_move_pct": item["selected_followup"],
        "iwm_followup_move_pct": item["_iwm_followup"],
        "expected_move_tag": (item.get("expected_move") or {}).get("tag"),
    }


def compare_engine_versions(
    rows: Sequence[Mapping[str, Any]], *, min_move_pct: float = DEFAULT_MIN_FOLLOWUP_MOVE_PCT
) -> Dict[str, Any]:
    items: List[Dict[str, Any]] = []
    for row in rows:
        new = replay_row(row)
        old_decision = row.get("decision") or SKIP
        stored_correct = row.get("direction_was_correct")
        old_hyp = score_hypothetical(old_decision, row, min_move_pct=min_move_pct)
        old_correct = stored_correct if stored_correct is not None else old_hyp
        new_decision = new.get("decision") if new.get("replayed") else old_decision
        quality = new.get("quality") or {}
        items.append(
            {
                "run_id": row.get("run_id"),
                "cycle_number": row.get("cycle_number"),
                "created_at": row.get("created_at"),
                "old_decision": old_decision,
                "old_symbol": row.get("selected_symbol"),
                "old_confidence": row.get("confidence_score"),
                "old_skip_reason": row.get("skip_reason"),
                "old_correct": old_correct,
                "old_hypothetical_correct": old_hyp,
                "new": new,
                "new_decision": new_decision,
                "new_hypothetical_correct": score_hypothetical(new_decision, row, min_move_pct=min_move_pct),
                "selected_followup": (
                    followup_move(row, "TNA" if old_decision == SignalDirection.BULLISH.value else "TZA")
                    if old_decision in TRADE_DECISIONS
                    else None
                ),
                "_iwm_followup": followup_move(row, "IWM"),
                "_bucket": _bucket(quality),
            }
        )
    expected_move_tags(items, min_move_pct=min_move_pct)

    def count(key: str, value: str) -> int:
        return sum(1 for i in items if i[key] == value)

    old_trades = [i for i in items if i["old_decision"] in TRADE_DECISIONS]
    new_trades = [i for i in items if i["new_decision"] in TRADE_DECISIONS]
    filtered = [i for i in old_trades if i["new_decision"] == SKIP]
    false_filtered = [i for i in filtered if i["old_correct"] is False]
    missed = [i for i in filtered if i["old_correct"] is True]
    preserved = [i for i in old_trades if i["old_correct"] is True and i["new_decision"] == i["old_decision"]]
    false_kept = [i for i in old_trades if i["old_correct"] is False and i["new_decision"] == i["old_decision"]]
    new_from_skip = [i for i in items if i["old_decision"] == SKIP and i["new_decision"] in TRADE_DECISIONS]

    old_stored_scored = [i for i in old_trades if i["old_correct"] is not None]
    old_hyp_scored = [i for i in old_trades if i["old_hypothetical_correct"] is not None]
    new_scored = [i for i in new_trades if i["new_hypothetical_correct"] is not None]

    new_skip_reasons: Dict[str, int] = {}
    quality_reasons_on_filtered: Dict[str, int] = {}
    for i in items:
        if i["new_decision"] == SKIP:
            reason = i["new"].get("skip_reason") or i["old_skip_reason"] or "unknown"
            new_skip_reasons[reason] = new_skip_reasons.get(reason, 0) + 1
    for i in filtered:
        reason = i["new"].get("skip_reason") or "unknown"
        quality_reasons_on_filtered[reason] = quality_reasons_on_filtered.get(reason, 0) + 1

    tagged = [i for i in items if (i.get("expected_move") or {}).get("tag")]
    return {
        "engine_version": SIGNAL_ENGINE_VERSION,
        "rows_evaluated": len(items),
        "rows_replayed": sum(1 for i in items if i["new"].get("replayed")),
        "rows_not_replayed": sum(1 for i in items if not i["new"].get("replayed")),
        "min_followup_move_pct": min_move_pct,
        "old": {
            "bullish_count": count("old_decision", SignalDirection.BULLISH.value),
            "bearish_count": count("old_decision", SignalDirection.BEARISH.value),
            "skip_count": count("old_decision", SKIP),
            "scored_count": len(old_stored_scored),
            "correct_count": sum(1 for i in old_stored_scored if i["old_correct"]),
            "correct_pct": _pct(sum(1 for i in old_stored_scored if i["old_correct"]), len(old_stored_scored)),
            "hypothetical_scored_count": len(old_hyp_scored),
            "hypothetical_correct_pct": _pct(
                sum(1 for i in old_hyp_scored if i["old_hypothetical_correct"]), len(old_hyp_scored)
            ),
        },
        "new": {
            "bullish_count": count("new_decision", SignalDirection.BULLISH.value),
            "bearish_count": count("new_decision", SignalDirection.BEARISH.value),
            "skip_count": count("new_decision", SKIP),
            "scored_count": len(new_scored),
            "correct_count": sum(1 for i in new_scored if i["new_hypothetical_correct"]),
            "hypothetical_correct_pct": _pct(
                sum(1 for i in new_scored if i["new_hypothetical_correct"]), len(new_scored)
            ),
            "skip_reasons": dict(sorted(new_skip_reasons.items())),
        },
        "signals_filtered": len(filtered),
        "false_signals_filtered": len(false_filtered),
        "good_signals_preserved": len(preserved),
        "missed_winners": len(missed),
        "false_signals_kept": len(false_kept),
        "new_signals_from_old_skips": len(new_from_skip),
        "filtered_by_reason": dict(sorted(quality_reasons_on_filtered.items())),
        "filtered_false_tna_examples": [
            _example(i) for i in false_filtered if i["old_decision"] == SignalDirection.BULLISH.value
        ][:MAX_EXAMPLES],
        "missed_winner_examples": [_example(i) for i in missed][:MAX_EXAMPLES],
        "expected_move_diagnostic": {
            "tag": LOW_EXPECTED_MOVE_TAG,
            "tagged_rows": len(tagged),
            "tagged_new_trades": sum(1 for i in tagged if i["new_decision"] in TRADE_DECISIONS),
            "min_similar_rows": EXPECTED_MOVE_MIN_SIMILAR,
            "applied_to_decisions": False,
            "note": "diagnostic only — not used by the live Signal Engine",
        },
        "writes_to_database": False,
        "orders_submitted": 0,
        "note": REPLAY_LABEL,
    }
