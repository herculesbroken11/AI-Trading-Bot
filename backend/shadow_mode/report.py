"""Shadow-mode log summary (diagnostic statistics only — not profit/P&L)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional

from backend.shadow_mode.models import SESSION_FIELDS, TRACKED_SYMBOLS
from backend.shadow_mode.session_quality import overall_market_window_quality, session_breakdown

DATA_QUALITY_SKIPS = frozenset({"stale_market_data", "missing_required_quote"})
UNCLEAR_SKIPS = frozenset({"unclear_market_direction"})

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


def summarize_shadow_logs(rows: Iterable[Any], *, latest: int = 10) -> Dict[str, Any]:
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
        "freshness_gate_pass_pct": _pct(sum(1 for d in items if d.get("freshness_gate_passed")), len(items)),
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
