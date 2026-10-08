"""
Offline comparison of signal profiles (Checkpoint 2.17A). Analytics only.

Replays stored shadow snapshots through conservative_v2 and/or
balanced_v3_shadow. Nothing is written back, no setting is changed, and this
module has no broker, order, or execution access.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping, Optional, Sequence

from backend.shadow_mode.analytics import followup_move, score_hypothetical, score_strict_direction
from backend.shadow_mode.engine_replay import engine_config_for_row
from backend.signals.candidate_engine_v3 import CandidateEngineV3, CandidateEngineV3Config
from backend.signals.models import SIGNAL_ENGINE_VERSION, MarketSnapshot, SignalDirection
from backend.signals.profiles import BALANCED_V3_SHADOW, CONSERVATIVE_V2, normalize_signal_profile
from backend.signals.tna_tza_signal_engine import TnaTzaSignalEngine

TRADE_DECISIONS = (SignalDirection.BULLISH.value, SignalDirection.BEARISH.value)
SKIP = SignalDirection.SKIP.value
FRESHNESS_SKIPS = frozenset({"stale_market_data", "missing_required_quote"})
MAX_EXAMPLES = 5
REPLAY_NOTE = "hypothetical profile replay — not written to the database, no orders, not applied to live trading"


def _json(raw: Any) -> Dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def row_matches_session_dates(row: Mapping[str, Any], session_dates: Sequence[str]) -> bool:
    created = str(row.get("created_at") or "")
    day = created[:10]
    run_id = str(row.get("run_id") or "")
    for raw in session_dates:
        stamp = str(raw)[:10]
        if day == stamp or stamp in run_id or stamp.replace("-", "") in run_id:
            return True
    return False


def parse_profile_selection(raw: Optional[str]) -> List[str]:
    """
    Profiles to add as a comparison section.

    None keeps the historical v2 replay output unchanged.
    'both' compares conservative_v2 and balanced_v3_shadow.
    """
    if raw is None or not str(raw).strip():
        return []
    text = str(raw).strip().lower()
    if text in {"both", "all"}:
        return [CONSERVATIVE_V2, BALANCED_V3_SHADOW]
    names: List[str] = []
    for part in text.split(","):
        if not part.strip():
            continue
        name = normalize_signal_profile(part)
        if name not in names:
            names.append(name)
    return names


def _ordered_rows(rows: Sequence[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    return sorted(rows, key=lambda row: (str(row.get("run_id") or ""), row.get("cycle_number") or 0, str(row.get("created_at") or "")))


def replay_profile_row(
    row: Mapping[str, Any],
    profile: str,
    *,
    previous_direction: Optional[str] = None,
    apply_path_filters: bool = True,
) -> Dict[str, Any]:
    """One hypothetical decision. Does not mutate the row."""
    selected = normalize_signal_profile(profile)
    snapshot_data = _json(row.get("raw_snapshot_json"))
    if not snapshot_data.get("quotes"):
        return {"replayed": False, "reason": "no stored snapshot", "profile": selected}
    try:
        snapshot = MarketSnapshot.from_dict(snapshot_data)
        if selected == CONSERVATIVE_V2:
            engine = TnaTzaSignalEngine(engine_config_for_row(row), wall_clock=lambda: snapshot.created_at)
        else:
            age = engine_config_for_row(row).max_quote_age_seconds
            engine = CandidateEngineV3(
                CandidateEngineV3Config.from_settings(max_quote_age_seconds=age),
                wall_clock=lambda: snapshot.created_at,
                mode="replay",
            )
        if selected == CONSERVATIVE_V2:
            decision = engine.decide(snapshot)
        else:
            decision = engine.decide(
                snapshot,
                previous_direction=previous_direction,
                apply_path_filters=apply_path_filters,
            )
    except (KeyError, TypeError, ValueError) as exc:
        return {"replayed": False, "reason": f"snapshot replay failed ({type(exc).__name__})", "profile": selected}
    scores = dict(decision.profile_scores or {})
    symbol = decision.selected_symbol
    return {
        "replayed": True,
        "profile": selected,
        "decision": decision.decision.value,
        "selected_symbol": symbol or ("TNA" if decision.decision is SignalDirection.BULLISH else "TZA" if decision.decision is SignalDirection.BEARISH else None),
        "skip_reason": decision.skip_reason.value if decision.skip_reason else None,
        "candidate_score": scores.get("candidate_score", decision.confidence_score),
        "profile_scores": scores,
        "engine_version": decision.engine_version,
    }


def _example(item: Mapping[str, Any], kind: str) -> Dict[str, Any]:
    scores = item.get("profile_scores") or {}
    return {
        "example_kind": kind,
        "run_id": item.get("run_id"),
        "cycle_number": item.get("cycle_number"),
        "decision": item.get("decision"),
        "selected_symbol": item.get("selected_symbol"),
        "skip_reason": item.get("skip_reason"),
        "hypothetical_correct": item.get("correct"),
        "iwm_followup_move_pct": item.get("iwm_followup"),
        "profile": scores.get("profile") or item.get("profile"),
        "candidate_score": scores.get("candidate_score", item.get("candidate_score")),
        "direction": scores.get("direction") or item.get("decision"),
        "iwm_momentum_score": scores.get("iwm_momentum_score"),
        "pair_confirmation_score": scores.get("pair_confirmation_score"),
        "relative_strength_score": scores.get("relative_strength_score"),
        "broad_window_score": scores.get("broad_window_score"),
        "vix_score": scores.get("vix_score"),
        "chop_risk_score": scores.get("chop_risk_score"),
        "pullback_risk_score": scores.get("pullback_risk_score"),
        "final_reason": scores.get("final_reason") or item.get("skip_reason"),
    }


def summarize_profile(
    rows: Sequence[Mapping[str, Any]],
    profile: str,
    *,
    min_move_pct: float,
) -> Dict[str, Any]:
    selected = normalize_signal_profile(profile)
    items: List[Dict[str, Any]] = []
    previous_by_run: Dict[str, Optional[str]] = {}
    for row in _ordered_rows(rows):
        run_id = str(row.get("run_id") or "")
        previous = previous_by_run.get(run_id) if selected == BALANCED_V3_SHADOW else None
        replayed = replay_profile_row(row, selected, previous_direction=previous, apply_path_filters=True)
        decision = replayed.get("decision") if replayed.get("replayed") else SKIP
        items.append(
            {
                "run_id": row.get("run_id"),
                "cycle_number": row.get("cycle_number"),
                "profile": selected,
                "decision": decision,
                "selected_symbol": replayed.get("selected_symbol"),
                "skip_reason": replayed.get("skip_reason"),
                "candidate_score": replayed.get("candidate_score"),
                "profile_scores": replayed.get("profile_scores") or {},
                "correct": score_strict_direction(str(decision), row),
                "meaningful_correct": score_hypothetical(str(decision), row, min_move_pct=min_move_pct),
                "iwm_followup": followup_move(row, "IWM"),
                "replayed": bool(replayed.get("replayed")),
            }
        )
        if selected == BALANCED_V3_SHADOW:
            previous_by_run[run_id] = decision if decision in TRADE_DECISIONS else None

    candidates = [item for item in items if item["decision"] in TRADE_DECISIONS]
    scored = [item for item in candidates if item["correct"] is not None]
    correct = [item for item in scored if item["correct"] is True]
    incorrect = [item for item in scored if item["correct"] is False]
    meaningful = [item for item in candidates if item["meaningful_correct"] is not None]
    meaningful_correct = [item for item in meaningful if item["meaningful_correct"] is True]
    meaningful_incorrect = [item for item in meaningful if item["meaningful_correct"] is False]
    missed = [
        item
        for item in items
        if item["decision"] == SKIP
        and item.get("skip_reason") not in FRESHNESS_SKIPS
        and item.get("iwm_followup") is not None
        and abs(float(item["iwm_followup"])) >= min_move_pct
    ]
    examples: List[Dict[str, Any]] = []
    for kind, group in (
        ("candidate", candidates),
        ("false_candidate", incorrect),
        ("missed_opportunity", missed),
    ):
        for item in group[:MAX_EXAMPLES]:
            examples.append(_example(item, kind))

    scored_count = len(scored)
    correct_count = len(correct)
    return {
        "profile": selected,
        "engine_version": BALANCED_V3_SHADOW if selected == BALANCED_V3_SHADOW else SIGNAL_ENGINE_VERSION,
        "rows": len(items),
        "candidate_count": len(candidates),
        "tna_count": sum(1 for item in items if item["decision"] == SignalDirection.BULLISH.value),
        "tza_count": sum(1 for item in items if item["decision"] == SignalDirection.BEARISH.value),
        "skip_count": sum(1 for item in items if item["decision"] == SKIP),
        "scored_count": scored_count,
        "correct_count": correct_count,
        "incorrect_count": len(incorrect),
        "correct_pct": round(correct_count / scored_count * 100.0, 1) if scored_count else None,
        "strict_scored_count": scored_count,
        "strict_correct": correct_count,
        "strict_incorrect": len(incorrect),
        "strict_correct_pct": round(correct_count / scored_count * 100.0, 1) if scored_count else None,
        "meaningful_scored_count": len(meaningful),
        "meaningful_correct": len(meaningful_correct),
        "meaningful_incorrect": len(meaningful_incorrect),
        "meaningful_correct_pct": round(len(meaningful_correct) / len(meaningful) * 100.0, 1) if meaningful else None,
        "false_candidates": len(incorrect),
        "missed_opportunities": len(missed),
        "examples": examples,
        "orders_submitted": 0,
        "writes_to_database": False,
        "applied_to_live": False,
        "shadow_only": selected == BALANCED_V3_SHADOW,
    }


def _sign_failure(replayed: Mapping[str, Any]) -> bool:
    """TNA with a falling IWM window, or TZA with a rising IWM window."""
    decision = replayed.get("decision")
    if decision not in TRADE_DECISIONS:
        return False
    raw = (replayed.get("profile_scores") or {}).get("window_final_move")
    if raw is None:
        return False
    move = float(raw)
    if decision == SignalDirection.BULLISH.value and move < 0:
        return True
    if decision == SignalDirection.BEARISH.value and move > 0:
        return True
    return False


def _correct_pct(flags: Sequence[Optional[bool]]) -> Optional[float]:
    scored = [flag for flag in flags if flag is not None]
    if not scored:
        return None
    return round(sum(1 for flag in scored if flag) / len(scored) * 100.0, 1)


def confirmation_comparison(
    rows: Sequence[Mapping[str, Any]],
    *,
    min_move_pct: float,
) -> Dict[str, Any]:
    """Before/after path and flip filters. Sign, momentum tiers, and adaptive entry apply to both passes."""
    previous_by_run: Dict[str, Optional[str]] = {}
    before_count = after_count = 0
    false_filtered = good_preserved = missed_winners = 0
    sign_failures = late_reversals = flip_cooldowns = 0
    iwm_rejected = adaptive_rejected = 0
    before_flags: List[Optional[bool]] = []
    after_flags: List[Optional[bool]] = []
    for row in _ordered_rows(rows):
        run_id = str(row.get("run_id") or "")
        before = replay_profile_row(row, BALANCED_V3_SHADOW, previous_direction=None, apply_path_filters=False)
        after = replay_profile_row(
            row,
            BALANCED_V3_SHADOW,
            previous_direction=previous_by_run.get(run_id),
            apply_path_filters=True,
        )
        before_decision = before.get("decision") if before.get("replayed") else SKIP
        after_decision = after.get("decision") if after.get("replayed") else SKIP
        if before_decision in TRADE_DECISIONS:
            before_count += 1
            before_flags.append(score_hypothetical(str(before_decision), row, min_move_pct=min_move_pct))
        if after_decision in TRADE_DECISIONS:
            after_count += 1
            after_flags.append(score_hypothetical(str(after_decision), row, min_move_pct=min_move_pct))
        if _sign_failure(before) or _sign_failure(after):
            sign_failures += 1
        if after.get("skip_reason") == "late_reversal_risk":
            late_reversals += 1
        if after.get("skip_reason") == "direction_flip_cooldown":
            flip_cooldowns += 1
        stage = (after.get("profile_scores") or {}).get("rejection_stage")
        if stage == "iwm_threshold":
            iwm_rejected += 1
        elif stage == "adaptive_entry":
            adaptive_rejected += 1
        before_correct = score_hypothetical(str(before_decision), row, min_move_pct=min_move_pct)
        if before_decision in TRADE_DECISIONS and after_decision == SKIP and before_correct is False:
            false_filtered += 1
        if before_decision in TRADE_DECISIONS and after_decision == before_decision and before_correct is True:
            good_preserved += 1
        if before_decision in TRADE_DECISIONS and before_correct is True and after_decision == SKIP:
            missed_winners += 1
        previous_by_run[run_id] = after_decision if after_decision in TRADE_DECISIONS else None
    return {
        "candidates_before_filters": before_count,
        "candidates_after_filters": after_count,
        "candidates_rejected_by_iwm_threshold": iwm_rejected,
        "candidates_rejected_by_late_reversal": late_reversals,
        "candidates_rejected_by_adaptive_entry": adaptive_rejected,
        "false_candidates_filtered": false_filtered,
        "good_candidates_preserved": good_preserved,
        "missed_winners": missed_winners,
        "correct_pct_before_filters": _correct_pct(before_flags),
        "correct_pct_after_filters": _correct_pct(after_flags),
        "direction_sign_failures": sign_failures,
        "late_reversal_risk_count": late_reversals,
        "direction_flip_cooldown_count": flip_cooldowns,
    }


def compare_signal_profiles(
    rows: Sequence[Mapping[str, Any]],
    profiles: Sequence[str],
    *,
    min_move_pct: float,
) -> Dict[str, Any]:
    selected = [normalize_signal_profile(name) for name in profiles]
    profiles_report = {}
    for name in selected:
        stats = summarize_profile(rows, name, min_move_pct=min_move_pct)
        if name == BALANCED_V3_SHADOW:
            stats.update(confirmation_comparison(rows, min_move_pct=min_move_pct))
        profiles_report[name] = stats
    return {
        "profiles": profiles_report,
        "orders_submitted": 0,
        "writes_to_database": False,
        "applied_to_live": False,
        "note": REPLAY_NOTE,
    }
