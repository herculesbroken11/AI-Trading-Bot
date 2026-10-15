"""
Candidate quality ranker (Checkpoint 2.21). Shadow analytics only.

Ranks an existing TNA/TZA candidate from independent evidence. It does not
create a candidate, does not change conservative_v2 or balanced_v3_shadow,
and does not read follow-up prices.

orders_submitted: 0
writes_to_database: false
analytics_only: true
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Sequence, Tuple

from backend.signals.models import MarketSnapshot, SymbolQuote
from backend.signals.predictive_continuation import (
    PHASE_DEVELOPING,
    PHASE_EARLY,
    PHASE_REVERSAL_RISK,
    evaluate_predictive_continuation,
)

ORDERS_SUBMITTED = 0
WRITES_TO_DATABASE = False
ANALYTICS_ONLY = True

_Point = Tuple[float, float]
_SUPPORTIVE_PHASES = frozenset({PHASE_EARLY, PHASE_DEVELOPING})


class CandidateQualityTier(str, Enum):
    A = "A"
    B = "B"
    C = "C"
    REJECT = "REJECT"


class RankedCandidateAction(str, Enum):
    ACCEPT_A = "ACCEPT_A"
    ACCEPT_B = "ACCEPT_B"
    OBSERVE_C = "OBSERVE_C"
    REJECT = "REJECT"


_ACTION_FOR_TIER = {
    CandidateQualityTier.A: RankedCandidateAction.ACCEPT_A,
    CandidateQualityTier.B: RankedCandidateAction.ACCEPT_B,
    CandidateQualityTier.C: RankedCandidateAction.OBSERVE_C,
    CandidateQualityTier.REJECT: RankedCandidateAction.REJECT,
}


@dataclass(frozen=True)
class CandidateQualityConfig:
    """General ranking limits. Not fitted to a single shadow session."""

    tier_a_min_score: float = 72.0
    tier_a_min_evidence: int = 4
    tier_b_min_score: float = 58.0
    tier_b_min_evidence: int = 3
    support_min: float = 55.0
    severe_exhaustion: float = 70.0
    strong_reversal: float = 70.0
    poor_timing: float = 25.0
    contradictory_pair_max: float = 30.0
    soft_move_pct: float = 0.01
    extension_pct: float = 0.18
    extreme_disagreement_pct: float = 0.12
    disagreement_soft_pct: float = 0.02
    one_tick_cap: float = 40.0
    spike_share: float = 0.70
    min_span_seconds: float = 8.0
    flat_step_pct: float = 0.002
    early_total_pct: float = 0.08
    correlation_unit_penalty: float = 25.0
    context_weight: float = 0.04
    momentum_weight: float = 0.22
    pair_weight: float = 0.22
    market_weight: float = 0.14
    timing_weight: float = 0.16
    path_weight: float = 0.10
    continuation_weight: float = 0.12
    extension_penalty_weight: float = 0.15
    disagreement_penalty_weight: float = 0.12
    correlation_penalty_weight: float = 0.08
    tier_a_requires_pair: bool = True
    accept_tiers: Tuple[str, ...] = ("A", "B")

    def validate(self) -> "CandidateQualityConfig":
        problems: List[str] = []
        if not 0.0 < self.tier_b_min_score <= self.tier_a_min_score <= 100.0:
            problems.append("tier score limits must satisfy 0 < B <= A <= 100")
        if self.tier_a_min_evidence < 1 or self.tier_b_min_evidence < 1:
            problems.append("evidence minimums must be >= 1")
        if self.tier_a_min_evidence < self.tier_b_min_evidence:
            problems.append("tier A must require at least as much evidence as tier B")
        bounds = (
            self.support_min,
            self.severe_exhaustion,
            self.strong_reversal,
            self.poor_timing,
            self.contradictory_pair_max,
            self.one_tick_cap,
        )
        if any(value < 0.0 or value > 100.0 for value in bounds):
            problems.append("score limits must be in [0, 100]")
        positive = (
            self.soft_move_pct,
            self.extension_pct,
            self.extreme_disagreement_pct,
            self.disagreement_soft_pct,
            self.min_span_seconds,
            self.early_total_pct,
        )
        if any(value <= 0.0 for value in positive):
            problems.append("move thresholds must be > 0")
        if not 0.0 < self.spike_share <= 1.0:
            problems.append("spike_share must be in (0, 1]")
        if self.flat_step_pct < 0.0:
            problems.append("flat_step_pct must be >= 0")
        weights = (
            self.context_weight,
            self.momentum_weight,
            self.pair_weight,
            self.market_weight,
            self.timing_weight,
            self.path_weight,
            self.continuation_weight,
            self.extension_penalty_weight,
            self.disagreement_penalty_weight,
            self.correlation_penalty_weight,
        )
        if any(weight < 0.0 or weight > 1.0 for weight in weights):
            problems.append("weights must be in [0, 1]")
        allowed = {tier.value for tier in CandidateQualityTier if tier is not CandidateQualityTier.REJECT}
        if any(tier not in allowed for tier in self.accept_tiers):
            problems.append("accept_tiers may contain only A, B, C")
        if problems:
            raise ValueError("Invalid candidate quality config: " + "; ".join(problems))
        return self


@dataclass(frozen=True)
class CandidateQualityResult:
    quality_score: float
    quality_tier: CandidateQualityTier
    evidence_count: int
    independent_confirmation_count: int
    momentum_quality_score: float
    pair_structure_score: float
    market_alignment_score: float
    timing_quality_score: float
    path_consistency_score: float
    continuation_quality_score: float
    extension_penalty: float
    disagreement_penalty: float
    correlated_evidence_penalty: float
    effective_evidence_count: int
    rank_passed: bool
    rank_reason: str
    warnings: List[str] = field(default_factory=list)
    ranked_candidate_action: str = RankedCandidateAction.OBSERVE_C.value
    continuation_probability: float = 0.0
    momentum_phase: str = PHASE_EARLY
    timing_phase: str = "EARLY"

    def to_dict(self) -> dict:
        return {
            "quality_score": self.quality_score,
            "quality_tier": self.quality_tier.value,
            "evidence_count": self.evidence_count,
            "independent_confirmation_count": self.independent_confirmation_count,
            "effective_evidence_count": self.effective_evidence_count,
            "momentum_quality_score": self.momentum_quality_score,
            "pair_structure_score": self.pair_structure_score,
            "market_alignment_score": self.market_alignment_score,
            "timing_quality_score": self.timing_quality_score,
            "path_consistency_score": self.path_consistency_score,
            "continuation_quality_score": self.continuation_quality_score,
            "extension_penalty": self.extension_penalty,
            "disagreement_penalty": self.disagreement_penalty,
            "correlated_evidence_penalty": self.correlated_evidence_penalty,
            "rank_passed": self.rank_passed,
            "rank_reason": self.rank_reason,
            "warnings": list(self.warnings),
            "ranked_candidate_action": self.ranked_candidate_action,
            "continuation_probability": self.continuation_probability,
            "momentum_phase": self.momentum_phase,
            "timing_phase": self.timing_phase,
            "orders_submitted": ORDERS_SUBMITTED,
            "writes_to_database": WRITES_TO_DATABASE,
            "analytics_only": ANALYTICS_ONLY,
        }


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _round1(value: float) -> float:
    return round(_clamp(value), 1)


def _signed_pct(later: Optional[float], earlier: Optional[float], sign: int) -> Optional[float]:
    if later is None or earlier is None or earlier <= 0:
        return None
    return (later - earlier) / earlier * 100.0 * sign


def _samples(quote: Optional[SymbolQuote]) -> List[_Point]:
    if quote is None:
        return []
    cleaned: List[_Point] = []
    raw = sorted((float(t), float(mid)) for t, mid in (quote.mid_path or ()) if mid is not None and mid > 0)
    for t, mid in raw:
        if cleaned and abs(cleaned[-1][0] - t) <= 1e-6:
            cleaned[-1] = (t, mid)
        else:
            cleaned.append((t, mid))
    if quote.first_mid is not None and quote.first_mid > 0 and (not cleaned or cleaned[0][0] > 0.25):
        cleaned.insert(0, (0.0, float(quote.first_mid)))
    if quote.mid is not None and quote.mid > 0:
        if not cleaned:
            cleaned.append((0.0, float(quote.mid)))
        elif abs(cleaned[-1][1] - float(quote.mid)) / float(quote.mid) > 1e-5:
            cleaned.append((cleaned[-1][0] + 0.001, float(quote.mid)))
    return cleaned


def _price_at(samples: Sequence[_Point], target: float) -> float:
    if target <= samples[0][0]:
        return samples[0][1]
    if target >= samples[-1][0]:
        return samples[-1][1]
    for (t0, p0), (t1, p1) in zip(samples, samples[1:]):
        if t0 <= target <= t1:
            if t1 == t0:
                return p1
            weight = (target - t0) / (t1 - t0)
            return p0 + (p1 - p0) * weight
    return samples[-1][1]


def _span_ok(samples: Sequence[_Point], config: CandidateQualityConfig) -> bool:
    return len(samples) >= 2 and (samples[-1][0] - samples[0][0]) >= config.min_span_seconds


def _steps(samples: Sequence[_Point], sign: int, config: CandidateQualityConfig) -> List[float]:
    moves: List[float] = []
    for (_, earlier), (_, later) in zip(samples, samples[1:]):
        move = _signed_pct(later, earlier, sign)
        if move is None or abs(move) < config.flat_step_pct:
            continue
        moves.append(move)
    return moves


def _slice(samples: Sequence[_Point], sign: int) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
    if len(samples) < 2:
        return None, None, None, None
    t0, p0 = samples[0]
    t1, p1 = samples[-1]
    span = t1 - t0
    if span <= 0 or p0 <= 0:
        return None, None, None, None
    mid = _price_at(samples, t0 + span / 2.0)
    third = _price_at(samples, t0 + span * (2.0 / 3.0))
    return (
        _signed_pct(mid, p0, sign),
        _signed_pct(p1, mid, sign),
        _signed_pct(p1, third, sign),
        _signed_pct(p1, p0, sign),
    )


def _flip_count(moves: Sequence[float]) -> int:
    flips = 0
    previous = 0
    for move in moves:
        sign = 1 if move > 0 else -1
        if previous and sign != previous:
            flips += 1
        previous = sign
    return flips


def _momentum(samples: Sequence[_Point], config: CandidateQualityConfig) -> float:
    if not _span_ok(samples, config):
        return 0.0
    first, second, final, _total = _slice(samples, 1)
    moves = _steps(samples, 1, config)
    if not moves:
        return 0.0
    consistency = sum(1 for move in moves if move > 0) / len(moves) * 100.0
    if second is None:
        shape = 0.0
    elif first is not None and second > 0 and second >= first:
        shape = 100.0
    elif second > 0:
        shape = 55.0
    else:
        shape = 10.0
    confirm = 100.0 if second is not None and second >= config.soft_move_pct else 40.0 if second is not None and second > 0 else 0.0
    score = 0.45 * consistency + 0.30 * shape + 0.25 * confirm
    if final is not None and final > 0:
        score += 6.0
    elif final is not None and final < -config.soft_move_pct:
        score -= 20.0
    positive = [move for move in moves if move > 0]
    if positive and max(positive) >= config.spike_share * sum(positive) and len(positive) >= 1 and sum(positive) > 0:
        if len(moves) <= 2 and max(positive) >= config.spike_share * sum(positive) and (first or 0) > (second or 0) * 3:
            score -= 35.0
        elif len(positive) == 1 and len(moves) >= 3:
            score -= 35.0
    if first is not None and first >= 0.08 and second is not None and second < first * 0.35:
        score -= 30.0
    score -= min(40.0, _flip_count(moves) * 15.0)
    return _clamp(score)


def _pair(
    selected: Sequence[_Point],
    inverse: Sequence[_Point],
    config: CandidateQualityConfig,
) -> Tuple[float, int, bool]:
    selected_moves = _steps(selected, 1, config) if _span_ok(selected, config) else []
    inverse_moves = _steps(inverse, -1, config) if _span_ok(inverse, config) else []
    selected_total = _slice(selected, 1)[3] if _span_ok(selected, config) else None
    inverse_total = _slice(inverse, -1)[3] if _span_ok(inverse, config) else None
    confirming = sum(1 for move in selected_moves if move > 0) + sum(1 for move in inverse_moves if move > 0)
    samples = max(len(selected_moves), len(inverse_moves), 1)
    persistence = confirming / samples
    both = (
        selected_total is not None
        and inverse_total is not None
        and selected_total >= config.soft_move_pct
        and inverse_total >= config.soft_move_pct
    )
    one_side = (
        (selected_total is not None and selected_total >= config.soft_move_pct)
        or (inverse_total is not None and inverse_total >= config.soft_move_pct)
    )
    if both:
        score = 70.0 + _clamp(persistence, 0.0, 1.0) * 30.0
    elif one_side:
        score = 35.0 + _clamp(persistence, 0.0, 1.0) * 20.0
    else:
        score = 10.0 + _clamp(persistence, 0.0, 1.0) * 15.0
    one_tick = confirming <= 1 and (len(selected_moves) + len(inverse_moves)) >= 2
    if one_tick:
        score = min(score, config.one_tick_cap)
    contradictory = False
    if selected_total is not None and selected_total <= -config.disagreement_soft_pct:
        contradictory = True
    if inverse_total is not None and inverse_total <= -config.disagreement_soft_pct and not both:
        contradictory = True
    if contradictory:
        score = min(score, config.contradictory_pair_max)
    return _clamp(score), confirming, contradictory


def _agree_points(move: Optional[float], config: CandidateQualityConfig) -> Optional[float]:
    if move is None:
        return None
    if move >= 0.03:
        return 100.0
    if move >= config.soft_move_pct:
        return 70.0
    if move > 0:
        return 45.0
    if move > -config.soft_move_pct:
        return 30.0
    if move > -0.05:
        return 15.0
    return 0.0


def _market(
    iwm: Optional[float],
    spy: Optional[float],
    qqq: Optional[float],
    config: CandidateQualityConfig,
) -> Tuple[float, float, bool]:
    parts = [(0.50, _agree_points(iwm, config)), (0.25, _agree_points(spy, config)), (0.25, _agree_points(qqq, config))]
    known = [(weight, score) for weight, score in parts if score is not None]
    if not known:
        return 0.0, 0.0, False
    weight_sum = sum(weight for weight, _score in known)
    displayed = sum(weight * score for weight, score in known) / weight_sum
    broad = [score for score in (_agree_points(spy, config), _agree_points(qqq, config)) if score is not None]
    broad_only = sum(broad) / len(broad) if broad else 0.0
    against = 0
    severity = 0.0
    for move in (iwm, spy, qqq):
        if move is not None and move <= -config.disagreement_soft_pct:
            against += 1
            severity += min(40.0, abs(move) / config.disagreement_soft_pct * 10.0)
    penalty = _clamp(against * 25.0 + severity)
    spy_against = spy is not None and spy <= -config.extreme_disagreement_pct
    qqq_against = qqq is not None and qqq <= -config.extreme_disagreement_pct
    if spy_against and qqq_against:
        penalty = max(penalty, 80.0)
    return _clamp(displayed), penalty, spy_against and qqq_against


def _retracement(samples: Sequence[_Point], sign: int) -> float:
    if len(samples) < 2 or samples[0][1] <= 0:
        return 0.0
    start = samples[0][1]
    peak = 0.0
    worst_giveback = 0.0
    for _t, price in samples:
        signed = (price - start) / start * 100.0 * sign
        peak = max(peak, signed)
        if peak > 0:
            worst_giveback = max(worst_giveback, (peak - signed) / peak)
    return _clamp(worst_giveback, 0.0, 1.0)


def _timing(
    samples: Sequence[_Point],
    config: CandidateQualityConfig,
) -> Tuple[float, str, float]:
    if not _span_ok(samples, config):
        return 0.0, "EXHAUSTED", 0.0
    first, second, final, total = _slice(samples, 1)
    extension = 0.0
    if total is not None and total >= config.extension_pct:
        extension = 55.0
        if first is not None and second is not None and first > 0 and second < first * 0.5:
            extension = 80.0
        if final is not None and final <= 0:
            extension = 90.0
    elif first is not None and second is not None and first >= 0.10 and second < max(0.01, first * 0.35):
        extension = 70.0
    accelerating = second is not None and second > 0 and (first is None or second >= first)
    if total is not None and total < config.early_total_pct and accelerating and (final is None or final >= 0):
        phase, base = "EARLY", 88.0
    elif second is not None and second > 0 and (final is None or final >= 0) and extension < 60.0:
        phase, base = "DEVELOPING", 76.0
    elif extension >= 60.0 or (first is not None and first >= 0.10 and (second is None or second <= 0)):
        phase, base = "EXHAUSTED", 18.0
    else:
        phase, base = "LATE", 38.0
    moves = _steps(samples, 1, config)
    score = base - min(50.0, _flip_count(moves) * 12.0) - _retracement(samples, 1) * 30.0
    if final is not None and final < -config.soft_move_pct:
        score -= 15.0
    return _clamp(score), phase, extension


def _path(samples: Sequence[_Point], config: CandidateQualityConfig) -> float:
    if not _span_ok(samples, config):
        return 0.0
    moves = _steps(samples, 1, config)
    if not moves:
        return 20.0
    ratio = sum(1 for move in moves if move > 0) / len(moves)
    _first, _second, final, _total = _slice(samples, 1)
    final_ok = 100.0 if final is not None and final > 0 else 25.0 if final is not None and final >= -config.flat_step_pct else 0.0
    smooth = 100.0 - min(100.0, _flip_count(moves) * 25.0)
    score = 0.40 * ratio * 100.0 + 0.25 * final_ok + 0.20 * (1.0 - _retracement(samples, 1)) * 100.0 + 0.15 * smooth
    return _clamp(score)


def rank_candidate(
    snapshot: MarketSnapshot,
    candidate_symbol: Optional[str],
    *,
    candidate_score: Optional[float] = None,
    config: Optional[CandidateQualityConfig] = None,
) -> Optional[CandidateQualityResult]:
    """Rank one existing TNA/TZA candidate. None when there is no candidate.

    Follow-up prices are not an input. candidate_score is a small context
    term only and is not the ranking value.
    """
    if candidate_symbol is None:
        return None
    symbol = str(candidate_symbol).upper()
    if symbol not in {"TNA", "TZA"}:
        return None
    cfg = (config or CandidateQualityConfig()).validate()
    sign = 1 if symbol == "TNA" else -1
    inverse_name = "TZA" if symbol == "TNA" else "TNA"
    etf_samples = _samples(snapshot.get(symbol))
    inverse_samples = _samples(snapshot.get(inverse_name))
    iwm_samples = _samples(snapshot.get("IWM"))
    etf_ready = _span_ok(etf_samples, cfg)
    iwm_ready = _span_ok(iwm_samples, cfg)

    momentum = _momentum_signed(iwm_samples, sign, cfg)
    pair_score, confirming_steps, contradictory = _pair(etf_samples, inverse_samples, cfg)
    iwm_move = _slice(iwm_samples, sign)[3] if iwm_ready else None
    spy_move = _slice(_samples(snapshot.get("SPY")), sign)[3] if _span_ok(_samples(snapshot.get("SPY")), cfg) else None
    qqq_move = _slice(_samples(snapshot.get("QQQ")), sign)[3] if _span_ok(_samples(snapshot.get("QQQ")), cfg) else None
    market_score, disagreement, extreme_disagreement = _market(iwm_move, spy_move, qqq_move, cfg)
    timing_samples = etf_samples if etf_ready else iwm_samples
    timing_score, timing_phase, extension = _timing(timing_samples, cfg)
    path_samples = etf_samples if etf_ready else iwm_samples
    path_score = _path(path_samples, cfg)
    continuation = evaluate_predictive_continuation(snapshot, symbol)
    if continuation is None:
        continuation_score = 0.0
        probability = 0.0
        phase = PHASE_EARLY
        exhaustion = 0.0
        reversal = 0.0
        continuation_passed = False
    else:
        continuation_score = continuation.continuation_probability
        probability = continuation.continuation_probability
        phase = continuation.momentum_phase
        exhaustion = continuation.exhaustion_score
        reversal = continuation.reversal_risk_score
        continuation_passed = continuation.prediction_passed

    broad_agrees = (spy_move is not None and spy_move >= cfg.soft_move_pct) or (
        qqq_move is not None and qqq_move >= cfg.soft_move_pct
    )
    pair_support = pair_score >= cfg.support_min and confirming_steps >= 2 and not contradictory
    timing_support = etf_ready and timing_score >= cfg.support_min and timing_phase in {"EARLY", "DEVELOPING"}
    continuation_support = (
        etf_ready and continuation_passed and phase in _SUPPORTIVE_PHASES and not (exhaustion >= cfg.severe_exhaustion)
    )
    momentum_support = iwm_ready and momentum >= cfg.support_min
    market_raw_support = market_score >= cfg.support_min
    market_support = broad_agrees and market_raw_support

    raw_groups = sum(
        1
        for flag in (
            momentum_support,
            pair_support,
            market_raw_support,
            timing_support or (timing_score >= cfg.support_min),
            continuation_score >= cfg.support_min,
        )
        if flag
    )
    independent = sum(1 for flag in (momentum_support, pair_support, market_support, timing_support, continuation_support) if flag)
    collapsed = max(0, raw_groups - independent)
    correlation_penalty = collapsed * cfg.correlation_unit_penalty
    if momentum_support and market_raw_support and not broad_agrees:
        correlation_penalty = max(correlation_penalty, cfg.correlation_unit_penalty)
    if momentum_support and not etf_ready and continuation_score >= cfg.support_min:
        correlation_penalty = max(correlation_penalty, cfg.correlation_unit_penalty)
    warnings: List[str] = []
    if confirming_steps <= 1 and etf_ready:
        warnings.append("pair relationship is only one sample")
    if momentum_support and not etf_ready:
        warnings.append("timing and continuation share the IWM path")
    if disagreement >= 25.0:
        warnings.append("broad market disagrees")
    if not etf_ready and iwm_ready:
        warnings.append("timing measured on IWM because the ETF path is short")

    broad_only_values = [value for value in (_agree_points(spy_move, cfg), _agree_points(qqq_move, cfg)) if value is not None]
    market_for_total = sum(broad_only_values) / len(broad_only_values) if broad_only_values else 0.0
    continuation_for_total = continuation_score if etf_ready else 0.0
    context = 0.0 if candidate_score is None else _clamp(float(candidate_score))
    quality = _clamp(
        cfg.momentum_weight * momentum
        + cfg.pair_weight * pair_score
        + cfg.market_weight * market_for_total
        + cfg.timing_weight * timing_score
        + cfg.path_weight * path_score
        + cfg.continuation_weight * continuation_for_total
        + cfg.context_weight * context
        - cfg.extension_penalty_weight * extension
        - cfg.disagreement_penalty_weight * disagreement
        - cfg.correlation_penalty_weight * correlation_penalty
    )

    severe_exhaustion = exhaustion >= cfg.severe_exhaustion or (
        timing_phase == "EXHAUSTED" and extension >= 60.0
    )
    strong_reversal = reversal >= cfg.strong_reversal or phase == PHASE_REVERSAL_RISK
    poor_timing = timing_score <= cfg.poor_timing and extension >= 60.0
    reason = "weak_or_mixed_evidence"
    tier = CandidateQualityTier.C
    if strong_reversal:
        tier = CandidateQualityTier.REJECT
        reason = "reversal_risk"
        warnings.append("strong reversal risk")
    elif severe_exhaustion:
        tier = CandidateQualityTier.REJECT
        reason = "severe_exhaustion"
        warnings.append("severe exhaustion")
    elif contradictory and pair_score <= cfg.contradictory_pair_max:
        tier = CandidateQualityTier.REJECT
        reason = "contradictory_pair"
        warnings.append("pair structure contradicts the candidate")
    elif extreme_disagreement:
        tier = CandidateQualityTier.REJECT
        reason = "extreme_disagreement"
        warnings.append("extreme broad-market disagreement")
    elif poor_timing:
        tier = CandidateQualityTier.REJECT
        reason = "poor_timing"
        warnings.append("entry timing is exhausted")
    elif (
        quality >= cfg.tier_a_min_score
        and independent >= cfg.tier_a_min_evidence
        and (pair_support or not cfg.tier_a_requires_pair)
    ):
        tier = CandidateQualityTier.A
        reason = "independent_evidence"
    elif quality >= cfg.tier_b_min_score and independent >= cfg.tier_b_min_evidence:
        tier = CandidateQualityTier.B
        reason = "moderate_evidence"
    action = _ACTION_FOR_TIER[tier]
    passed = tier.value in cfg.accept_tiers and tier is not CandidateQualityTier.REJECT
    return CandidateQualityResult(
        quality_score=round(quality, 1),
        quality_tier=tier,
        evidence_count=raw_groups,
        independent_confirmation_count=independent,
        momentum_quality_score=_round1(momentum),
        pair_structure_score=_round1(pair_score),
        market_alignment_score=_round1(market_score),
        timing_quality_score=_round1(timing_score),
        path_consistency_score=_round1(path_score),
        continuation_quality_score=_round1(continuation_score),
        extension_penalty=_round1(extension),
        disagreement_penalty=_round1(disagreement),
        correlated_evidence_penalty=_round1(correlation_penalty),
        effective_evidence_count=independent,
        rank_passed=passed,
        rank_reason=reason,
        warnings=warnings,
        ranked_candidate_action=action.value,
        continuation_probability=round(probability, 1),
        momentum_phase=phase,
        timing_phase=timing_phase,
    )


def _momentum_signed(samples: Sequence[_Point], sign: int, config: CandidateQualityConfig) -> float:
    if sign >= 0:
        return _momentum(samples, config)
    flipped = [(t, 1.0 / price) for t, price in samples if price > 0]
    return _momentum(flipped, config)
