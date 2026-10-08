"""Shadow-mode log summary (diagnostic statistics only — not profit/P&L)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional

from backend.shadow_mode.models import SESSION_FIELDS, STALE_FOLLOWUP_NOTE, TRACKED_SYMBOLS
from backend.shadow_mode.session_quality import overall_market_window_quality, session_breakdown

DATA_QUALITY_SKIPS = frozenset({"stale_market_data", "missing_required_quote"})
UNCLEAR_SKIPS = frozenset({"unclear_market_direction"})
FRESHNESS_EVAL_MIN_PCT = 90.0
QUOTE_AGE_EVAL_MAX_SECONDS = 1.0
STREAM_NOT_READY_EVAL_MAX_PCT = 10.0
STALE_FOLLOWUP_EVAL_MAX_PCT = 10.0
DEFAULT_MIN_FOLLOWUP_MOVE_PCT = 0.03

_ROW_FIELDS = (
    "id",
    "run_id",
    "cycle_number",
    "created_at",
    "decision",
    "selected_symbol",
    "confidence_score",
    "bullish_score",
    "bearish_score",
    "skip_reason",
    "market_regime",
    "explanation",
    "freshness_gate_passed",
    "submitted",
    "production_execution_blocked",
    "followup_seconds",
    "selected_symbol_move_pct",
    "iwm_move_pct",
    "direction_was_correct",
    "outcome_note",
    *SESSION_FIELDS,
    *(f"quote_age_{s.lower()}" for s in TRACKED_SYMBOLS),
    *(f"{s.lower()}_mid" for s in TRACKED_SYMBOLS),
)


def row_to_dict(row: Any) -> Dict[str, Any]:
    """Accept an ORM row, a ShadowCycleRecord or a mapping."""
    if hasattr(row, "to_dict") and not isinstance(row, Mapping):
        source: Mapping[str, Any] = row.to_dict()
    elif isinstance(row, Mapping):
        source = row
    else:
        source = {name: getattr(row, name, None) for name in _ROW_FIELDS}
    data = {name: source.get(name) for name in _ROW_FIELDS}
    created = data.get("created_at")
    if isinstance(created, datetime):
        data["created_at"] = created.isoformat()
    return data


def _avg(values: Iterable[Optional[float]], digits: int = 3) -> Optional[float]:
    clean = [float(v) for v in values if v is not None]
    return round(sum(clean) / len(clean), digits) if clean else None


def _pct(part: int, whole: int) -> Optional[float]:
    return round(part / whole * 100.0, 1) if whole else None


def _meaningful_flag(item: Mapping[str, Any], *, min_move_pct: float) -> Optional[bool]:
    """Meaningful-move score from the stored follow-up percents. Flat moves stay unscored."""
    direction = item.get("decision")
    if direction not in ("bullish", "bearish"):
        return None
    if STALE_FOLLOWUP_NOTE in str(item.get("outcome_note") or ""):
        return None
    selected = item.get("selected_symbol_move_pct")
    iwm = item.get("iwm_move_pct")
    known = [float(move) for move in (selected, iwm) if move is not None]
    if not known:
        return None
    iwm_directional = None if iwm is None else (float(iwm) if direction == "bullish" else -float(iwm))
    if (selected is not None and float(selected) >= min_move_pct) or (
        iwm_directional is not None and iwm_directional >= min_move_pct
    ):
        return True
    if max(abs(move) for move in known) < min_move_pct:
        return None
    return False


def _outcome_block(flags: List[Optional[bool]]) -> Dict[str, Any]:
    scored = [flag for flag in flags if flag is not None]
    correct = sum(1 for flag in scored if flag is True)
    return {
        "scored": len(scored),
        "correct": correct,
        "incorrect": len(scored) - correct,
        "correct_pct": _pct(correct, len(scored)),
    }


def _evaluation(items: List[Dict[str, Any]], *, freshness_pct: Optional[float], average_age: Optional[float], stream_not_ready: int, stale_followup: int) -> Dict[str, Any]:
    reasons: List[str] = []
    if freshness_pct is None or freshness_pct < FRESHNESS_EVAL_MIN_PCT:
        reasons.append(f"freshness pass below {FRESHNESS_EVAL_MIN_PCT:g}%")
    if average_age is None or average_age > QUOTE_AGE_EVAL_MAX_SECONDS:
        reasons.append(f"average quote age above {QUOTE_AGE_EVAL_MAX_SECONDS:g}s")
    total = len(items)
    if total and stream_not_ready / total * 100.0 > STREAM_NOT_READY_EVAL_MAX_PCT:
        reasons.append(f"stream_not_ready above {STREAM_NOT_READY_EVAL_MAX_PCT:g}% of cycles")
    with_followup = sum(1 for item in items if item.get("followup_seconds"))
    if with_followup and stale_followup / with_followup * 100.0 > STALE_FOLLOWUP_EVAL_MAX_PCT:
        reasons.append(f"stale follow-up above {STALE_FOLLOWUP_EVAL_MAX_PCT:g}% of follow-ups")
    return {"valid_for_signal_evaluation": not reasons, "evaluation_block_reasons": reasons}


def summarize_shadow_logs(rows: Iterable[Any], *, latest: int = 10, min_followup_move_pct: float = DEFAULT_MIN_FOLLOWUP_MOVE_PCT) -> Dict[str, Any]:
    items: List[Dict[str, Any]] = [row_to_dict(r) for r in rows]
    items.sort(key=lambda d: (d.get("created_at") or "", d.get("id") or 0), reverse=True)

    by_decision = {k: [d for d in items if d.get("decision") == k] for k in ("bullish", "bearish", "skip")}
    trades = by_decision["bullish"] + by_decision["bearish"]
    skips = by_decision["skip"]

    skip_reasons: Dict[str, int] = {}
    for d in skips:
        key = d.get("skip_reason") or "unknown"
        skip_reasons[key] = skip_reasons.get(key, 0) + 1

    scored = [d for d in trades if d.get("direction_was_correct") is not None]
    correct = sum(1 for d in scored if d["direction_was_correct"] is True)
    stream_not_ready = sum(1 for d in items if d.get("skip_reason") == "stream_not_ready")
    stale_followup = sum(1 for d in items if STALE_FOLLOWUP_NOTE in str(d.get("outcome_note") or ""))
    strict = _outcome_block([d.get("direction_was_correct") if d.get("decision") in ("bullish", "bearish") else None for d in items])
    meaningful_flags = [_meaningful_flag(d, min_move_pct=min_followup_move_pct) for d in items]
    meaningful = _outcome_block(meaningful_flags)
    symbol_ages = [_avg(d.get(f"quote_age_{s.lower()}") for d in items) for s in TRACKED_SYMBOLS]
    average_age = _avg(symbol_ages)
    freshness_pct = _pct(sum(1 for d in items if d.get("freshness_gate_passed")), len(items))
    evaluation = _evaluation(
        items,
        freshness_pct=freshness_pct,
        average_age=average_age,
        stream_not_ready=stream_not_ready,
        stale_followup=stale_followup,
    )

    def outcome_for(direction: str) -> Dict[str, Any]:
        group = [d for d in by_decision[direction] if d.get("direction_was_correct") is not None]
        ok = sum(1 for d in group if d["direction_was_correct"] is True)
        return {
            "scored": len(group),
            "correct": ok,
            "incorrect": len(group) - ok,
            "correct_pct": _pct(ok, len(group)),
            "avg_selected_symbol_move_pct": _avg((d.get("selected_symbol_move_pct") for d in by_decision[direction]), 4),
            "avg_iwm_move_pct": _avg((d.get("iwm_move_pct") for d in by_decision[direction]), 4),
        }

    return {
        "total_signals": len(items),
        "bullish_count": len(by_decision["bullish"]),
        "bearish_count": len(by_decision["bearish"]),
        "skip_count": len(skips),
        "stale_or_missing_data_skip_count": sum(1 for d in skips if d.get("skip_reason") in DATA_QUALITY_SKIPS),
        "unclear_market_skip_count": sum(1 for d in skips if d.get("skip_reason") in UNCLEAR_SKIPS),
        "skip_reasons": dict(sorted(skip_reasons.items())),
        "average_confidence_all": _avg((d.get("confidence_score") for d in items), 2),
        "average_confidence_trade_signals": _avg((d.get("confidence_score") for d in trades), 2),
        "average_quote_age_seconds": {
            s: _avg(d.get(f"quote_age_{s.lower()}") for d in items) for s in TRACKED_SYMBOLS
        },
        "stream_not_ready_count": stream_not_ready,
        "stale_followup_count": stale_followup,
        "freshness_gate_pass_pct": freshness_pct,
        "average_quote_age_seconds_overall": average_age,
        "strict_direction_outcome": strict,
        "meaningful_move_outcome": meaningful,
        **evaluation,
        "outcomes": {
            "with_followup": sum(1 for d in items if d.get("followup_seconds")),
            "scored": len(scored),
            "correct": correct,
            "incorrect": len(scored) - correct,
            "correct_pct": _pct(correct, len(scored)),
            "bullish": outcome_for("bullish"),
            "bearish": outcome_for("bearish"),
            "skip_avg_iwm_move_pct": _avg((d.get("iwm_move_pct") for d in skips), 4),
        },
        "session_breakdown": session_breakdown(items),
        "market_window_quality": overall_market_window_quality(items),
        "submitted_count": sum(1 for d in items if d.get("submitted")),
        "production_execution_blocked_all": all(d.get("production_execution_blocked") is not False for d in items),
        "latest_decisions": items[: max(0, latest)],
        "note": "diagnostic direction statistics only; not a profit calculation",
    }
