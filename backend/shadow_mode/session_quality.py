"""
Shadow window quality by market session (Checkpoint 2.14). Read-only statistics.

Every row gets an effective session label: the logged session_label, or —
for rows logged before 2.14 — a label derived from created_at (UTC). Rows are
grouped into windows (run_id + session label) and each window is rated
good / weak / bad so poor collection windows can be excluded before analysis.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from backend.market_session import DEFAULT_NEAR_CLOSE_MINUTES, SessionLabel, get_session_status
from backend.shadow_mode.models import TRACKED_SYMBOLS

GOOD_FRESHNESS_PCT = 90.0
MIN_FRESHNESS_PCT = 70.0
BAD_MAJORITY = 0.5
BAD_SESSION_LABELS = frozenset(
    {
        SessionLabel.AFTER_HOURS.value,
        SessionLabel.WEEKEND.value,
        SessionLabel.PRE_MARKET.value,
        SessionLabel.CLOSED_UNKNOWN_HOLIDAY.value,
    }
)
STALE_SKIPS = frozenset({"stale_market_data", "missing_required_quote", "invalid_quote"})
UNKNOWN_LABEL = "unknown"
LABEL_ORDER = [label.value for label in SessionLabel] + [UNKNOWN_LABEL]


def _parse_created(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        created = value
    else:
        try:
            created = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return created if created.tzinfo else created.replace(tzinfo=timezone.utc)


def effective_session_label(
    row: Mapping[str, Any], *, near_close_minutes: float = DEFAULT_NEAR_CLOSE_MINUTES
) -> Tuple[str, str]:
    """(label, source) where source is logged | derived_from_created_at | unknown."""
    label = row.get("session_label")
    if label:
        return str(label), "logged"
    created = _parse_created(row.get("created_at"))
    if created is None:
        return UNKNOWN_LABEL, "unknown"
    return get_session_status(created, near_close_minutes=near_close_minutes).label.value, "derived_from_created_at"


def _avg(values: Iterable[Optional[float]], digits: int = 3) -> Optional[float]:
    clean = [float(v) for v in values if v is not None]
    return round(sum(clean) / len(clean), digits) if clean else None


def _pct(part: int, whole: int) -> Optional[float]:
    return round(part / whole * 100.0, 1) if whole else None


def group_stats(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    ages_by_symbol = {s: _avg(r.get(f"quote_age_{s.lower()}") for r in rows) for s in TRACKED_SYMBOLS}
    return {
        "cycles": len(rows),
        "decisions": {k: sum(1 for r in rows if r.get("decision") == k) for k in ("bullish", "bearish", "skip")},
        "freshness_pass_pct": _pct(sum(1 for r in rows if r.get("freshness_gate_passed")), len(rows)),
        "stale_skip_count": sum(
            1 for r in rows if r.get("decision") == "skip" and r.get("skip_reason") in STALE_SKIPS
        ),
        "avg_quote_age_seconds": _avg(r.get(f"quote_age_{s.lower()}") for r in rows for s in TRACKED_SYMBOLS),
        "avg_quote_age_by_symbol": ages_by_symbol,
    }


def window_quality(
    label: Optional[str],
    freshness_pass_pct: Optional[float],
    *,
    market_window_status: Optional[str] = None,
) -> Tuple[str, List[str]]:
    """
    bad:  after_hours / weekend / pre_market / holiday, freshness < 70%
          (includes near_close with poor freshness), or no cycles.
    weak: near_close with good freshness, unknown session, freshness < 90%,
          or a flat/choppy market.
    good: regular hours, freshness >= 90%, market not flat/choppy.
    """
    if label in BAD_SESSION_LABELS:
        return "bad", [f"{label} session"]
    if freshness_pass_pct is None:
        return "bad", ["no cycles"]
    if freshness_pass_pct < MIN_FRESHNESS_PCT:
        reasons = [f"freshness_pass_pct {freshness_pass_pct:g}% < {MIN_FRESHNESS_PCT:g}%"]
        if label == SessionLabel.NEAR_CLOSE.value:
            reasons.append("near_close with poor freshness")
        return "bad", reasons
    weak: List[str] = []
    if label == SessionLabel.NEAR_CLOSE.value:
        weak.append("near_close window")
    if label in (None, UNKNOWN_LABEL):
        weak.append("session unknown")
    if freshness_pass_pct < GOOD_FRESHNESS_PCT:
        weak.append(f"freshness_pass_pct {freshness_pass_pct:g}% < {GOOD_FRESHNESS_PCT:g}%")
    if market_window_status == "flat/choppy":
        weak.append("market flat/choppy")
    if weak:
        return "weak", weak
    return "good", ["regular hours with fresh quotes"]


def _labels(rows: Sequence[Mapping[str, Any]], near_close_minutes: float) -> List[Tuple[str, str]]:
    return [effective_session_label(r, near_close_minutes=near_close_minutes) for r in rows]


def session_breakdown(
    rows: Sequence[Mapping[str, Any]], *, near_close_minutes: float = DEFAULT_NEAR_CLOSE_MINUTES
) -> Dict[str, Dict[str, Any]]:
    groups: Dict[str, List[Mapping[str, Any]]] = {}
    sources: Dict[str, Dict[str, int]] = {}
    for row, (label, source) in zip(rows, _labels(rows, near_close_minutes)):
        groups.setdefault(label, []).append(row)
        sources.setdefault(label, {}).setdefault(source, 0)
        sources[label][source] += 1
    out: Dict[str, Dict[str, Any]] = {}
    for label in sorted(groups, key=lambda k: LABEL_ORDER.index(k) if k in LABEL_ORDER else len(LABEL_ORDER)):
        stats = group_stats(groups[label])
        quality, reasons = window_quality(label, stats["freshness_pass_pct"])
        out[label] = {**stats, "label_sources": sources[label], "window_quality": quality, "quality_reasons": reasons}
    return out


def session_windows(
    rows: Sequence[Mapping[str, Any]], *, near_close_minutes: float = DEFAULT_NEAR_CLOSE_MINUTES
) -> List[Dict[str, Any]]:
    """One entry per (run_id, session label) window with its quality and row indexes."""
    windows: Dict[Tuple[str, str], List[int]] = {}
    for index, (label, _source) in enumerate(_labels(rows, near_close_minutes)):
        windows.setdefault((str(rows[index].get("run_id")), label), []).append(index)
    out = []
    for (run_id, label), indexes in windows.items():
        group = [rows[i] for i in indexes]
        stats = group_stats(group)
        quality, reasons = window_quality(label, stats["freshness_pass_pct"])
        out.append(
            {
                "run_id": run_id,
                "session_label": label,
                "cycles": stats["cycles"],
                "freshness_pass_pct": stats["freshness_pass_pct"],
                "stale_skip_count": stats["stale_skip_count"],
                "window_quality": quality,
                "reasons": reasons,
                "first_cycle_at": group[0].get("created_at"),
                "_indexes": indexes,
            }
        )
    out.sort(key=lambda w: str(w["first_cycle_at"] or ""))
    return out


def _public(window: Mapping[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in window.items() if not k.startswith("_")}


def split_bad_session_windows(
    rows: Sequence[Mapping[str, Any]], *, near_close_minutes: float = DEFAULT_NEAR_CLOSE_MINUTES
) -> Tuple[List[Mapping[str, Any]], List[Dict[str, Any]]]:
    """(kept rows, excluded bad windows). Rows are returned unchanged, never copied or mutated."""
    windows = session_windows(rows, near_close_minutes=near_close_minutes)
    excluded_idx = {i for w in windows if w["window_quality"] == "bad" for i in w["_indexes"]}
    kept = [row for i, row in enumerate(rows) if i not in excluded_idx]
    return kept, [_public(w) for w in windows if w["window_quality"] == "bad"]


def overall_market_window_quality(
    rows: Sequence[Mapping[str, Any]],
    *,
    market_window_status: Optional[str] = None,
    near_close_minutes: float = DEFAULT_NEAR_CLOSE_MINUTES,
) -> Dict[str, Any]:
    windows = session_windows(rows, near_close_minutes=near_close_minutes)
    total = len(rows)
    freshness = _pct(sum(1 for r in rows if r.get("freshness_gate_passed")), total)
    bad_cycles = sum(w["cycles"] for w in windows if w["window_quality"] == "bad")
    weak_cycles = sum(w["cycles"] for w in windows if w["window_quality"] == "weak")
    reasons: List[str] = []
    if not total:
        quality, reasons = "bad", ["no cycles"]
    elif freshness is not None and freshness < MIN_FRESHNESS_PCT:
        quality = "bad"
        reasons.append(f"freshness_pass_pct {freshness:g}% < {MIN_FRESHNESS_PCT:g}%")
    elif bad_cycles / total > BAD_MAJORITY:
        quality = "bad"
        reasons.append(f"{bad_cycles}/{total} cycles in bad session windows")
    else:
        if bad_cycles:
            reasons.append(f"{bad_cycles}/{total} cycles in bad session windows")
        if weak_cycles:
            reasons.append(f"{weak_cycles}/{total} cycles in weak session windows")
        if freshness is not None and freshness < GOOD_FRESHNESS_PCT:
            reasons.append(f"freshness_pass_pct {freshness:g}% < {GOOD_FRESHNESS_PCT:g}%")
        if market_window_status == "flat/choppy":
            reasons.append("market flat/choppy")
        quality = "weak" if reasons else "good"
        if not reasons:
            reasons.append("regular-hours windows with fresh quotes")
    return {
        "market_window_quality": quality,
        "reasons": reasons,
        "freshness_pass_pct": freshness,
        "bad_window_cycles": bad_cycles,
        "weak_window_cycles": weak_cycles,
        "windows": [_public(w) for w in windows],
    }
