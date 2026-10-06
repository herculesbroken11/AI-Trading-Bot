"""
Replay-only diagnostic strategy variants (Checkpoint 2.16) — DIAGNOSTIC ONLY.

Each stored snapshot is replayed through the current (v2 quality-gated) Signal
Engine, then a variant may *hypothetically* turn some of its SKIPs into TNA/TZA
to measure whether a rule (mainly broad_market_disagreement) is over-blocking
small-cap divergence. Variants never change live decisions: the live engine
does not import this module, nothing is written back, thresholds are unchanged
and there is no broker / order / execution access.

Variants:
  current_v2
      The live engine's decision, replayed.
  allow_smallcap_divergence_strict
      May replace a broad/strength/clarity/choppy SKIP with a trade only if:
      IWM window move >= 2x momentum threshold, IWM above (below) both day
      open and previous close, selected ETF up >= 3x and opposite ETF down
      >= 3x in-window, neither SPY nor QQQ moving against IWM in-window,
      freshness passed, VIX known and safe, and the v2 quality gates pass.
  allow_smallcap_divergence_moderate
      Same idea with weaker broad-market agreement: IWM window move >= 1x,
      IWM level not against, strong TNA/TZA pair (>= 3x each), at most one of
      SPY/QQQ moving against in-window, VIX not elevated (missing allowed);
      weak_continuation / choppy_confirmation are not applied (they encode
      the broad-market requirement), pullback / overextension gates still are.
  quality_gates_without_broad_block
      Only for broad_market_disagreement SKIPs: re-applies the normal
      thresholds (entry 70 / opposing 40 / gap 20) and v2 quality gates as if
      the broad-market block did not exist.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from backend.shadow_mode.analytics import DEFAULT_MIN_FOLLOWUP_MOVE_PCT, followup_move, score_hypothetical
from backend.shadow_mode.engine_replay import engine_config_for_row
from backend.shadow_mode.models import pct_move
from backend.shadow_mode.skip_opportunity_analysis import DIAGNOSTIC_NOTE, virtual_candidate_labels
from backend.signals.models import VOLATILITY_SYMBOL, MarketSnapshot, SignalDecision, SignalDirection, SignalReason
from backend.signals.quality_gates import PAIR_STRONG_X, STRONG_MOMENTUM_X, assess_quality, gate_failure
from backend.signals.tna_tza_signal_engine import SignalEngineConfig, TnaTzaSignalEngine, trend_of

CURRENT_V2 = "current_v2"
STRICT = "allow_smallcap_divergence_strict"
MODERATE = "allow_smallcap_divergence_moderate"
NO_BROAD_BLOCK = "quality_gates_without_broad_block"
VARIANTS = (CURRENT_V2, STRICT, MODERATE, NO_BROAD_BLOCK)

BULLISH, BEARISH, SKIP = SignalDirection.BULLISH.value, SignalDirection.BEARISH.value, SignalDirection.SKIP.value
TRADES = (BULLISH, BEARISH)
RESCUABLE_SKIPS = frozenset(
    {
        SignalReason.BROAD_MARKET_DISAGREEMENT.value,
        SignalReason.INSUFFICIENT_SIGNAL_STRENGTH.value,
        SignalReason.UNCLEAR_MARKET_DIRECTION.value,
        SignalReason.CHOPPY_CONFIRMATION.value,
    }
)
CONFIRM_AGAINST_X = 0.5  # SPY/QQQ in-window move <= -0.5x momentum threshold counts as moving against IWM
MAX_EXAMPLES = 5


class VariantConfigError(ValueError):
    """Unknown variant name."""


def parse_variants(raw: Optional[str]) -> List[str]:
    if not raw:
        return []
    names = [p.strip() for p in raw.split(",") if p.strip()]
    if names == ["all"]:
        return list(VARIANTS)
    unknown = [n for n in names if n not in VARIANTS]
    if unknown:
        raise VariantConfigError(f"unknown variant(s): {', '.join(unknown)}; choose from {', '.join(VARIANTS)} or all")
    ordered = list(dict.fromkeys(names))
    return ordered if CURRENT_V2 in ordered else [CURRENT_V2, *ordered]


@dataclass
class ReplayContext:
    snapshot: MarketSnapshot
    config: SignalEngineConfig
    decision: SignalDecision
    quotes: Dict[str, Any]
    trends: Dict[str, int]
    moves: Dict[str, Optional[float]]
    vix_level: Optional[float]
    vix_change_pct: Optional[float]


def build_context(row: Mapping[str, Any]) -> Optional[ReplayContext]:
    raw = row.get("raw_snapshot_json")
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(data, dict) or not data.get("quotes"):
            return None
        snapshot = MarketSnapshot.from_dict(data)
        config = engine_config_for_row(row)
        decision = TnaTzaSignalEngine(config, wall_clock=lambda: snapshot.created_at).decide(snapshot)
    except (KeyError, TypeError, ValueError):
        return None
    quotes = {s: snapshot.get(s) for s in config.required_symbols}
    trends = {s: trend_of(quotes.get(s), config.level_tolerance_pct) for s in ("IWM", "SPY", "QQQ", "TNA", "TZA")}
    moves = {s: pct_move(q.first_mid, q.mid) if q else None for s, q in quotes.items()}
    vix = snapshot.get(VOLATILITY_SYMBOL)
    vix_level = vix.price if vix else None
    vix_change = pct_move(vix.prev_close, vix_level) if vix else None
    return ReplayContext(snapshot, config, decision, quotes, trends, moves, vix_level, vix_change)


def _result(decision: str, reason: Optional[str], rule: str) -> Dict[str, Any]:
    return {"decision": decision, "reason": reason, "rule": rule}


def _current(ctx: ReplayContext) -> Dict[str, Any]:
    d = ctx.decision
    return _result(d.decision.value, d.skip_reason.value if d.skip_reason else None, "current_v2")


def _divergence(ctx: ReplayContext, *, strict: bool) -> Dict[str, Any]:
    base = _current(ctx)
    if base["decision"] != SKIP or base["reason"] not in RESCUABLE_SKIPS:
        return base
    cfg = ctx.config
    thr = cfg.momentum_threshold_pct
    prefix = "strict" if strict else "moderate"

    iwm = ctx.moves.get("IWM")
    if iwm is None or abs(iwm) < thr:
        return _result(SKIP, base["reason"], f"{prefix}: no IWM window direction")
    s = 1 if iwm > 0 else -1
    iwm_s = iwm * s
    favoured, inverse = (ctx.moves.get("TNA"), ctx.moves.get("TZA")) if s == 1 else (ctx.moves.get("TZA"), ctx.moves.get("TNA"))
    pair_ok = favoured is not None and inverse is not None and favoured >= PAIR_STRONG_X * thr and inverse <= -PAIR_STRONG_X * thr
    against = sum(
        1 for sym in ("SPY", "QQQ") if ctx.moves.get(sym) is not None and ctx.moves[sym] * s <= -CONFIRM_AGAINST_X * thr
    )
    iwm_trend = ctx.trends["IWM"]
    vix_elevated = ctx.vix_level is not None and (
        ctx.vix_level >= cfg.vix_high_level or (ctx.vix_change_pct is not None and ctx.vix_change_pct >= cfg.vix_rising_pct)
    )

    if strict:
        checks = (
            (iwm_s >= STRONG_MOMENTUM_X * thr, "IWM window move not strong"),
            (iwm_trend == s, "IWM level not on the same side of open and previous close"),
            (pair_ok, "TNA/TZA not confirming inverse movement"),
            (against == 0, "SPY/QQQ moving against IWM in-window"),
            (ctx.vix_level is not None and not vix_elevated, "VIX unknown or not safe"),
        )
    else:
        checks = (
            (iwm_s >= thr, "IWM window move below threshold"),
            (iwm_trend != -s, "IWM level against the window move"),
            (pair_ok, "TNA/TZA not strongly aligned"),
            (against <= 1, "SPY and QQQ both moving against IWM in-window"),
            (not vix_elevated, "VIX elevated"),
        )
    for ok, why in checks:
        if not ok:
            return _result(SKIP, base["reason"], f"{prefix}: {why}")

    assessment = assess_quality(s, ctx.quotes, ctx.trends, thr)
    failure = gate_failure(assessment, momentum_threshold_pct=thr, winning_score=100.0, entry_threshold=cfg.entry_score_threshold)
    ignored = set() if strict else {SignalReason.WEAK_CONTINUATION, SignalReason.CHOPPY_CONFIRMATION}
    if failure is not None and failure[0] not in ignored:
        return _result(SKIP, failure[0].value, f"{prefix}: quality gate {failure[0].value}")
    return _result(BULLISH if s == 1 else BEARISH, None, f"{prefix}: small-cap divergence allowed (was {base['reason']})")


def _no_broad_block(ctx: ReplayContext) -> Dict[str, Any]:
    base = _current(ctx)
    if base["reason"] != SignalReason.BROAD_MARKET_DISAGREEMENT.value:
        return base
    cfg = ctx.config
    bull, bear = ctx.decision.bullish_score, ctx.decision.bearish_score
    if bull >= cfg.entry_score_threshold and bear <= cfg.opposing_score_max:
        s = 1
    elif bear >= cfg.entry_score_threshold and bull <= cfg.opposing_score_max:
        s = -1
    elif abs(bull - bear) < cfg.min_score_gap:
        return _result(SKIP, SignalReason.UNCLEAR_MARKET_DIRECTION.value, "no_broad_block: scores too close")
    else:
        return _result(SKIP, SignalReason.INSUFFICIENT_SIGNAL_STRENGTH.value, "no_broad_block: below entry threshold")
    winning = bull if s == 1 else bear
    assessment = assess_quality(s, ctx.quotes, ctx.trends, cfg.momentum_threshold_pct)
    failure = gate_failure(
        assessment,
        momentum_threshold_pct=cfg.momentum_threshold_pct,
        winning_score=winning,
        entry_threshold=cfg.entry_score_threshold,
    )
    if failure is not None:
        return _result(SKIP, failure[0].value, f"no_broad_block: quality gate {failure[0].value}")
    return _result(BULLISH if s == 1 else BEARISH, None, "no_broad_block: thresholds + quality gates passed")


_VARIANT_FUNCS: Dict[str, Callable[[ReplayContext], Dict[str, Any]]] = {
    CURRENT_V2: _current,
    STRICT: lambda ctx: _divergence(ctx, strict=True),
    MODERATE: lambda ctx: _divergence(ctx, strict=False),
    NO_BROAD_BLOCK: _no_broad_block,
}


def evaluate_variants(row: Mapping[str, Any], variants: Sequence[str] = VARIANTS) -> Dict[str, Dict[str, Any]]:
    """Hypothetical decision per variant for one stored row (rows that cannot be replayed keep the stored decision)."""
    ctx = build_context(row)
    out: Dict[str, Dict[str, Any]] = {}
    for name in variants:
        if ctx is None:
            out[name] = _result(row.get("decision") or SKIP, row.get("skip_reason"), "not replayed (no snapshot)")
        else:
            out[name] = _VARIANT_FUNCS[name](ctx)
        out[name]["replayed"] = ctx is not None
        out[name]["hypothetical"] = True
    return out


def _pct(part: int, whole: int) -> Optional[float]:
    return round(part / whole * 100.0, 1) if whole else None


def compare_variants(
    rows: Sequence[Mapping[str, Any]],
    variants: Sequence[str] = VARIANTS,
    *,
    min_move_pct: float = DEFAULT_MIN_FOLLOWUP_MOVE_PCT,
) -> Dict[str, Any]:
    variants = list(variants) if CURRENT_V2 in variants else [CURRENT_V2, *variants]
    items: List[Dict[str, Any]] = []
    for row in rows:
        old = row.get("decision") or SKIP
        stored = row.get("direction_was_correct")
        old_correct = stored if stored is not None else score_hypothetical(old, row, min_move_pct=min_move_pct)
        results = evaluate_variants(row, variants)
        for r in results.values():
            r["correct"] = score_hypothetical(r["decision"], row, min_move_pct=min_move_pct)
        items.append(
            {
                "row": row,
                "old_decision": old,
                "old_correct": old_correct,
                "variants": results,
                "labels": virtual_candidate_labels(row, min_move_pct=min_move_pct),
            }
        )

    summaries: Dict[str, Dict[str, Any]] = {}
    for name in variants:
        decs = [(i, i["variants"][name]) for i in items]
        trades = [(i, v) for i, v in decs if v["decision"] in TRADES]
        scored = [(i, v) for i, v in trades if v["correct"] is not None]
        correct = sum(1 for _, v in scored if v["correct"])
        skip_reasons: Dict[str, int] = {}
        for _, v in decs:
            if v["decision"] == SKIP:
                key = v["reason"] or "unknown"
                skip_reasons[key] = skip_reasons.get(key, 0) + 1
        old_trades = [(i, v) for i, v in decs if i["old_decision"] in TRADES]
        new_vs_old = [(i, v) for i, v in trades if i["old_decision"] == SKIP]
        added_vs_current = [(i, v) for i, v in trades if i["variants"][CURRENT_V2]["decision"] == SKIP]
        preferred = {BULLISH: "would_have_preferred_tna", BEARISH: "would_have_preferred_tza"}
        examples = sorted(trades, key=lambda iv: iv[0]["variants"][CURRENT_V2]["decision"] in TRADES)[:MAX_EXAMPLES]
        summaries[name] = {
            "bullish_count": sum(1 for _, v in decs if v["decision"] == BULLISH),
            "bearish_count": sum(1 for _, v in decs if v["decision"] == BEARISH),
            "skip_count": sum(1 for _, v in decs if v["decision"] == SKIP),
            "scored_count": len(scored),
            "correct_count": correct,
            "incorrect_count": len(scored) - correct,
            "correct_pct": _pct(correct, len(scored)),
            "false_signals_filtered": sum(1 for i, v in old_trades if i["old_correct"] is False and v["decision"] == SKIP),
            "good_signals_preserved": sum(
                1 for i, v in old_trades if i["old_correct"] is True and v["decision"] == i["old_decision"]
            ),
            "missed_winners": sum(1 for i, v in old_trades if i["old_correct"] is True and v["decision"] == SKIP),
            "new_trades_vs_old": len(new_vs_old),
            "new_trades_vs_old_correct": sum(1 for _, v in new_vs_old if v["correct"] is True),
            "new_trades_vs_old_incorrect": sum(1 for _, v in new_vs_old if v["correct"] is False),
            "trades_added_vs_current_v2": len(added_vs_current),
            "trades_matching_hindsight": sum(1 for i, v in trades if i["labels"][preferred[v["decision"]]]),
            "skip_reasons": dict(sorted(skip_reasons.items(), key=lambda kv: (-kv[1], kv[0]))),
            "trade_examples": [
                {
                    "run_id": i["row"].get("run_id"),
                    "cycle_number": i["row"].get("cycle_number"),
                    "created_at": i["row"].get("created_at"),
                    "old_decision": i["old_decision"],
                    "old_skip_reason": i["row"].get("skip_reason"),
                    "current_v2_decision": i["variants"][CURRENT_V2]["decision"],
                    "variant_decision": v["decision"],
                    "rule": v["rule"],
                    "hypothetical_correct": v["correct"],
                    "iwm_followup_move_pct": followup_move(i["row"], "IWM"),
                    "selected_followup_move_pct": followup_move(i["row"], "TNA" if v["decision"] == BULLISH else "TZA"),
                    "followup_class": i["labels"]["followup_class"],
                }
                for i, v in examples
            ],
        }

    return {
        "variants": variants,
        "rows_evaluated": len(items),
        "rows_replayed": sum(1 for i in items if i["variants"][CURRENT_V2]["replayed"]),
        "min_followup_move_pct": min_move_pct,
        "old_stored": {
            "bullish_count": sum(1 for i in items if i["old_decision"] == BULLISH),
            "bearish_count": sum(1 for i in items if i["old_decision"] == BEARISH),
            "skip_count": sum(1 for i in items if i["old_decision"] == SKIP),
        },
        "virtual_candidates": {
            "would_have_preferred_tna": sum(1 for i in items if i["labels"]["would_have_preferred_tna"]),
            "would_have_preferred_tza": sum(1 for i in items if i["labels"]["would_have_preferred_tza"]),
            "would_have_skipped": sum(1 for i in items if i["labels"]["would_have_skipped"]),
            "applied_to_decisions": False,
        },
        "results": summaries,
        "row_labels": [
            {
                "run_id": i["row"].get("run_id"),
                "cycle_number": i["row"].get("cycle_number"),
                "old_decision": i["old_decision"],
                **{f"{name}_decision": i["variants"][name]["decision"] for name in variants},
                **{k: i["labels"][k] for k in ("would_have_preferred_tna", "would_have_preferred_tza", "would_have_skipped")},
            }
            for i in items
        ],
        "diagnostic_only": True,
        "applied_to_live": False,
        "writes_to_database": False,
        "orders_submitted": 0,
        "note": DIAGNOSTIC_NOTE,
    }
