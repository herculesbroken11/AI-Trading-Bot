"""
Skip-opportunity analysis (Checkpoint 2.16) — diagnostics only.

Looks at stored SKIP rows and asks what the market did next:
  * follow-up direction per row (bullish / bearish / flat / mixed)
  * opportunity rates by skip reason, score profile and quality scores
  * missed bullish / bearish opportunities among actionable (fresh) skips
  * broad_market_disagreement diagnostics: did the block avoid a bad trade
    or block a winner?
  * virtual candidate labels (hindsight): would_have_preferred_tna /
    would_have_preferred_tza / would_have_skipped

Read-only: nothing is written back to the database, no setting or threshold is
changed and this module has no broker / order / execution access.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping, Optional, Sequence

from backend.shadow_mode.analytics import (
    DATA_QUALITY_SKIPS,
    DEFAULT_MIN_FOLLOWUP_MOVE_PCT,
    followup_move,
    score_hypothetical,
)
from backend.shadow_mode.models import pct_move

WINDOW_SYMBOLS = ("IWM", "TNA", "TZA", "SPY", "QQQ")
DEFAULT_WINDOW_THRESHOLD_PCT = 0.03  # Signal Engine momentum_threshold_pct default
MAX_EXAMPLES = 5
BROAD_REASON = "broad_market_disagreement"
INSUFFICIENT_REASON = "insufficient_signal_strength"
DIAGNOSTIC_NOTE = (
    "DIAGNOSTIC ONLY — no DB writes, no orders; live thresholds (entry=70 opposing=40 gap=20) "
    "and live signal logic unchanged"
)

FOLLOWUP_CLASSES = ("bullish", "bearish", "flat", "mixed")
_DIRECTION_WORD = {1: "up", -1: "down", 0: "flat"}
_LEAN_WORD = {1: "bullish", -1: "bearish", 0: "neutral"}


def _json(raw: Any) -> Dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _pct(part: int, whole: int) -> Optional[float]:
    return round(part / whole * 100.0, 1) if whole else None


def _avg(values) -> Optional[float]:
    clean = [float(v) for v in values if v is not None]
    return round(sum(clean) / len(clean), 4) if clean else None


def _round(value: Optional[float], digits: int = 4) -> Optional[float]:
    return None if value is None else round(value, digits)


# ---------------------------------------------------------------------------
# Per-row primitives
# ---------------------------------------------------------------------------


def classify_followup(row: Mapping[str, Any], *, min_move_pct: float) -> Optional[str]:
    """
    bullish: TNA up or IWM up by >= min_move_pct
    bearish: TZA up or IWM down by >= min_move_pct
    mixed:   both of the above
    flat:    follow-up exists but neither happened
    None:    no follow-up recorded
    """
    tna, tza, iwm = (followup_move(row, s) for s in ("TNA", "TZA", "IWM"))
    if all(m is None for m in (tna, tza, iwm)):
        return None
    bullish = (tna is not None and tna >= min_move_pct) or (iwm is not None and iwm >= min_move_pct)
    bearish = (tza is not None and tza >= min_move_pct) or (iwm is not None and iwm <= -min_move_pct)
    if bullish and bearish:
        return "mixed"
    if bullish:
        return "bullish"
    if bearish:
        return "bearish"
    return "flat"


def window_moves(row: Mapping[str, Any]) -> Dict[str, Optional[float]]:
    """In-window % move (current mid vs window first mid) per symbol from the stored snapshot."""
    quotes = _json(row.get("raw_snapshot_json")).get("quotes") or {}
    out: Dict[str, Optional[float]] = {}
    for symbol in WINDOW_SYMBOLS:
        q = quotes.get(symbol) or {}
        mid = q.get("mid")
        if mid is None and q.get("bid") is not None and q.get("ask") is not None:
            mid = (float(q["bid"]) + float(q["ask"])) / 2
        out[symbol] = pct_move(q.get("first_mid"), mid)
    return out


def direction_of(move: Optional[float], threshold_pct: float) -> Optional[int]:
    if move is None:
        return None
    return 1 if move >= threshold_pct else -1 if move <= -threshold_pct else 0


def _components(row: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    breakdown = _json(row.get("raw_score_json")).get("score_breakdown") or {}
    return {c.get("name"): c for c in breakdown.get("components") or [] if isinstance(c, dict)}


def stored_quality(row: Mapping[str, Any]) -> Dict[str, Any]:
    return _json(row.get("raw_score_json")).get("quality") or {}


def iwm_primary_direction(row: Mapping[str, Any], *, threshold_pct: float = DEFAULT_WINDOW_THRESHOLD_PCT) -> int:
    """The engine's IWM direction (level + momentum points); falls back to the IWM window move."""
    comps = _components(row)
    names = ("iwm_vs_day_open", "iwm_vs_prev_close", "iwm_momentum")
    if any(n in comps for n in names):
        bull = sum(float(comps[n].get("bullish") or 0) for n in names if n in comps)
        bear = sum(float(comps[n].get("bearish") or 0) for n in names if n in comps)
        return 1 if bull > bear else -1 if bear > bull else 0
    return direction_of(window_moves(row)["IWM"], threshold_pct) or 0


def level_trend(row: Mapping[str, Any], symbol: str) -> Optional[int]:
    """SPY / QQQ level trend used by the engine's confirmation (vs open / previous close)."""
    comp = _components(row).get(f"{symbol.lower()}_confirmation")
    if comp is None:
        return None
    return 1 if float(comp.get("bullish") or 0) > 0 else -1 if float(comp.get("bearish") or 0) > 0 else 0


def evidence_lean(row: Mapping[str, Any], *, threshold_pct: float = DEFAULT_WINDOW_THRESHOLD_PCT) -> int:
    """Which side the stored evidence leaned to: score gap first, then the IWM window move."""
    bull = float(row.get("bullish_score") or 0)
    bear = float(row.get("bearish_score") or 0)
    if bull > bear:
        return 1
    if bear > bull:
        return -1
    return direction_of(window_moves(row)["IWM"], threshold_pct) or 0


def virtual_candidate_labels(row: Mapping[str, Any], *, min_move_pct: float) -> Dict[str, Any]:
    """
    Diagnostic-only hindsight labels for one row (never written back):
      would_have_preferred_tna  follow-up was clearly bullish
      would_have_preferred_tza  follow-up was clearly bearish
      would_have_skipped        flat / mixed / no follow-up
    plus whether the stored evidence (scores / IWM window / quality direction) leaned the same way.
    """
    followup = classify_followup(row, min_move_pct=min_move_pct)
    lean = evidence_lean(row)
    quality = stored_quality(row)
    hindsight = 1 if followup == "bullish" else -1 if followup == "bearish" else 0
    return {
        "followup_class": followup,
        "would_have_preferred_tna": hindsight == 1,
        "would_have_preferred_tza": hindsight == -1,
        "would_have_skipped": hindsight == 0,
        "evidence_lean": _LEAN_WORD[lean],
        "quality_direction": quality.get("direction") if quality.get("evaluated") else None,
        "lean_matches_followup": None if hindsight == 0 or lean == 0 else lean == hindsight,
    }


def broad_disagreement_diagnostics(
    row: Mapping[str, Any],
    *,
    min_move_pct: float,
    threshold_pct: float = DEFAULT_WINDOW_THRESHOLD_PCT,
) -> Dict[str, Any]:
    """Why the broad-market block fired and what happened afterwards (diagnostic only)."""
    moves = window_moves(row)
    primary = iwm_primary_direction(row, threshold_pct=threshold_pct)
    iwm_window = direction_of(moves["IWM"], threshold_pct)

    favoured, inverse = (moves["TNA"], moves["TZA"]) if primary >= 0 else (moves["TZA"], moves["TNA"])
    if primary == 0 or favoured is None or inverse is None:
        pair = "unknown"
    elif favoured >= threshold_pct and inverse <= -threshold_pct:
        pair = "confirming"
    elif favoured <= -threshold_pct and inverse >= threshold_pct:
        pair = "against"
    elif favoured >= threshold_pct or inverse <= -threshold_pct:
        pair = "partial"
    else:
        pair = "flat"

    spy_level, qqq_level = level_trend(row, "SPY"), level_trend(row, "QQQ")
    spy_window, qqq_window = direction_of(moves["SPY"], threshold_pct / 2), direction_of(moves["QQQ"], threshold_pct / 2)

    iwm_after = followup_move(row, "IWM")
    signed_iwm = None if iwm_after is None or primary == 0 else iwm_after * primary
    iwm_validated = direction_of(signed_iwm, min_move_pct)
    etf = "TNA" if primary >= 0 else "TZA"
    etf_validated = direction_of(followup_move(row, etf), min_move_pct)
    direction = "bullish" if primary == 1 else "bearish" if primary == -1 else None
    outcome = score_hypothetical(direction, row, min_move_pct=min_move_pct) if direction else None
    return {
        "iwm_direction": _DIRECTION_WORD.get(primary),
        "iwm_window_direction": _DIRECTION_WORD.get(iwm_window) if iwm_window is not None else None,
        "iwm_window_move_pct": _round(moves["IWM"]),
        "tna_window_move_pct": _round(moves["TNA"]),
        "tza_window_move_pct": _round(moves["TZA"]),
        "tna_tza_window_confirmation": pair,
        "spy_level_direction": _DIRECTION_WORD.get(spy_level) if spy_level is not None else None,
        "qqq_level_direction": _DIRECTION_WORD.get(qqq_level) if qqq_level is not None else None,
        "spy_window_direction": _DIRECTION_WORD.get(spy_window) if spy_window is not None else None,
        "qqq_window_direction": _DIRECTION_WORD.get(qqq_window) if qqq_window is not None else None,
        "spy_qqq_level_disagreed": primary != 0 and spy_level == -primary and qqq_level == -primary,
        "spy_qqq_window_disagreed": primary != 0 and spy_window == -primary and qqq_window == -primary,
        "iwm_followup_validated": None if iwm_validated in (None, 0) else iwm_validated == 1,
        "etf_followup_symbol": etf if primary != 0 else None,
        "etf_followup_validated": None if etf_validated in (None, 0) or primary == 0 else etf_validated == 1,
        "blocked_trade_outcome": (
            "blocked_winner" if outcome is True else "avoided_bad_trade" if outcome is False else "flat_or_unscored"
        ),
    }


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _new_stats() -> Dict[str, Any]:
    return {"skips": 0, "with_followup": 0, "bullish": 0, "bearish": 0, "flat": 0, "mixed": 0,
            "lean_validated": 0, "lean_contradicted": 0, "_abs_iwm": []}


def _add(stats: Dict[str, Any], item: Mapping[str, Any]) -> None:
    stats["skips"] += 1
    cls = item["followup_class"]
    if cls is None:
        return
    stats["with_followup"] += 1
    stats[cls] += 1
    match = item["labels"]["lean_matches_followup"]
    if match is True:
        stats["lean_validated"] += 1
    elif match is False:
        stats["lean_contradicted"] += 1
    if item["iwm_followup_move_pct"] is not None:
        stats["_abs_iwm"].append(abs(item["iwm_followup_move_pct"]))


def _finish(groups: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    out = {}
    for key in sorted(groups):
        s = dict(groups[key])
        moved = s["bullish"] + s["bearish"]
        s["opportunity_pct"] = _pct(moved, s["with_followup"])
        s["avg_abs_iwm_followup_pct"] = _avg(s.pop("_abs_iwm"))
        out[key] = s
    return out


def _score_band(row: Mapping[str, Any]) -> str:
    best = max(float(row.get("bullish_score") or 0), float(row.get("bearish_score") or 0))
    if best >= 70:
        return "70+"
    if best >= 60:
        return "60-69"
    if best >= 40:
        return "40-59"
    return "0-39"


def _band3(score: Optional[float]) -> str:
    if score is None:
        return "n/a"
    return "low" if score < 100 / 3 else "mid" if score < 200 / 3 else "high"


def _quality_bucket(row: Mapping[str, Any]) -> str:
    q = stored_quality(row)
    if not q.get("evaluated"):
        return "quality_not_evaluated"
    return f"continuation_{_band3(q.get('continuation_score'))}/chop_{_band3(q.get('chop_risk_score'))}"


def _example(item: Mapping[str, Any], *, broad: bool = False) -> Dict[str, Any]:
    row = item["row"]
    q = stored_quality(row)
    out = {
        "run_id": row.get("run_id"),
        "cycle_number": row.get("cycle_number"),
        "created_at": row.get("created_at"),
        "skip_reason": row.get("skip_reason"),
        "bullish_score": row.get("bullish_score"),
        "bearish_score": row.get("bearish_score"),
        "followup_class": item["followup_class"],
        "iwm_followup_move_pct": _round(item["iwm_followup_move_pct"]),
        "tna_followup_move_pct": _round(followup_move(row, "TNA")),
        "tza_followup_move_pct": _round(followup_move(row, "TZA")),
        "iwm_window_move_pct": _round(item["window"]["IWM"]),
        "continuation_score": q.get("continuation_score"),
        "chop_risk_score": q.get("chop_risk_score"),
        "labels": item["labels"],
    }
    if broad:
        out["broad"] = item["broad"]
    return out


def _missed(items: Sequence[Mapping[str, Any]], cls: str) -> Dict[str, Any]:
    hits = [i for i in items if i["actionable"] and i["followup_class"] == cls]
    by_reason: Dict[str, int] = {}
    for i in hits:
        key = i["row"].get("skip_reason") or "unknown"
        by_reason[key] = by_reason.get(key, 0) + 1
    lean = 1 if cls == "bullish" else -1
    return {
        "count": len(hits),
        "evidence_leaned_same_way": sum(1 for i in hits if evidence_lean(i["row"]) == lean),
        "by_skip_reason": dict(sorted(by_reason.items(), key=lambda kv: (-kv[1], kv[0]))),
        "examples": [_example(i) for i in hits[:MAX_EXAMPLES]],
    }


def analyze_skip_opportunities(
    rows: Sequence[Mapping[str, Any]], *, min_move_pct: float = DEFAULT_MIN_FOLLOWUP_MOVE_PCT
) -> Dict[str, Any]:
    skips = [r for r in rows if (r.get("decision") or "skip") == "skip"]
    items: List[Dict[str, Any]] = []
    for row in skips:
        reason = row.get("skip_reason") or "unknown"
        item = {
            "row": row,
            "followup_class": classify_followup(row, min_move_pct=min_move_pct),
            "iwm_followup_move_pct": followup_move(row, "IWM"),
            "window": window_moves(row),
            "labels": virtual_candidate_labels(row, min_move_pct=min_move_pct),
            "actionable": bool(row.get("freshness_gate_passed")) and reason not in DATA_QUALITY_SKIPS,
        }
        if reason == BROAD_REASON:
            item["broad"] = broad_disagreement_diagnostics(row, min_move_pct=min_move_pct)
        items.append(item)

    reasons: Dict[str, int] = {}
    by_reason: Dict[str, Dict[str, Any]] = {}
    by_score: Dict[str, Dict[str, Any]] = {}
    by_quality: Dict[str, Dict[str, Any]] = {}
    followup_counts = {f"{c}_followup": 0 for c in FOLLOWUP_CLASSES}
    followup_counts["no_followup"] = 0
    for item in items:
        row = item["row"]
        reason = row.get("skip_reason") or "unknown"
        reasons[reason] = reasons.get(reason, 0) + 1
        cls = item["followup_class"]
        followup_counts[f"{cls}_followup" if cls else "no_followup"] += 1
        _add(by_reason.setdefault(reason, _new_stats()), item)
        if item["actionable"]:
            _add(by_score.setdefault(f"{item['labels']['evidence_lean']}/{_score_band(row)}", _new_stats()), item)
            _add(by_quality.setdefault(_quality_bucket(row), _new_stats()), item)

    broad_items = [i for i in items if "broad" in i]
    outcomes = [i["broad"]["blocked_trade_outcome"] for i in broad_items]
    moved = lambda i: i["followup_class"] in ("bullish", "bearish", "mixed")  # noqa: E731
    broad_summary = {
        "rows": len(broad_items),
        "with_followup": sum(1 for i in broad_items if i["followup_class"] is not None),
        "avoided_bad_trade": outcomes.count("avoided_bad_trade"),
        "blocked_winner": outcomes.count("blocked_winner"),
        "flat_or_unscored": outcomes.count("flat_or_unscored"),
        "iwm_followup_validated_iwm_direction": sum(1 for i in broad_items if i["broad"]["iwm_followup_validated"] is True),
        "iwm_followup_contradicted_iwm_direction": sum(1 for i in broad_items if i["broad"]["iwm_followup_validated"] is False),
        "etf_followup_validated": sum(1 for i in broad_items if i["broad"]["etf_followup_validated"] is True),
        "etf_followup_contradicted": sum(1 for i in broad_items if i["broad"]["etf_followup_validated"] is False),
        "spy_qqq_also_disagreed_in_window": sum(1 for i in broad_items if i["broad"]["spy_qqq_window_disagreed"]),
        "tna_tza_window_confirmation": {
            k: sum(1 for i in broad_items if i["broad"]["tna_tza_window_confirmation"] == k)
            for k in ("confirming", "partial", "flat", "against", "unknown")
        },
    }

    return {
        "min_followup_move_pct": min_move_pct,
        "total_rows": len(rows),
        "total_skips": len(items),
        "skips_with_followup": sum(1 for i in items if i["followup_class"] is not None),
        "actionable_skips": sum(1 for i in items if i["actionable"]),
        "data_quality_skips_with_movement": sum(
            1 for i in items if not i["actionable"] and i["followup_class"] in ("bullish", "bearish", "mixed")
        ),
        "skip_reasons": dict(sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))),
        "followup_direction": followup_counts,
        "opportunity_by_skip_reason": _finish(by_reason),
        "opportunity_by_score_profile": _finish(by_score),
        "opportunity_by_quality_scores": _finish(by_quality),
        "missed_bullish_opportunities": _missed(items, "bullish"),
        "missed_bearish_opportunities": _missed(items, "bearish"),
        "virtual_candidates": {
            "would_have_preferred_tna": sum(1 for i in items if i["labels"]["would_have_preferred_tna"]),
            "would_have_preferred_tza": sum(1 for i in items if i["labels"]["would_have_preferred_tza"]),
            "would_have_skipped": sum(1 for i in items if i["labels"]["would_have_skipped"]),
            "applied_to_decisions": False,
        },
        "broad_market_disagreement": broad_summary,
        "broad_market_disagreement_moved_examples": [
            _example(i, broad=True) for i in broad_items if moved(i)
        ][:MAX_EXAMPLES],
        "insufficient_signal_strength_moved_examples": [
            _example(i) for i in items if i["row"].get("skip_reason") == INSUFFICIENT_REASON and moved(i)
        ][:MAX_EXAMPLES],
        "correct_skip_examples": [
            _example(i) for i in items if i["actionable"] and i["followup_class"] in ("flat", "mixed")
        ][:MAX_EXAMPLES],
        "writes_to_database": False,
        "orders_submitted": 0,
        "note": DIAGNOSTIC_NOTE,
    }
