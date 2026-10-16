"""SOXL/SOXS context for shadow logs. Analytics only. Never a trading input.

Missing or hard-stale semiconductor quotes become UNAVAILABLE. The TNA/TZA
decision is left unchanged. This module does not import execution code.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from backend.shadow_mode.stream_readiness import HARD_STALE_SECONDS
from backend.signals.models import MarketSnapshot, SymbolQuote

CONTEXT_SYMBOLS = ("SOXL", "SOXS")
DIRECTION_BULLISH = "BULLISH"
DIRECTION_BEARISH = "BEARISH"
DIRECTION_MIXED = "MIXED"
DIRECTION_UNAVAILABLE = "UNAVAILABLE"
AGREE = "AGREE"
DISAGREE = "DISAGREE"
AGREEMENT_VALUES = (AGREE, DISAGREE, DIRECTION_MIXED, DIRECTION_UNAVAILABLE)


def shadow_stream_symbols(symbols: Sequence[str]) -> List[str]:
    """Required signal symbols plus analytics-only semiconductor ETFs."""
    ordered: List[str] = []
    for symbol in list(symbols) + list(CONTEXT_SYMBOLS):
        name = str(symbol).strip().upper()
        if name and name not in ordered:
            ordered.append(name)
    return ordered


def _window_move_pct(quote: Optional[SymbolQuote]) -> Optional[float]:
    """Same observation window the signal engine uses: mid versus first mid."""
    if quote is None or quote.mid is None or quote.first_mid is None or quote.first_mid <= 0:
        return None
    return round((quote.mid - quote.first_mid) / quote.first_mid * 100.0, 4)


def _usable_move(quote: Optional[SymbolQuote]) -> Optional[float]:
    if quote is None or not quote.has_quote:
        return None
    age = quote.quote_age_seconds
    if age is not None and float(age) > HARD_STALE_SECONDS:
        return None
    return _window_move_pct(quote)


def _direction(soxl: Optional[float], soxs: Optional[float]) -> str:
    if soxl is None or soxs is None:
        return DIRECTION_UNAVAILABLE
    if soxl > 0 and soxs < 0:
        return DIRECTION_BULLISH
    if soxl < 0 and soxs > 0:
        return DIRECTION_BEARISH
    return DIRECTION_MIXED


def _agreement(decision: Optional[str], selected_symbol: Optional[str], direction: str) -> Optional[str]:
    if decision not in ("bullish", "bearish") or selected_symbol not in ("TNA", "TZA"):
        return None
    if direction == DIRECTION_UNAVAILABLE:
        return DIRECTION_UNAVAILABLE
    if direction == DIRECTION_MIXED:
        return DIRECTION_MIXED
    if decision == "bullish" and selected_symbol == "TNA":
        return AGREE if direction == DIRECTION_BULLISH else DISAGREE
    if decision == "bearish" and selected_symbol == "TZA":
        return AGREE if direction == DIRECTION_BEARISH else DISAGREE
    return None


def evaluate_semiconductor_context(
    snapshot: Optional[MarketSnapshot],
    *,
    decision: Optional[str] = None,
    selected_symbol: Optional[str] = None,
) -> Dict[str, Any]:
    """Classify the semiconductor pair. Does not read or write a trading decision."""
    soxl_quote = None if snapshot is None else snapshot.get("SOXL")
    soxs_quote = None if snapshot is None else snapshot.get("SOXS")
    qqq_quote = None if snapshot is None else snapshot.get("QQQ")
    soxl = _usable_move(soxl_quote)
    soxs = _usable_move(soxs_quote)
    direction = _direction(soxl, soxs)
    spread = None if soxl is None or soxs is None else round(soxl - soxs, 4)
    return {
        "soxl_window_move_pct": soxl,
        "soxs_window_move_pct": soxs,
        "semiconductor_pair_spread_pct": spread,
        "semiconductor_direction": direction,
        "semiconductor_agreement": _agreement(decision, selected_symbol, direction),
        "qqq_window_move_pct": _window_move_pct(qqq_quote),
        "analytics_only": True,
        "strategy_impact": "none",
        "orders_submitted": 0,
    }


def _loads(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _lookup(row: Any, name: str) -> Any:
    if isinstance(row, Mapping):
        return row.get(name)
    return getattr(row, name, None)


def _eligible(decision: Optional[str], symbol: Optional[str]) -> bool:
    return decision in ("bullish", "bearish") and symbol in ("TNA", "TZA")


def _quote_was_stored(quote: Any) -> bool:
    if not isinstance(quote, dict):
        return False
    if quote.get("quote_updates") or quote.get("quote_age_seconds") is not None:
        return True
    return quote.get("bid") is not None and quote.get("ask") is not None


def row_has_semiconductor_fields(row: Any) -> bool:
    """True only when this row already stored a SOXL or SOXS quote or move."""
    blobs = [
        _lookup(row, "semiconductor_context"),
        _loads(_lookup(row, "raw_score_json")).get("semiconductor_context"),
    ]
    for blob in blobs:
        if not isinstance(blob, dict):
            continue
        if blob.get("soxl_window_move_pct") is not None or blob.get("soxs_window_move_pct") is not None:
            return True
        if blob.get("fields_present") is True:
            return True
        direction = blob.get("semiconductor_direction")
        if direction not in (None, DIRECTION_UNAVAILABLE):
            return True
    quotes = _loads(_lookup(row, "raw_snapshot_json")).get("quotes") or {}
    if not isinstance(quotes, dict):
        return False
    for name in CONTEXT_SYMBOLS:
        if _quote_was_stored(quotes.get(name) or quotes.get(name.lower())):
            return True
    return False


def context_for_candidate(row: Any, *, decision: Optional[str], symbol: Optional[str]) -> Dict[str, Any]:
    """Context for one replayed candidate. Missing history stays UNAVAILABLE.

    Agreement uses the candidate's direction and symbol. A stored skip decision
    cannot leave the agreement null.
    """
    present = row_has_semiconductor_fields(row)
    snapshot = None
    payload = _loads(_lookup(row, "raw_snapshot_json"))
    if payload.get("quotes"):
        try:
            snapshot = MarketSnapshot.from_dict(payload)
        except (KeyError, TypeError, ValueError):
            snapshot = None
    context = evaluate_semiconductor_context(snapshot, decision=decision, selected_symbol=symbol)
    context["fields_present"] = present
    if not present:
        context["soxl_window_move_pct"] = None
        context["soxs_window_move_pct"] = None
        context["semiconductor_pair_spread_pct"] = None
        context["semiconductor_direction"] = DIRECTION_UNAVAILABLE
        if _eligible(decision, symbol):
            context["semiconductor_agreement"] = DIRECTION_UNAVAILABLE
            context["bucket_reason"] = "no stored SOXL/SOXS data"
        else:
            context["semiconductor_agreement"] = None
            context["exclusion_reason"] = "not a bullish TNA or bearish TZA candidate"
            context["bucket_reason"] = context["exclusion_reason"]
        return context
    context["bucket_reason"] = "classified from stored semiconductor quotes"
    if context.get("semiconductor_agreement") not in AGREEMENT_VALUES and _eligible(decision, symbol):
        context["semiconductor_agreement"] = _agreement(
            decision, symbol, str(context.get("semiconductor_direction") or DIRECTION_UNAVAILABLE)
        )
    return context


def context_from_row(row: Any) -> Dict[str, Any]:
    """Read stored context, else the stored snapshot. Old rows stay unavailable.

    Does not invent SOXL/SOXS prices that were never logged.
    """
    direct = _lookup(row, "semiconductor_context")
    if isinstance(direct, dict) and direct.get("semiconductor_direction"):
        return dict(direct)
    stored = _loads(_lookup(row, "raw_score_json")).get("semiconductor_context")
    if isinstance(stored, dict) and stored.get("semiconductor_direction"):
        return dict(stored)
    return context_for_candidate(
        row,
        decision=_lookup(row, "direction") or _lookup(row, "decision"),
        symbol=_lookup(row, "symbol") or _lookup(row, "selected_symbol"),
    )


def _accuracy(correct: int, incorrect: int) -> Optional[float]:
    total = correct + incorrect
    return round(correct / total * 100.0, 1) if total else None


def _sign(value: Optional[float]) -> int:
    if value is None or value == 0:
        return 0
    return 1 if value > 0 else -1


def _candidate_identity(record: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "run_id": record.get("run_id"),
        "session_date": record.get("session_date"),
        "cycle": record.get("cycle", record.get("cycle_number")),
        "symbol": record.get("symbol") or record.get("selected_symbol"),
        "direction": record.get("direction") or record.get("decision"),
    }


def _resolve_context(record: Mapping[str, Any]) -> Dict[str, Any]:
    """One bucket per labeled candidate. Null agreement is not dropped."""
    context = record.get("semiconductor_context")
    if not isinstance(context, dict):
        context = context_from_row(record)
    context = dict(context)
    identity = _candidate_identity(record)
    decision = identity["direction"]
    symbol = identity["symbol"]
    stored_agreement = context.get("semiconductor_agreement")
    present = context.get("fields_present")
    if present is None:
        present = (
            context.get("soxl_window_move_pct") is not None
            or context.get("soxs_window_move_pct") is not None
            or context.get("semiconductor_direction") not in (None, DIRECTION_UNAVAILABLE)
        )
    context["fields_present"] = bool(present)
    if not present:
        context["semiconductor_direction"] = DIRECTION_UNAVAILABLE
        context["soxl_window_move_pct"] = None
        context["soxs_window_move_pct"] = None
        context["semiconductor_pair_spread_pct"] = None
        if _eligible(decision, symbol):
            context["semiconductor_agreement"] = DIRECTION_UNAVAILABLE
            if stored_agreement is None:
                context["bucket_reason"] = (
                    "no stored SOXL/SOXS data; stored row decision/symbol was not a "
                    "bullish TNA or bearish TZA pair, so the previous summary omitted this candidate"
                )
            else:
                context["bucket_reason"] = "no stored SOXL/SOXS data"
        elif stored_agreement in AGREEMENT_VALUES:
            context["semiconductor_agreement"] = stored_agreement
            context["bucket_reason"] = context.get("bucket_reason") or "no stored SOXL/SOXS data"
        else:
            context["semiconductor_agreement"] = None
            context["exclusion_reason"] = "not a bullish TNA or bearish TZA candidate"
            context["bucket_reason"] = context["exclusion_reason"]
        return context
    if context.get("semiconductor_agreement") not in AGREEMENT_VALUES and _eligible(decision, symbol):
        context["semiconductor_agreement"] = _agreement(
            decision, symbol, str(context.get("semiconductor_direction") or DIRECTION_UNAVAILABLE)
        )
        context["bucket_reason"] = "agreement assigned from the replayed candidate"
    elif not context.get("bucket_reason"):
        context["bucket_reason"] = "classified from stored semiconductor quotes"
    return context


def _outcome_status(flag: Optional[bool]) -> str:
    if flag is True:
        return "correct"
    if flag is False:
        return "incorrect"
    return "unlabeled"


def summarize_semiconductor(
    records: Sequence[Mapping[str, Any]],
    *,
    correct_key: str = "direction_correct_60s",
) -> Dict[str, Any]:
    """Counterfactual agreement stats. Nothing here is applied to a decision."""
    buckets = {
        name: {"count": 0, "correct": 0, "incorrect": 0}
        for name in (AGREE, DISAGREE, DIRECTION_MIXED, DIRECTION_UNAVAILABLE)
    }
    winners_agree = losers_agree = winners_disagree = losers_disagree = 0
    soxl_obs = soxl_agree = soxs_obs = soxs_agree = 0
    excluded: List[Dict[str, Any]] = []
    details: List[Dict[str, Any]] = []
    labeled_records = [record for record in records if record.get(correct_key) is not None]
    for record in labeled_records:
        context = _resolve_context(record)
        identity = _candidate_identity(record)
        flag = record.get(correct_key)
        agreement = context.get("semiconductor_agreement")
        detail = {
            **identity,
            "outcome_60s": _outcome_status(flag if isinstance(flag, bool) or flag is None else None),
            "semiconductor_direction": context.get("semiconductor_direction"),
            "semiconductor_agreement": agreement,
            "fields_present": bool(context.get("fields_present")),
            "bucket_reason": context.get("bucket_reason"),
        }
        if agreement not in buckets:
            reason = str(context.get("exclusion_reason") or context.get("bucket_reason") or "semiconductor_agreement missing")
            detail["bucket"] = "excluded"
            detail["bucket_reason"] = reason
            excluded.append(detail)
            details.append(detail)
            continue
        detail["bucket"] = agreement
        details.append(detail)
        buckets[agreement]["count"] += 1
        if flag is True:
            buckets[agreement]["correct"] += 1
        elif flag is False:
            buckets[agreement]["incorrect"] += 1
        if agreement == AGREE and flag is True:
            winners_agree += 1
        elif agreement == AGREE and flag is False:
            losers_agree += 1
        elif agreement == DISAGREE and flag is True:
            winners_disagree += 1
        elif agreement == DISAGREE and flag is False:
            losers_disagree += 1
        qqq = _sign(context.get("qqq_window_move_pct"))
        soxl = _sign(context.get("soxl_window_move_pct"))
        soxs = _sign(context.get("soxs_window_move_pct"))
        if qqq and soxl:
            soxl_obs += 1
            soxl_agree += int(soxl == qqq)
        if qqq and soxs:
            soxs_obs += 1
            soxs_agree += int((-soxs) == qqq)
    labeled_count = len(labeled_records)
    accounted = (
        buckets[AGREE]["count"]
        + buckets[DISAGREE]["count"]
        + buckets[DIRECTION_MIXED]["count"]
        + buckets[DIRECTION_UNAVAILABLE]["count"]
        + len(excluded)
    )
    disagree = buckets[DISAGREE]["count"]
    kept = labeled_count - disagree
    accounting = {
        "labeled_candidates": labeled_count,
        "agree": buckets[AGREE]["count"],
        "disagree": buckets[DISAGREE]["count"],
        "mixed": buckets[DIRECTION_MIXED]["count"],
        "unavailable": buckets[DIRECTION_UNAVAILABLE]["count"],
        "excluded": len(excluded),
        "accounted_total": accounted,
        "accounting_valid": accounted == labeled_count,
    }
    result: Dict[str, Any] = {
        "analytics_only": True,
        "strategy_impact": "none",
        "orders_submitted": 0,
        AGREE: {**buckets[AGREE], "accuracy": _accuracy(buckets[AGREE]["correct"], buckets[AGREE]["incorrect"])},
        DISAGREE: {
            **buckets[DISAGREE],
            "accuracy": _accuracy(buckets[DISAGREE]["correct"], buckets[DISAGREE]["incorrect"]),
        },
        DIRECTION_MIXED: {
            **buckets[DIRECTION_MIXED],
            "accuracy": _accuracy(buckets[DIRECTION_MIXED]["correct"], buckets[DIRECTION_MIXED]["incorrect"]),
        },
        DIRECTION_UNAVAILABLE: {"count": buckets[DIRECTION_UNAVAILABLE]["count"]},
        "winners_with_semiconductor_agreement": winners_agree,
        "losers_with_semiconductor_agreement": losers_agree,
        "winners_with_semiconductor_disagreement": winners_disagree,
        "losers_with_semiconductor_disagreement": losers_disagree,
        "if_semiconductor_disagreement_had_blocked_candidate": {
            "winners_removed": winners_disagree,
            "losers_removed": losers_disagree,
            "candidate_retention_pct": round(kept / labeled_count * 100.0, 1) if labeled_count else None,
        },
        "semiconductor_candidate_accounting": accounting,
        "excluded_candidates": excluded,
        "candidates": details,
        "qqq_overlap": {
            "soxl_vs_qqq_agreement_count": soxl_agree,
            "soxl_vs_qqq_observations": soxl_obs,
            "soxl_vs_qqq_agreement_pct": _accuracy(soxl_agree, soxl_obs - soxl_agree),
            "soxs_inverse_vs_qqq_agreement_count": soxs_agree,
            "soxs_inverse_vs_qqq_observations": soxs_obs,
            "soxs_inverse_vs_qqq_agreement_pct": _accuracy(soxs_agree, soxs_obs - soxs_agree),
        },
    }
    return result


def semiconductor_report_lines(block: Optional[Mapping[str, Any]]) -> Iterable[str]:
    data = block or {}
    lines = [
        "semiconductor_context:",
        "  analytics_only: true",
        "  strategy_impact: none",
    ]
    for name in (AGREE, DISAGREE, DIRECTION_MIXED):
        group = data.get(name) or {}
        accuracy = group.get("accuracy")
        accuracy_text = "n/a" if accuracy is None else f"{accuracy:g}"
        lines.append(
            f"  {name}: count={group.get('count', 0)} correct={group.get('correct', 0)} "
            f"incorrect={group.get('incorrect', 0)} accuracy={accuracy_text}"
        )
    unavailable = data.get(DIRECTION_UNAVAILABLE) or {}
    lines.append(f"  {DIRECTION_UNAVAILABLE}: count={unavailable.get('count', 0)}")
    for name in (
        "winners_with_semiconductor_agreement",
        "losers_with_semiconductor_agreement",
        "winners_with_semiconductor_disagreement",
        "losers_with_semiconductor_disagreement",
    ):
        lines.append(f"  {name}: {data.get(name, 0)}")
    blocked = data.get("if_semiconductor_disagreement_had_blocked_candidate") or {}
    retention = blocked.get("candidate_retention_pct")
    retention_text = "n/a" if retention is None else f"{retention:g}"
    lines.append("  if_semiconductor_disagreement_had_blocked_candidate:")
    lines.append(f"    winners_removed: {blocked.get('winners_removed', 0)}")
    lines.append(f"    losers_removed: {blocked.get('losers_removed', 0)}")
    lines.append(f"    candidate_retention_pct: {retention_text}")
    overlap = data.get("qqq_overlap") or {}
    lines.append("  qqq_overlap:")
    lines.append(
        "    SOXL vs QQQ directional agreement: "
        f"count={overlap.get('soxl_vs_qqq_agreement_count', 0)}/"
        f"{overlap.get('soxl_vs_qqq_observations', 0)} "
        f"pct={_pct_text(overlap.get('soxl_vs_qqq_agreement_pct'))}"
    )
    lines.append(
        "    SOXS inverse vs QQQ directional agreement: "
        f"count={overlap.get('soxs_inverse_vs_qqq_agreement_count', 0)}/"
        f"{overlap.get('soxs_inverse_vs_qqq_observations', 0)} "
        f"pct={_pct_text(overlap.get('soxs_inverse_vs_qqq_agreement_pct'))}"
    )
    accounting = data.get("semiconductor_candidate_accounting") or {}
    lines.append("semiconductor_candidate_accounting:")
    lines.append(f"  labeled_candidates: {accounting.get('labeled_candidates', 0)}")
    lines.append(f"  agree: {accounting.get('agree', 0)}")
    lines.append(f"  disagree: {accounting.get('disagree', 0)}")
    lines.append(f"  mixed: {accounting.get('mixed', 0)}")
    lines.append(f"  unavailable: {accounting.get('unavailable', 0)}")
    lines.append(f"  excluded: {accounting.get('excluded', 0)}")
    lines.append(f"  accounted_total: {accounting.get('accounted_total', 0)}")
    lines.append(f"  accounting_valid: {str(bool(accounting.get('accounting_valid'))).lower()}")
    lines.append("semiconductor_candidates:")
    candidates = data.get("candidates") or []
    if not candidates:
        lines.append("  none")
    for item in candidates:
        lines.append(
            "  "
            f"run_id={item.get('run_id')} session_date={item.get('session_date') or 'n/a'} "
            f"cycle={item.get('cycle')} symbol={item.get('symbol')} direction={item.get('direction')} "
            f"outcome_60s={item.get('outcome_60s')} "
            f"semiconductor_direction={item.get('semiconductor_direction')} "
            f"semiconductor_agreement={item.get('semiconductor_agreement')} "
            f"fields_present={str(bool(item.get('fields_present'))).lower()} "
            f"bucket={item.get('bucket')} reason={item.get('bucket_reason')}"
        )
    return lines


def _pct_text(value: Any) -> str:
    return "n/a" if value is None else f"{float(value):g}"
