"""
Multi-session validation dataset (Checkpoint 2.22). Read-only.

Rebuilds balanced_v3_shadow candidates from stored shadow rows and attaches
follow-up labels for later review. Continuation and quality ranking are
recorded beside the candidate. They do not change the decision.

Follow-up prices are labels only. They are never passed into the signal
engine, continuation scoring, or rank_candidate().

orders_submitted: 0
writes_to_database: false
analytics_only: true
"""

from __future__ import annotations

import json
from datetime import datetime, time, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from backend.shadow_mode.analytics import followup_move, pct_move
from backend.shadow_mode.profile_replay import _ordered_rows, _remember_direction, replay_profile_row
from backend.shadow_mode.session_quality import BAD_SESSION_LABELS, effective_session_label, group_stats, window_quality
from backend.shadow_mode.semiconductor_context import context_from_row, summarize_semiconductor
from backend.shadow_mode.stream_readiness import summarize_liveness
from backend.signals.candidate_quality_ranker import rank_candidate
from backend.signals.models import MarketSnapshot, SymbolQuote
from backend.signals.predictive_continuation import continuation_entry_block, evaluate_predictive_continuation
from backend.signals.profiles import BALANCED_V3_SHADOW

ORDERS_SUBMITTED = 0
WRITES_TO_DATABASE = False
ANALYTICS_ONLY = True
MINIMUM_LABELED_CANDIDATES = 20
PRELIMINARY_LABELED_CANDIDATES = 50
HORIZONS = (30, 60, 120)
_NY = ZoneInfo("America/New_York")
_OPEN = time(9, 30)
_CLOSE = time(16, 0)
TRADE_DECISIONS = frozenset({"bullish", "bearish"})
NOTE = (
    "validation dataset only — candidates are the current balanced_v3_shadow "
    "decisions; continuation and ranking are labels, not filters; "
    "no orders and no database writes"
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


def _session_date(row: Mapping[str, Any], created: Optional[datetime]) -> Optional[str]:
    explicit = row.get("session_date")
    if explicit:
        return str(explicit)[:10]
    if created is None:
        return None
    return created.astimezone(_NY).date().isoformat()


def _clock(row: Mapping[str, Any], created: Optional[datetime]) -> Tuple[Optional[bool], Optional[float], Optional[float], str]:
    label, _source = effective_session_label(row)
    regular = label == "regular_hours"
    after_open = None
    before_close = row.get("session_minutes_to_close")
    if before_close is not None:
        before_close = float(before_close)
    if created is not None:
        local = created.astimezone(_NY)
        opened = local.replace(hour=_OPEN.hour, minute=_OPEN.minute, second=0, microsecond=0)
        closed = local.replace(hour=_CLOSE.hour, minute=_CLOSE.minute, second=0, microsecond=0)
        after_open = round((local - opened).total_seconds() / 60.0, 1)
        if before_close is None:
            before_close = round((closed - local).total_seconds() / 60.0, 1)
    return regular, after_open, before_close, label


def _day_part(regular: Optional[bool], after_open: Optional[float], before_close: Optional[float], label: str) -> str:
    if label not in {"regular_hours", "near_close"} and regular is not True:
        return "outside_regular"
    if after_open is not None and after_open < 60:
        return "first_hour"
    if before_close is not None and before_close <= 60:
        return "final_hour"
    return "midday"


def _samples(quote: Optional[SymbolQuote]) -> List[Tuple[float, float]]:
    if quote is None:
        return []
    cleaned: List[Tuple[float, float]] = []
    for t, mid in quote.mid_path or ():
        if mid is None or mid <= 0:
            continue
        point = (float(t), float(mid))
        if cleaned and abs(cleaned[-1][0] - point[0]) <= 1e-6:
            cleaned[-1] = point
        else:
            cleaned.append(point)
    if quote.first_mid and quote.first_mid > 0 and (not cleaned or cleaned[0][0] > 0.25):
        cleaned.insert(0, (0.0, float(quote.first_mid)))
    if quote.mid and quote.mid > 0:
        if not cleaned:
            cleaned.append((0.0, float(quote.mid)))
        elif abs(cleaned[-1][1] - float(quote.mid)) / float(quote.mid) > 1e-5:
            cleaned.append((cleaned[-1][0] + 0.001, float(quote.mid)))
    return cleaned


def _price_at(samples: Sequence[Tuple[float, float]], target: float) -> float:
    if target <= samples[0][0]:
        return samples[0][1]
    if target >= samples[-1][0]:
        return samples[-1][1]
    for (t0, p0), (t1, p1) in zip(samples, samples[1:]):
        if t0 <= target <= t1 and t1 != t0:
            weight = (target - t0) / (t1 - t0)
            return p0 + (p1 - p0) * weight
    return samples[-1][1]


def _raw_move(later: Optional[float], earlier: Optional[float]) -> Optional[float]:
    return pct_move(earlier, later)


def _window_moves(quote: Optional[SymbolQuote]) -> Dict[str, Optional[float]]:
    samples = _samples(quote)
    empty = {"full": None, "first": None, "middle": None, "final": None, "final_10s": None}
    if len(samples) < 2:
        return empty
    t0, p0 = samples[0]
    t1, p1 = samples[-1]
    span = t1 - t0
    if span <= 0 or p0 <= 0:
        return empty
    first_end = _price_at(samples, t0 + span / 3.0)
    mid_end = _price_at(samples, t0 + 2.0 * span / 3.0)
    final_anchor = _price_at(samples, max(t0, t1 - 10.0))
    return {
        "full": _raw_move(p1, p0),
        "first": _raw_move(first_end, p0),
        "middle": _raw_move(mid_end, first_end),
        "final": _raw_move(p1, mid_end),
        "final_10s": _raw_move(p1, final_anchor),
    }


def _vix(snapshot: MarketSnapshot) -> Tuple[Optional[float], Optional[float]]:
    quote = snapshot.get("VIX")
    if quote is None:
        return None, None
    level = quote.price
    change = pct_move(quote.prev_close, level) if level is not None else None
    return level, change


def _direction_correct(direction: str, selected_move: Optional[float], iwm_move: Optional[float]) -> Optional[bool]:
    if selected_move is None and iwm_move is None:
        return None
    if direction == "bullish":
        supported = (selected_move is not None and selected_move > 0) or (iwm_move is not None and iwm_move > 0)
    else:
        supported = (selected_move is not None and selected_move > 0) or (iwm_move is not None and iwm_move < 0)
    if supported:
        return True
    if selected_move is not None and iwm_move is not None:
        return False
    return None


def _meaningful(
    direction: str,
    selected_move: Optional[float],
    iwm_move: Optional[float],
    *,
    min_move_pct: float,
) -> Optional[bool]:
    iwm_dir = None if iwm_move is None else (iwm_move if direction == "bullish" else -iwm_move)
    if (selected_move is not None and selected_move >= min_move_pct) or (
        iwm_dir is not None and iwm_dir >= min_move_pct
    ):
        return True
    known = [move for move in (selected_move, iwm_dir) if move is not None]
    if any(abs(move) >= min_move_pct for move in known):
        return False
    return None


def _explicit_move(row: Mapping[str, Any], symbol: str, horizon: int) -> Optional[float]:
    direct = row.get(f"{symbol.lower()}_move_{horizon}s")
    if direct is not None:
        return float(direct)
    after = row.get(f"{symbol.lower()}_mid_after_{horizon}")
    if after is None:
        nested = _json(row.get("followups")).get(str(horizon), {})
        if isinstance(nested, Mapping):
            after = nested.get(symbol) or nested.get(symbol.lower())
    if after is None:
        return None
    return pct_move(row.get(f"{symbol.lower()}_mid"), after)


def _horizon_moves(row: Mapping[str, Any], symbol: str) -> Dict[int, Tuple[Optional[float], Optional[float]]]:
    moves = {horizon: (_explicit_move(row, symbol, horizon), _explicit_move(row, "IWM", horizon)) for horizon in HORIZONS}
    seconds = row.get("followup_seconds")
    stale = "unscored_due_to_stale_followup" in str(row.get("outcome_note") or "")
    if seconds and not stale:
        nearest = min(HORIZONS, key=lambda horizon: abs(horizon - float(seconds)))
        if abs(nearest - float(seconds)) <= 5 and moves[nearest] == (None, None):
            moves[nearest] = (followup_move(row, symbol), followup_move(row, "IWM"))
    return moves


def _dedupe(rows: Sequence[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    seen = set()
    unique: List[Mapping[str, Any]] = []
    for row in _ordered_rows(rows):
        key = (str(row.get("run_id") or ""), row.get("cycle_number"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return unique


def _excluded_indexes(rows: Sequence[Mapping[str, Any]]) -> set:
    groups: Dict[Tuple[str, str], List[int]] = {}
    for index, row in enumerate(rows):
        label, _source = effective_session_label(row)
        groups.setdefault((str(row.get("run_id") or ""), label), []).append(index)
    excluded = set()
    for (_run_id, label), indexes in groups.items():
        group = [rows[i] for i in indexes]
        stats = group_stats(group)
        has_freshness = any(row.get("freshness_gate_passed") is not None for row in group)
        quality, _reasons = window_quality(label, stats["freshness_pass_pct"] if has_freshness else 100.0)
        if label in BAD_SESSION_LABELS or (has_freshness and quality == "bad"):
            excluded.update(indexes)
    return excluded


def _score_band(score: Optional[float]) -> str:
    if score is None:
        return "missing"
    if score < 65:
        return "below_65"
    if score < 70:
        return "65_to_70"
    if score < 80:
        return "70_to_80"
    if score < 90:
        return "80_to_90"
    return "90_plus"


def _probability_band(probability: Optional[float]) -> str:
    if probability is None:
        return "missing"
    if probability < 40:
        return "below_40"
    if probability < 55:
        return "40_to_55"
    if probability < 70:
        return "55_to_70"
    return "70_plus"


def _vix_band(level: Optional[float]) -> str:
    if level is None:
        return "missing"
    if level < 20:
        return "below_20"
    if level < 25:
        return "20_to_25"
    return "25_plus"


def _accuracy(flags: Iterable[Optional[bool]]) -> Dict[str, Any]:
    correct = incorrect = 0
    for flag in flags:
        if flag is True:
            correct += 1
        elif flag is False:
            incorrect += 1
    scored = correct + incorrect
    return {
        "count": scored,
        "correct": correct,
        "incorrect": incorrect,
        "accuracy": round(correct / scored * 100.0, 1) if scored else None,
    }


def _grouped(records: Sequence[Mapping[str, Any]], key_name: str, horizon: int) -> Dict[str, Any]:
    groups: Dict[str, List[Optional[bool]]] = {}
    field = f"direction_correct_{horizon}s"
    for record in records:
        groups.setdefault(str(record.get(key_name) or "missing"), []).append(record.get(field))
    return {name: _accuracy(flags) for name, flags in sorted(groups.items())}


def _primary_correct(record: Mapping[str, Any]) -> Optional[bool]:
    for horizon in (60, 30, 120):
        flag = record.get(f"direction_correct_{horizon}s")
        if flag is not None:
            return flag
    return None


def _filter_tally(records: Sequence[Mapping[str, Any]], accepted_key: str) -> Dict[str, Any]:
    accepted = rejected = 0
    winners_accepted = losers_accepted = winners_rejected = losers_rejected = 0
    for record in records:
        kept = bool(record.get(accepted_key))
        flag = _primary_correct(record)
        if kept:
            accepted += 1
            if flag is True:
                winners_accepted += 1
            elif flag is False:
                losers_accepted += 1
        else:
            rejected += 1
            if flag is True:
                winners_rejected += 1
            elif flag is False:
                losers_rejected += 1
    total = accepted + rejected
    return {
        "accepted": accepted,
        "rejected": rejected,
        "winners_accepted": winners_accepted,
        "losers_accepted": losers_accepted,
        "winners_rejected": winners_rejected,
        "losers_rejected": losers_rejected,
        "retention_pct": round(accepted / total * 100.0, 1) if total else None,
    }


def _sample_status(labeled: int) -> str:
    if labeled < MINIMUM_LABELED_CANDIDATES:
        return "insufficient"
    if labeled < PRELIMINARY_LABELED_CANDIDATES:
        return "preliminary"
    return "useful_for_tuning"


def _avg(values: Iterable[Optional[float]]) -> Optional[float]:
    clean = [float(value) for value in values if value is not None]
    return round(sum(clean) / len(clean), 3) if clean else None


def build_validation_dataset(
    rows: Sequence[Mapping[str, Any]],
    *,
    min_move_pct: float = 0.03,
    exclude_invalid: bool = True,
) -> Dict[str, Any]:
    """Candidates from the current shadow engine, plus label-only follow-ups.

    Input rows are not modified. Nothing is written.
    """
    unique = _dedupe(rows)
    excluded_indexes = _excluded_indexes(unique) if exclude_invalid else set()
    included = [row for index, row in enumerate(unique) if index not in excluded_indexes]
    excluded_runs = sorted({str(unique[index].get("run_id") or "") for index in excluded_indexes})

    run_meta: Dict[str, Dict[str, Any]] = {}
    by_run: Dict[str, List[Mapping[str, Any]]] = {}
    for row in included:
        by_run.setdefault(str(row.get("run_id") or ""), []).append(row)
    for run_id, group in by_run.items():
        stats = group_stats(group)
        labels = [effective_session_label(row)[0] for row in group]
        label = max(set(labels), key=labels.count) if labels else "unknown"
        has_freshness = any(row.get("freshness_gate_passed") is not None for row in group)
        freshness = stats["freshness_pass_pct"] if has_freshness else None
        quality, reasons = window_quality(label, 100.0 if freshness is None else freshness)
        ages = [
            row.get(f"quote_age_{symbol.lower()}")
            for row in group
            for symbol in ("TNA", "TZA", "IWM", "SPY", "QQQ", "VIX")
        ]
        created = _parse_created(group[0].get("created_at"))
        run_meta[run_id] = {
            "session_date": _session_date(group[0], created),
            "freshness_pass_pct": stats["freshness_pass_pct"] if has_freshness else None,
            "average_quote_age": _avg(ages),
            "market_window_quality": quality,
            "quality_reasons": reasons,
            "regular_hours": label == "regular_hours",
        }

    records: List[Dict[str, Any]] = []
    previous_by_run: Dict[str, Optional[str]] = {}
    last_taken_by_run: Dict[str, Optional[str]] = {}
    for row in included:
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
        scores = replayed.get("profile_scores") or {}
        candidate_score = scores.get("candidate_score", replayed.get("candidate_score"))
        # Real-time models receive the snapshot and the candidate only.
        continuation = evaluate_predictive_continuation(snapshot, str(symbol))
        ranked = rank_candidate(
            snapshot,
            str(symbol),
            candidate_score=None if candidate_score is None else float(candidate_score),
        )
        continuation_block = continuation_entry_block(continuation, vix_penalty=False)
        iwm = _window_moves(snapshot.get("IWM"))
        tna = _window_moves(snapshot.get("TNA"))["full"]
        tza = _window_moves(snapshot.get("TZA"))["full"]
        spy = _window_moves(snapshot.get("SPY"))["full"]
        qqq = _window_moves(snapshot.get("QQQ"))["full"]
        if decision == "bullish":
            pair_confirmation = tna is not None and tza is not None and tna > 0 and tza < 0
        else:
            pair_confirmation = tza is not None and tna is not None and tza > 0 and tna < 0
        level, change = _vix(snapshot)
        created = _parse_created(row.get("created_at"))
        regular, after_open, before_close, label = _clock(row, created)
        meta = run_meta.get(run_id, {})
        horizons = _horizon_moves(row, str(symbol))
        record: Dict[str, Any] = {
            "run_id": run_id,
            "session_date": _session_date(row, created) or meta.get("session_date"),
            "cycle": row.get("cycle_number"),
            "timestamp": created.isoformat() if created else row.get("created_at"),
            "symbol": symbol,
            "direction": decision,
            "candidate_score": candidate_score,
            "decision_reason": scores.get("final_reason") or decision,
            "iwm_full_window_move": iwm["full"],
            "iwm_first_third_move": iwm["first"],
            "iwm_middle_third_move": iwm["middle"],
            "iwm_final_third_move": iwm["final"],
            "iwm_final_10s_move": iwm["final_10s"],
            "tna_move": tna,
            "tza_move": tza,
            "pair_confirmation": pair_confirmation,
            "spy_move": spy,
            "qqq_move": qqq,
            "vix": level,
            "vix_change": change,
            "pullback_risk": scores.get("pullback_risk_score"),
            "chop_risk": scores.get("chop_risk_score"),
            "late_reversal": bool(scores.get("final_10s_reversal")),
            "direction_flip": False,
            "same_direction_reentry": False,
            "predictive_continuation_probability": None if continuation is None else continuation.continuation_probability,
            "predictive_momentum_phase": None if continuation is None else continuation.momentum_phase,
            "continuation_analytics_accepted": continuation_block is None,
            "candidate_quality_score": None if ranked is None else ranked.quality_score,
            "quality_tier": None if ranked is None else ranked.quality_tier.value,
            "ranking_analytics_accepted": bool(ranked and ranked.rank_passed),
            "regular_hours": regular if row.get("session_label") or row.get("session_is_regular_hours") is not None else meta.get("regular_hours"),
            "minutes_after_open": after_open,
            "minutes_before_close": before_close,
            "day_part": _day_part(regular, after_open, before_close, label),
            "freshness_pass_pct": meta.get("freshness_pass_pct"),
            "average_quote_age": meta.get("average_quote_age"),
            "market_window_quality": meta.get("market_window_quality"),
            "score_band": _score_band(None if candidate_score is None else float(candidate_score)),
            "vix_band": _vix_band(level),
            "probability_band": _probability_band(None if continuation is None else continuation.continuation_probability),
            "semiconductor_context": context_from_row(row),
        }
        if row.get("session_is_regular_hours") is not None:
            record["regular_hours"] = bool(row.get("session_is_regular_hours"))
        for horizon in HORIZONS:
            selected_move, iwm_move = horizons[horizon]
            record[f"selected_etf_move_{horizon}s"] = selected_move
            record[f"iwm_move_{horizon}s"] = iwm_move
            record[f"direction_correct_{horizon}s"] = _direction_correct(decision, selected_move, iwm_move)
            record[f"meaningful_move_{horizon}s"] = _meaningful(
                decision, selected_move, iwm_move, min_move_pct=min_move_pct
            )
        records.append(record)

    labeled = {horizon: sum(1 for record in records if record.get(f"direction_correct_{horizon}s") is not None) for horizon in HORIZONS}
    labeled_any = sum(1 for record in records if _primary_correct(record) is not None)
    dates = sorted({str(record["session_date"]) for record in records if record.get("session_date")})
    if not dates:
        dates = sorted({str(meta["session_date"]) for meta in run_meta.values() if meta.get("session_date")})
    tna = sum(1 for record in records if record["symbol"] == "TNA")
    tza = sum(1 for record in records if record["symbol"] == "TZA")
    liveness = summarize_liveness(unique)
    known_fresh = [row.get("freshness_gate_passed") for row in unique if row.get("freshness_gate_passed") is not None]
    legacy_pct = None if not known_fresh else round(sum(1 for flag in known_fresh if flag) / len(known_fresh) * 100.0, 1)
    liveness["legacy_freshness_pass_pct"] = legacy_pct
    return {
        "runs": len(by_run),
        "run_ids": sorted(by_run),
        "session_dates": dates,
        "total_cycles": len(included),
        "total_candidates": len(records),
        "labeled_candidates_30s": labeled[30],
        "labeled_candidates_60s": labeled[60],
        "labeled_candidates_120s": labeled[120],
        "labeled_candidates": labeled_any,
        "tna_candidates": tna,
        "feed_health": liveness,
        "tza_candidates": tza,
        "baseline_accuracy": {f"{horizon}s": _accuracy(record.get(f"direction_correct_{horizon}s") for record in records) for horizon in HORIZONS},
        "accuracy_by": {
            "candidate_score_band": _grouped(records, "score_band", 60),
            "signal_reason": _grouped(records, "decision_reason", 60),
            "symbol": _grouped(records, "symbol", 60),
            "day_part": _grouped(records, "day_part", 60),
            "vix_range": _grouped(records, "vix_band", 60),
            "momentum_phase": _grouped(records, "predictive_momentum_phase", 60),
            "predictive_probability_band": _grouped(records, "probability_band", 60),
            "quality_tier": _grouped(records, "quality_tier", 60),
        },
        "candidate_retention_by_filter": {
            "predictive_continuation": _filter_tally(records, "continuation_analytics_accepted"),
            "quality_ranking": _filter_tally(records, "ranking_analytics_accepted"),
        },
        "minimum_labeled_candidates": MINIMUM_LABELED_CANDIDATES,
        "sample_size_status": _sample_status(max(labeled.values()) if labeled else 0),
        "generalization_status": "insufficient_sessions" if len(dates) < 3 else "awaiting_comparison",
        "excluded_run_ids": excluded_runs,
        "candidates": records,
        "orders_submitted": ORDERS_SUBMITTED,
        "writes_to_database": WRITES_TO_DATABASE,
        "analytics_only": ANALYTICS_ONLY,
        "strategy_impact": "none",
        "semiconductor_context": summarize_semiconductor(records),
        "note": NOTE,
    }


build_validation_report = build_validation_dataset
