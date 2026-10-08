"""
Shadow analytics and offline threshold calibration (Checkpoint 2.13).

Read-only analysis of shadow_signal_log rows:
  * actual-run statistics (skip reasons, freshness, quote ages, follow-up moves)
  * market flatness detection (is the window too flat to judge thresholds?)
  * per-run comparison
  * offline threshold simulation: each stored raw_snapshot_json is replayed
    through the real Signal Engine under alternative *safe* thresholds
    (fallback: stored raw_score_json scores). Results are hypothetical and
    diagnostic only — nothing is written back, no threshold is applied, and
    this module has no broker / order / execution access.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from backend.shadow_mode.models import SESSION_FIELDS, TRACKED_SYMBOLS, ShadowCycleRecord, pct_move
from backend.shadow_mode.session_quality import (
    effective_session_label,
    overall_market_window_quality,
    session_breakdown,
)
from backend.signals.models import MarketSnapshot, SignalDirection
from backend.signals.tna_tza_signal_engine import SignalEngineConfig, TnaTzaSignalEngine

SAFE_MIN_ENTRY = 60.0
SAFE_MAX_ENTRY = 100.0
SAFE_MIN_SCORE_GAP = 10.0
SAFE_OPPOSING_MARGIN = 20.0  # opposing max must be <= entry - 20
DEFAULT_MIN_FOLLOWUP_MOVE_PCT = 0.03
FLAT_MAJORITY = 0.5
MIN_SCORED_FOR_CANDIDATE = 5
NEAR_MISS_POINTS = 10.0
MAX_FALSE_SIGNAL_EXAMPLES = 5

THRESHOLD_DEPENDENT_SKIPS = frozenset({"unclear_market_direction", "insufficient_signal_strength"})
DATA_QUALITY_SKIPS = frozenset({"stale_market_data", "missing_required_quote", "invalid_quote"})
DIAGNOSTIC_LABEL = "diagnostic only — not applied; live and sandbox thresholds are unchanged"

_FIELDS = (
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
    "freshness_gate_passed",
    "followup_seconds",
    "direction_was_correct",
    "selected_symbol_move_pct",
    "iwm_move_pct",
    "vix_last",
    "raw_snapshot_json",
    "raw_score_json",
    "submitted",
    "production_execution_blocked",
    *SESSION_FIELDS,
    *(f"quote_age_{s.lower()}" for s in TRACKED_SYMBOLS),
    *(f"{s.lower()}_mid" for s in TRACKED_SYMBOLS),
    *(f"{s.lower()}_mid_after" for s in TRACKED_SYMBOLS),
)


class AnalyticsConfigError(ValueError):
    """Unsafe or invalid analytics/simulation arguments."""


# ---------------------------------------------------------------------------
# Row normalisation (read-only copies; ORM objects are never mutated)
# ---------------------------------------------------------------------------


def to_analysis_row(row: Any) -> Dict[str, Any]:
    if isinstance(row, ShadowCycleRecord):
        data = row.to_dict()
        data["raw_snapshot_json"] = row.raw_snapshot_json
        data["raw_score_json"] = row.raw_score_json
        source: Mapping[str, Any] = data
    elif isinstance(row, Mapping):
        source = row
    else:
        source = {name: getattr(row, name, None) for name in _FIELDS}
    out = {name: source.get(name) for name in _FIELDS}
    created = out.get("created_at")
    if created is not None and not isinstance(created, str):
        out["created_at"] = created.isoformat()
    return out


def load_rows(rows: Iterable[Any]) -> List[Dict[str, Any]]:
    items = [to_analysis_row(r) for r in rows]
    items.sort(key=lambda d: (d.get("created_at") or "", d.get("id") or 0))
    return items


def _avg(values: Iterable[Optional[float]], digits: int = 4) -> Optional[float]:
    clean = [float(v) for v in values if v is not None]
    return round(sum(clean) / len(clean), digits) if clean else None


def _pct(part: int, whole: int) -> Optional[float]:
    return round(part / whole * 100.0, 1) if whole else None


def followup_move(row: Mapping[str, Any], symbol: str) -> Optional[float]:
    if not row.get("followup_seconds"):
        return None
    key = symbol.lower()
    return pct_move(row.get(f"{key}_mid"), row.get(f"{key}_mid_after"))


def _json(raw: Any) -> Optional[Dict[str, Any]]:
    if not raw:
        return None
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _stored_thresholds(row: Mapping[str, Any]) -> Dict[str, float]:
    score = _json(row.get("raw_score_json")) or {}
    return dict(score.get("thresholds") or {})


# ---------------------------------------------------------------------------
# Actual-run analysis
# ---------------------------------------------------------------------------


def analyze_market_window(rows: Sequence[Mapping[str, Any]], *, min_move_pct: float) -> Dict[str, Any]:
    """Flat/choppy if most follow-up windows moved IWM less than min_move_pct (absolute)."""
    moves = [m for m in (followup_move(r, "IWM") for r in rows) if m is not None]
    flat = [m for m in moves if abs(m) < min_move_pct]
    significant = [m for m in moves if abs(m) >= min_move_pct]
    direction_changes = sum(
        1 for a, b in zip(significant, significant[1:]) if (a > 0) != (b > 0)
    )
    if not moves:
        status = "insufficient_data"
        recommendation = "run shadow mode with --followup-seconds > 0 to measure follow-up movement"
    elif len(flat) / len(moves) > FLAT_MAJORITY:
        status = "flat/choppy"
        recommendation = "collect more data during stronger movement before tuning"
    elif significant and direction_changes >= max(2, len(significant) // 2):
        status = "flat/choppy"
        recommendation = "moves keep reversing; collect more data during stronger movement before tuning"
    else:
        status = "moving"
        recommendation = "enough movement to compare thresholds (still diagnostic only)"
    return {
        "market_window_status": status,
        "recommendation": recommendation,
        "min_followup_move_pct": min_move_pct,
        "cycles_with_followup": len(moves),
        "flat_cycles": len(flat),
        "flat_pct": _pct(len(flat), len(moves)),
        "enough_movement_cycles": len(significant),
        "direction_changes": direction_changes,
        "avg_abs_iwm_move_pct": _avg(abs(m) for m in moves),
    }


def _score_profile(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    scored = [r for r in rows if r.get("freshness_gate_passed")]
    entry = _avg(_stored_thresholds(r).get("entry_score_threshold") for r in scored) or 70.0
    bulls = [float(r.get("bullish_score") or 0) for r in scored]
    bears = [float(r.get("bearish_score") or 0) for r in scored]
    best = [max(b, s) for b, s in zip(bulls, bears)]
    return {
        "gate_passed_cycles": len(scored),
        "avg_bullish_score": _avg(bulls, 2),
        "avg_bearish_score": _avg(bears, 2),
        "max_bullish_score": max(bulls) if bulls else None,
        "max_bearish_score": max(bears) if bears else None,
        "avg_score_gap": _avg((abs(b - s) for b, s in zip(bulls, bears)), 2),
        "stored_entry_threshold": entry,
        "near_miss_cycles": sum(1 for v in best if entry - NEAR_MISS_POINTS <= v < entry),
    }


def _diagnosis(summary: Mapping[str, Any]) -> str:
    total = summary["total_cycles"]
    if not total:
        return "no data"
    skips = summary["skip_reasons"]
    data_quality = sum(v for k, v in skips.items() if k in DATA_QUALITY_SKIPS)
    threshold_skips = sum(v for k, v in skips.items() if k in THRESHOLD_DEPENDENT_SKIPS)
    window = summary["market_window"]["market_window_status"]
    profile = summary["score_profile"]
    if data_quality / total > 0.5:
        return "mostly data-quality skips (stale/missing quotes); fix freshness before judging thresholds"
    quality = summary.get("market_window_quality") or {}
    if quality.get("market_window_quality") == "bad":
        return (
            "poor collection window (" + "; ".join(quality.get("reasons") or []) + "); "
            "collect during recommended regular-hours windows before tuning"
        )
    if window == "flat/choppy":
        return "market window was flat/choppy; skips are expected — collect data during stronger movement"
    if threshold_skips and profile["near_miss_cycles"] >= max(1, threshold_skips // 2):
        return "many near-miss scores; thresholds may be strict (diagnostic only, do not auto-loosen)"
    if threshold_skips:
        return "scores mostly far from thresholds; signals were genuinely weak/unclear"
    return "decisions were not dominated by threshold skips"


def analyze_rows(rows: Sequence[Mapping[str, Any]], *, min_move_pct: float = DEFAULT_MIN_FOLLOWUP_MOVE_PCT) -> Dict[str, Any]:
    decisions = {k: sum(1 for r in rows if r.get("decision") == k) for k in ("bullish", "bearish", "skip")}
    skips = [r for r in rows if r.get("decision") == "skip"]
    skip_reasons: Dict[str, int] = {}
    for r in skips:
        key = r.get("skip_reason") or "unknown"
        skip_reasons[key] = skip_reasons.get(key, 0) + 1

    skip_moves = {s: [followup_move(r, s) for r in skips] for s in ("IWM", "TNA", "TZA")}
    market_window = analyze_market_window(rows, min_move_pct=min_move_pct)
    summary: Dict[str, Any] = {
        "total_cycles": len(rows),
        "run_ids": sorted({str(r.get("run_id")) for r in rows if r.get("run_id")}),
        "decisions": decisions,
        "skip_reasons": dict(sorted(skip_reasons.items(), key=lambda kv: (-kv[1], kv[0]))),
        "freshness_pass_pct": _pct(sum(1 for r in rows if r.get("freshness_gate_passed")), len(rows)),
        "average_quote_age_seconds": {
            s: _avg((r.get(f"quote_age_{s.lower()}") for r in rows), 3) for s in TRACKED_SYMBOLS
        },
        "after_skips": {
            "skips_with_followup": sum(1 for m in skip_moves["IWM"] if m is not None),
            "avg_iwm_move_pct": _avg(skip_moves["IWM"]),
            "avg_abs_iwm_move_pct": _avg(abs(m) for m in skip_moves["IWM"] if m is not None),
            "avg_tna_move_pct": _avg(skip_moves["TNA"]),
            "avg_tza_move_pct": _avg(skip_moves["TZA"]),
            "enough_movement_after_skip": sum(
                1 for m in skip_moves["IWM"] if m is not None and abs(m) >= min_move_pct
            ),
        },
        "market_window": market_window,
        "session_breakdown": session_breakdown(rows),
        "market_window_quality": overall_market_window_quality(
            rows, market_window_status=market_window["market_window_status"]
        ),
        "score_profile": _score_profile(rows),
        "submitted_count": sum(1 for r in rows if r.get("submitted")),
    }
    summary["diagnosis"] = _diagnosis(summary)
    return summary


def compare_runs(rows: Sequence[Mapping[str, Any]], *, min_move_pct: float = DEFAULT_MIN_FOLLOWUP_MOVE_PCT) -> List[Dict[str, Any]]:
    by_run: Dict[str, List[Mapping[str, Any]]] = {}
    for r in rows:
        by_run.setdefault(str(r.get("run_id")), []).append(r)
    out = []
    for run_id, group in sorted(by_run.items(), key=lambda kv: kv[1][0].get("created_at") or ""):
        stats = analyze_rows(group, min_move_pct=min_move_pct)
        labels: Dict[str, int] = {}
        for r in group:
            label, _source = effective_session_label(r)
            labels[label] = labels.get(label, 0) + 1
        out.append(
            {
                "run_id": run_id,
                "cycles": stats["total_cycles"],
                "first_cycle_at": group[0].get("created_at"),
                "decisions": stats["decisions"],
                "top_skip_reason": next(iter(stats["skip_reasons"]), None),
                "freshness_pass_pct": stats["freshness_pass_pct"],
                "avg_abs_iwm_move_pct": stats["market_window"]["avg_abs_iwm_move_pct"],
                "market_window_status": stats["market_window"]["market_window_status"],
                "market_window_quality": stats["market_window_quality"]["market_window_quality"],
                "session_labels": labels,
                "near_miss_cycles": stats["score_profile"]["near_miss_cycles"],
            }
        )
    return out


# ---------------------------------------------------------------------------
# Offline threshold simulation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ThresholdSet:
    entry_score_threshold: float
    opposing_score_max: float
    min_score_gap: float

    @property
    def label(self) -> str:
        return f"entry={self.entry_score_threshold:g} opposing={self.opposing_score_max:g} gap={self.min_score_gap:g}"

    def safety_problem(self) -> Optional[str]:
        if not SAFE_MIN_ENTRY <= self.entry_score_threshold <= SAFE_MAX_ENTRY:
            return f"entry must be in [{SAFE_MIN_ENTRY:g}, {SAFE_MAX_ENTRY:g}]"
        if not 0 <= self.opposing_score_max <= self.entry_score_threshold - SAFE_OPPOSING_MARGIN:
            return f"opposing must be in [0, entry - {SAFE_OPPOSING_MARGIN:g}]"
        if self.min_score_gap < SAFE_MIN_SCORE_GAP:
            return f"gap must be >= {SAFE_MIN_SCORE_GAP:g}"
        return None

    def engine_config(self, max_quote_age_seconds: float) -> SignalEngineConfig:
        return replace(
            SignalEngineConfig(),
            max_quote_age_seconds=max_quote_age_seconds,
            entry_score_threshold=self.entry_score_threshold,
            opposing_score_max=self.opposing_score_max,
            min_score_gap=self.min_score_gap,
        ).validate()

    def to_dict(self) -> Dict[str, float]:
        return {
            "entry_score_threshold": self.entry_score_threshold,
            "opposing_score_max": self.opposing_score_max,
            "min_score_gap": self.min_score_gap,
        }


BASELINE = ThresholdSet(70.0, 40.0, 20.0)


def parse_float_list(raw: str, *, name: str) -> List[float]:
    try:
        values = [float(part) for part in str(raw).split(",") if part.strip()]
    except ValueError as exc:
        raise AnalyticsConfigError(f"{name}: not a comma-separated list of numbers") from exc
    if not values:
        raise AnalyticsConfigError(f"{name}: at least one value required")
    return sorted(set(values))


def build_threshold_grid(
    entries: Sequence[float],
    opposings: Sequence[float],
    gaps: Sequence[float],
) -> Tuple[List[ThresholdSet], List[Dict[str, Any]]]:
    """
    Values below the absolute safe minimums are an error (never simulated).
    Combinations that break the relative rule (opposing <= entry - 20) are
    rejected and reported, not silently adjusted.
    """
    problems = []
    if any(e < SAFE_MIN_ENTRY or e > SAFE_MAX_ENTRY for e in entries):
        problems.append(f"--min-entry values must be in [{SAFE_MIN_ENTRY:g}, {SAFE_MAX_ENTRY:g}]")
    if any(o < 0 for o in opposings):
        problems.append("--max-opposing values must be >= 0")
    if any(g < SAFE_MIN_SCORE_GAP for g in gaps):
        problems.append(f"--min-score-gap values must be >= {SAFE_MIN_SCORE_GAP:g}")
    if problems:
        raise AnalyticsConfigError("unsafe threshold candidates: " + "; ".join(problems))

    safe: List[ThresholdSet] = []
    rejected: List[Dict[str, Any]] = []
    for entry in entries:
        for opposing in opposings:
            for gap in gaps:
                candidate = ThresholdSet(float(entry), float(opposing), float(gap))
                problem = candidate.safety_problem()
                if problem:
                    rejected.append({**candidate.to_dict(), "reason": problem})
                else:
                    safe.append(candidate)
    if not safe:
        raise AnalyticsConfigError("no safe threshold combination to simulate")
    return safe, rejected


def _threshold_decision(bull: float, bear: float, t: ThresholdSet) -> Tuple[str, Optional[str]]:
    if bull >= t.entry_score_threshold and bear <= t.opposing_score_max:
        return SignalDirection.BULLISH.value, None
    if bear >= t.entry_score_threshold and bull <= t.opposing_score_max:
        return SignalDirection.BEARISH.value, None
    if abs(bull - bear) < t.min_score_gap:
        return SignalDirection.SKIP.value, "unclear_market_direction"
    return SignalDirection.SKIP.value, "insufficient_signal_strength"


def replay_decision(row: Mapping[str, Any], thresholds: ThresholdSet) -> Dict[str, Any]:
    """Hypothetical decision for one stored row. Never mutates the row."""
    stored = _stored_thresholds(row)
    max_age = float(stored.get("max_quote_age_seconds") or 1.0)
    snapshot_data = _json(row.get("raw_snapshot_json"))
    if snapshot_data and snapshot_data.get("quotes"):
        try:
            snapshot = MarketSnapshot.from_dict(snapshot_data)
            engine = TnaTzaSignalEngine(thresholds.engine_config(max_age), wall_clock=lambda: snapshot.created_at)
            decision = engine.decide(snapshot)
            return {
                "decision": decision.decision.value,
                "skip_reason": decision.skip_reason.value if decision.skip_reason else None,
                "bullish_score": decision.bullish_score,
                "bearish_score": decision.bearish_score,
                "method": "snapshot_replay",
            }
        except (KeyError, TypeError, ValueError):
            pass

    bull = float(row.get("bullish_score") or 0.0)
    bear = float(row.get("bearish_score") or 0.0)
    score = _json(row.get("raw_score_json")) or {}
    breakdown = score.get("score_breakdown")
    if isinstance(breakdown, dict):
        bull = float(breakdown.get("bullish_score", bull))
        bear = float(breakdown.get("bearish_score", bear))
    original_skip = row.get("skip_reason")
    if breakdown is None or (row.get("decision") == "skip" and original_skip not in THRESHOLD_DEPENDENT_SKIPS):
        return {
            "decision": SignalDirection.SKIP.value,
            "skip_reason": original_skip,
            "bullish_score": bull,
            "bearish_score": bear,
            "method": "stored_scores",
        }
    direction, reason = _threshold_decision(bull, bear, thresholds)
    return {"decision": direction, "skip_reason": reason, "bullish_score": bull, "bearish_score": bear, "method": "stored_scores"}


def score_strict_direction(direction: str, row: Mapping[str, Any]) -> Optional[bool]:
    """Same direction rule as the shadow report: any follow-up move counts, flat included.

    Bullish is correct when TNA or IWM rose. Bearish is correct when TZA rose or IWM fell.
    Both moves known and neither supporting the candidate is incorrect.
    A stale index follow-up is unscored.
    """
    if direction not in (SignalDirection.BULLISH.value, SignalDirection.BEARISH.value):
        return None
    if "unscored_due_to_stale_followup" in str(row.get("outcome_note") or ""):
        return None
    selected = followup_move(row, "TNA" if direction == SignalDirection.BULLISH.value else "TZA")
    iwm = followup_move(row, "IWM")
    if direction == SignalDirection.BULLISH.value:
        supported = (selected is not None and selected > 0) or (iwm is not None and iwm > 0)
    else:
        supported = (selected is not None and selected > 0) or (iwm is not None and iwm < 0)
    if supported:
        return True
    if selected is not None and iwm is not None:
        return False
    return None


def score_hypothetical(direction: str, row: Mapping[str, Any], *, min_move_pct: float) -> Optional[bool]:
    """Meaningful-move diagnostic. Flat follow-up below min_move_pct stays unscored.

    True if the selected ETF rose >= min move or IWM moved >= min move in the
    signal direction; False if something moved >= min move but not that way;
    None if no follow-up or everything moved less than min move.
    """
    if direction not in (SignalDirection.BULLISH.value, SignalDirection.BEARISH.value):
        return None
    selected = followup_move(row, "TNA" if direction == SignalDirection.BULLISH.value else "TZA")
    iwm = followup_move(row, "IWM")
    known = [m for m in (selected, iwm) if m is not None]
    if not known:
        return None
    iwm_directional = None if iwm is None else (iwm if direction == SignalDirection.BULLISH.value else -iwm)
    if (selected is not None and selected >= min_move_pct) or (iwm_directional is not None and iwm_directional >= min_move_pct):
        return True
    if max(abs(m) for m in known) < min_move_pct:
        return None
    return False


def _simulate_one(rows: Sequence[Mapping[str, Any]], t: ThresholdSet, *, min_move_pct: float) -> Dict[str, Any]:
    counts = {"bullish": 0, "bearish": 0, "skip": 0}
    skip_reasons: Dict[str, int] = {}
    methods: Dict[str, int] = {}
    scored = correct = 0
    false_signals: List[Dict[str, Any]] = []
    changed = 0
    for row in rows:
        result = replay_decision(row, t)
        counts[result["decision"]] += 1
        methods[result["method"]] = methods.get(result["method"], 0) + 1
        if result["decision"] != row.get("decision"):
            changed += 1
        if result["decision"] == "skip":
            key = result["skip_reason"] or "unknown"
            skip_reasons[key] = skip_reasons.get(key, 0) + 1
            continue
        verdict = score_hypothetical(result["decision"], row, min_move_pct=min_move_pct)
        if verdict is None:
            continue
        scored += 1
        if verdict:
            correct += 1
        elif len(false_signals) < MAX_FALSE_SIGNAL_EXAMPLES:
            false_signals.append(
                {
                    "run_id": row.get("run_id"),
                    "cycle_number": row.get("cycle_number"),
                    "created_at": row.get("created_at"),
                    "hypothetical_decision": result["decision"],
                    "hypothetical_symbol": "TNA" if result["decision"] == "bullish" else "TZA",
                    "bullish_score": result["bullish_score"],
                    "bearish_score": result["bearish_score"],
                    "selected_move_pct": followup_move(row, "TNA" if result["decision"] == "bullish" else "TZA"),
                    "iwm_move_pct": followup_move(row, "IWM"),
                }
            )
    return {
        "thresholds": t.to_dict(),
        "label": t.label,
        "is_current_default": t == BASELINE,
        "bullish_count": counts["bullish"],
        "bearish_count": counts["bearish"],
        "skip_count": counts["skip"],
        "skip_reasons": dict(sorted(skip_reasons.items())),
        "decisions_changed_vs_logged": changed,
        "scored_count": scored,
        "direction_correct_count": correct,
        "direction_incorrect_count": scored - correct,
        "correct_pct": _pct(correct, scored),
        "false_signal_examples": false_signals,
        "replay_methods": methods,
    }


def _candidate_rank(result: Mapping[str, Any]) -> Tuple:
    t = result["thresholds"]
    return (
        result["correct_pct"] or 0.0,
        result["scored_count"],
        t["entry_score_threshold"],  # stricter preferred on ties
        -t["opposing_score_max"],
        t["min_score_gap"],
    )


def simulate_thresholds(
    rows: Sequence[Mapping[str, Any]],
    grid: Sequence[ThresholdSet],
    *,
    min_move_pct: float = DEFAULT_MIN_FOLLOWUP_MOVE_PCT,
    rejected: Sequence[Mapping[str, Any]] = (),
    market_window_status: Optional[str] = None,
) -> Dict[str, Any]:
    """Hypothetical decisions only. Rows are read, never written; no order path exists here."""
    for t in grid:
        problem = t.safety_problem()
        if problem:
            raise AnalyticsConfigError(f"unsafe threshold set {t.label}: {problem}")

    results = [_simulate_one(rows, t, min_move_pct=min_move_pct) for t in grid]
    baseline = next((r for r in results if r["is_current_default"]), None) or _simulate_one(
        rows, BASELINE, min_move_pct=min_move_pct
    )
    replay_check = sum(
        1
        for row in rows
        if replay_decision(
            row,
            ThresholdSet(
                float(_stored_thresholds(row).get("entry_score_threshold", BASELINE.entry_score_threshold)),
                float(_stored_thresholds(row).get("opposing_score_max", BASELINE.opposing_score_max)),
                float(_stored_thresholds(row).get("min_score_gap", BASELINE.min_score_gap)),
            ),
        )["decision"]
        != row.get("decision")
    )

    eligible = [r for r in results if r["scored_count"] >= MIN_SCORED_FOR_CANDIDATE]
    if eligible:
        best = max(eligible, key=_candidate_rank)
        best_candidate: Dict[str, Any] = {
            "thresholds": best["thresholds"],
            "label": best["label"],
            "correct_pct": best["correct_pct"],
            "scored_count": best["scored_count"],
            "trade_signals": best["bullish_count"] + best["bearish_count"],
            "reason": "highest hypothetical direction-correct % among sets with enough scored samples",
        }
    else:
        best_candidate = {
            "thresholds": None,
            "label": None,
            "reason": (
                f"insufficient scored follow-up samples (need >= {MIN_SCORED_FOR_CANDIDATE} "
                "hypothetical signals with meaningful follow-up movement)"
            ),
        }
    best_candidate.update({"diagnostic_only": True, "applied": False, "note": DIAGNOSTIC_LABEL})
    if market_window_status == "flat/choppy":
        best_candidate["warning"] = "market window was flat/choppy; do not tune thresholds from this data"

    return {
        "rows_evaluated": len(rows),
        "min_followup_move_pct": min_move_pct,
        "sets_evaluated": len(results),
        "rejected_unsafe_sets": list(rejected),
        "baseline": baseline,
        "results": results,
        "best_candidate": best_candidate,
        "replay_mismatches_with_logged_thresholds": replay_check,
        "writes_to_database": False,
        "orders_submitted": 0,
        "note": "hypothetical decisions from stored snapshots; " + DIAGNOSTIC_LABEL,
    }
