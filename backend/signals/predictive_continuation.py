"""
Predictive continuation layer (Checkpoint 2.20). Shadow analytics and replay only.

Scores whether an existing TNA or TZA candidate is likely to keep moving for the
next 30-60 seconds. It never creates a signal, never changes conservative_v2 or
balanced_v3_shadow decisions, and never submits an order or writes a database.

orders_submitted: 0
writes_to_database: false
analytics_only: true
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from backend.signals.models import MarketSnapshot, SymbolQuote

PHASE_EARLY = "EARLY"
PHASE_DEVELOPING = "DEVELOPING"
PHASE_EXHAUSTION = "EXHAUSTION"
PHASE_REVERSAL_RISK = "REVERSAL_RISK"
MOMENTUM_PHASES = frozenset({PHASE_EARLY, PHASE_DEVELOPING, PHASE_EXHAUSTION, PHASE_REVERSAL_RISK})

CANDIDATE_SYMBOLS = frozenset({"TNA", "TZA"})
REASON_HEALTHY = "healthy_continuation"
REASON_EARLY = "early_momentum"
REASON_EXHAUSTION = "momentum_exhaustion"
REASON_REVERSAL = "reversal_risk"
REASON_WEAK = "weak_continuation"
REASON_INSUFFICIENT = "insufficient_path"

ORDERS_SUBMITTED = 0
WRITES_TO_DATABASE = False
ANALYTICS_ONLY = True

_Point = Tuple[float, float]


@dataclass(frozen=True)
class PredictiveContinuationConfig:
    """Weights and move thresholds. Positive weights are shares of a 0-100 score.

    continuation_probability =
        velocity_weight * velocity_score
        + acceleration_weight * acceleration_score
        + persistence_weight * persistence_score
        + confirmation_weight * confirmation_score
        - exhaustion_penalty_weight * exhaustion_score
        - reversal_penalty_weight * reversal_risk_score
    The result is clamped to 0-100. A candidate passes when that probability
    is at least pass_threshold.
    """

    velocity_weight: float = 0.25
    acceleration_weight: float = 0.25
    persistence_weight: float = 0.20
    confirmation_weight: float = 0.15
    exhaustion_penalty_weight: float = 0.20
    reversal_penalty_weight: float = 0.25
    pass_threshold: float = 55.0
    strong_half_move_pct: float = 0.06
    accel_full_pct: float = 0.04
    large_initial_move_pct: float = 0.12
    fade_ratio: float = 0.35
    extension_pct: float = 0.20
    reversal_move_pct: float = 0.03
    final_seconds: float = 10.0
    early_total_move_pct: float = 0.06
    exhaustion_phase_min: float = 60.0
    reversal_phase_min: float = 60.0
    confirmation_move_pct: float = 0.03
    confirmation_soft_pct: float = 0.01
    min_span_seconds: float = 8.0
    flat_step_pct: float = 0.002
    disagreement_pct: float = 0.02

    def validate(self) -> "PredictiveContinuationConfig":
        weights = (
            self.velocity_weight,
            self.acceleration_weight,
            self.persistence_weight,
            self.confirmation_weight,
            self.exhaustion_penalty_weight,
            self.reversal_penalty_weight,
        )
        problems: List[str] = []
        if any(weight < 0.0 or weight > 1.0 for weight in weights):
            problems.append("score weights must be in [0, 1]")
        if not 0.0 < self.pass_threshold <= 100.0:
            problems.append("pass_threshold must be in (0, 100]")
        positive_thresholds = (
            self.strong_half_move_pct,
            self.accel_full_pct,
            self.large_initial_move_pct,
            self.extension_pct,
            self.reversal_move_pct,
            self.final_seconds,
            self.early_total_move_pct,
            self.confirmation_move_pct,
            self.confirmation_soft_pct,
            self.min_span_seconds,
            self.disagreement_pct,
        )
        if any(value <= 0.0 for value in positive_thresholds):
            problems.append("move thresholds must be > 0")
        if not 0.0 < self.fade_ratio <= 1.0:
            problems.append("fade_ratio must be in (0, 1]")
        if not 0.0 <= self.exhaustion_phase_min <= 100.0 or not 0.0 <= self.reversal_phase_min <= 100.0:
            problems.append("phase minimums must be in [0, 100]")
        if self.flat_step_pct < 0.0:
            problems.append("flat_step_pct must be >= 0")
        if problems:
            raise ValueError("Invalid predictive continuation config: " + "; ".join(problems))
        return self


@dataclass(frozen=True)
class PredictiveContinuationResult:
    continuation_probability: float
    momentum_phase: str
    acceleration_score: float
    velocity_score: float
    persistence_score: float
    exhaustion_score: float
    reversal_risk_score: float
    prediction_passed: bool
    prediction_reason: str

    def to_dict(self) -> dict:
        return {
            "continuation_probability": self.continuation_probability,
            "momentum_phase": self.momentum_phase,
            "acceleration_score": self.acceleration_score,
            "velocity_score": self.velocity_score,
            "persistence_score": self.persistence_score,
            "exhaustion_score": self.exhaustion_score,
            "reversal_risk_score": self.reversal_risk_score,
            "prediction_passed": self.prediction_passed,
            "prediction_reason": self.prediction_reason,
            "orders_submitted": ORDERS_SUBMITTED,
            "writes_to_database": WRITES_TO_DATABASE,
            "analytics_only": ANALYTICS_ONLY,
        }


@dataclass(frozen=True)
class _WindowSlice:
    first: Optional[float]
    second: Optional[float]
    final: Optional[float]
    total: Optional[float]
    span: float
    sufficient: bool
    failed_extreme: bool


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _round1(value: float) -> float:
    return round(_clamp(value), 1)


def _signed_pct(later: Optional[float], earlier: Optional[float], sign: int) -> Optional[float]:
    if later is None or earlier is None or earlier <= 0:
        return None
    return (later - earlier) / earlier * 100.0 * sign


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


def _path(quote: Optional[SymbolQuote]) -> List[_Point]:
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


def _failed_extreme(samples: Sequence[_Point], sign: int, mid_t: float) -> bool:
    if len(samples) < 2:
        return False
    end = samples[-1][1]
    if sign > 0:
        peak_t, peak_px = max(samples, key=lambda item: (item[1], -item[0]))
        return peak_t < mid_t - 1.0 and end < peak_px * (1.0 - 0.0008)
    trough_t, trough_px = min(samples, key=lambda item: (item[1], item[0]))
    return trough_t < mid_t - 1.0 and end > trough_px * (1.0 + 0.0008)


def _window_slice(quote: Optional[SymbolQuote], sign: int, config: PredictiveContinuationConfig) -> _WindowSlice:
    samples = _path(quote)
    if len(samples) < 2:
        return _WindowSlice(None, None, None, None, 0.0, False, False)
    t0, p0 = samples[0]
    t1, p1 = samples[-1]
    span = t1 - t0
    total = _signed_pct(p1, p0, sign)
    if span < config.min_span_seconds or p0 <= 0:
        return _WindowSlice(None, None, None, total, span, False, False)
    mid_t = t0 + span / 2.0
    midpoint = _price_at(samples, mid_t)
    final_anchor = _price_at(samples, max(t0, t1 - config.final_seconds))
    return _WindowSlice(
        first=_signed_pct(midpoint, p0, sign),
        second=_signed_pct(p1, midpoint, sign),
        final=_signed_pct(p1, final_anchor, sign),
        total=total,
        span=span,
        sufficient=True,
        failed_extreme=_failed_extreme(samples, sign, mid_t),
    )


def _velocity_score(first: Optional[float], second: Optional[float], config: PredictiveContinuationConfig) -> float:
    if second is None:
        return 0.0
    magnitude = _clamp(second / config.strong_half_move_pct, 0.0, 1.0) * 70.0
    if first is None:
        shape = 30.0 if second > 0 else 0.0
    elif second > 0 and first > 0 and second >= first * 1.2:
        shape = 30.0
    elif second > 0 and second >= first:
        shape = 24.0
    elif second > 0:
        shape = 12.0
    else:
        shape = 0.0
    return _clamp(magnitude + shape)


def _acceleration_score(first: Optional[float], second: Optional[float], config: PredictiveContinuationConfig) -> float:
    if first is None or second is None:
        return 0.0
    delta = second - first
    if second > 0 and delta > 0:
        return _clamp(55.0 + _clamp(delta / config.accel_full_pct, 0.0, 1.0) * 45.0)
    if second > 0 and delta > -0.01:
        return 50.0
    if second > 0:
        return 30.0
    return _clamp(20.0 + delta / 0.05 * 20.0, 0.0, 25.0)


def _persistence_score(quote: Optional[SymbolQuote], sign: int, config: PredictiveContinuationConfig) -> float:
    samples = _path(quote)
    if len(samples) < 2:
        return 0.0
    good = 0
    total = 0
    for (_, earlier), (_, later) in zip(samples, samples[1:]):
        if earlier <= 0:
            continue
        raw = (later - earlier) / earlier * 100.0
        total += 1
        if abs(raw) < config.flat_step_pct:
            continue
        if raw * sign > 0:
            good += 1
    if total == 0:
        return 0.0
    return good / total * 100.0


def _window_move(quote: Optional[SymbolQuote], sign: int) -> Optional[float]:
    if quote is None:
        return None
    if quote.mid is not None and quote.first_mid is not None:
        return _signed_pct(quote.mid, quote.first_mid, sign)
    samples = _path(quote)
    if len(samples) < 2:
        return None
    return _signed_pct(samples[-1][1], samples[0][1], sign)


def _confirmation_score(
    quotes: dict,
    *,
    symbol: str,
    iwm_sign: int,
    config: PredictiveContinuationConfig,
) -> float:
    inverse = "TZA" if symbol == "TNA" else "TNA"
    iwm = _window_move(quotes.get("IWM"), iwm_sign)
    favored = _window_move(quotes.get(symbol), 1)
    other = _window_move(quotes.get(inverse), -1)
    spy = _window_move(quotes.get("SPY"), iwm_sign)
    qqq = _window_move(quotes.get("QQQ"), iwm_sign)
    score = 0.0
    if iwm is not None:
        if iwm >= config.confirmation_move_pct:
            score += 40.0
        elif iwm >= config.confirmation_soft_pct:
            score += 25.0
        elif iwm > 0:
            score += 10.0
    if (
        favored is not None
        and other is not None
        and favored >= config.confirmation_soft_pct
        and other >= config.confirmation_soft_pct
    ):
        score += 35.0
    elif favored is not None and favored >= config.confirmation_soft_pct:
        score += 15.0
    broad = sum(1 for move in (spy, qqq) if move is not None and move >= config.confirmation_soft_pct)
    if broad == 2:
        score += 25.0
    elif broad == 1:
        score += 12.0
    return _clamp(score)


def _exhaustion_score(window: _WindowSlice, config: PredictiveContinuationConfig) -> float:
    first, second, total = window.first, window.second, window.total
    if second is None or second <= -config.reversal_move_pct:
        return 0.0
    score = 0.0
    if first is not None and first >= config.large_initial_move_pct and second < max(0.02, first * config.fade_ratio):
        fade = (first - max(second, 0.0)) / first if first else 0.0
        score = max(score, 70.0 + _clamp(fade, 0.0, 1.0) * 30.0)
    elif first is not None and first >= 0.08 and second < first * 0.4:
        score = max(score, 60.0)
    if window.failed_extreme and first is not None and first >= 0.10:
        score = max(score, 65.0)
    if (
        first is not None
        and total is not None
        and total >= config.extension_pct
        and second < first * 0.5
    ):
        score = max(score, 70.0)
    return _clamp(score)


def _reversal_score(
    window: _WindowSlice,
    *,
    disagreement: bool,
    config: PredictiveContinuationConfig,
) -> float:
    score = 0.0
    if window.final is not None and window.final <= -config.reversal_move_pct:
        severity = _clamp(abs(window.final) / (config.reversal_move_pct * 2.5), 0.0, 1.0)
        score = max(score, 60.0 + severity * 40.0)
    if window.second is not None and window.second <= -config.reversal_move_pct:
        severity = _clamp(abs(window.second) / (config.reversal_move_pct * 2.5), 0.0, 1.0)
        score = max(score, 70.0 + severity * 30.0)
    if disagreement:
        score = max(score, 75.0)
    if window.total is not None and window.total <= -config.reversal_move_pct:
        score = max(score, 80.0)
    return _clamp(score)


def _phase(
    *,
    total: Optional[float],
    first: Optional[float],
    second: Optional[float],
    exhaustion: float,
    reversal: float,
    config: PredictiveContinuationConfig,
) -> str:
    if reversal >= config.reversal_phase_min and reversal >= exhaustion:
        return PHASE_REVERSAL_RISK
    if exhaustion >= config.exhaustion_phase_min:
        return PHASE_EXHAUSTION
    if reversal >= config.reversal_phase_min:
        return PHASE_REVERSAL_RISK
    accelerating = second is not None and second > 0 and (first is None or second >= first)
    if total is not None and total < config.early_total_move_pct and accelerating:
        return PHASE_EARLY
    return PHASE_DEVELOPING


def _reason(phase: str, passed: bool, exhaustion: float, reversal: float, config: PredictiveContinuationConfig) -> str:
    if passed and phase == PHASE_EARLY:
        return REASON_EARLY
    if passed:
        return REASON_HEALTHY
    if phase == PHASE_REVERSAL_RISK or (reversal >= exhaustion and reversal >= config.reversal_phase_min):
        return REASON_REVERSAL
    if phase == PHASE_EXHAUSTION or exhaustion >= config.exhaustion_phase_min:
        return REASON_EXHAUSTION
    return REASON_WEAK


def _insufficient(symbol: str) -> PredictiveContinuationResult:
    del symbol
    return PredictiveContinuationResult(
        continuation_probability=0.0,
        momentum_phase=PHASE_EARLY,
        acceleration_score=0.0,
        velocity_score=0.0,
        persistence_score=0.0,
        exhaustion_score=0.0,
        reversal_risk_score=0.0,
        prediction_passed=False,
        prediction_reason=REASON_INSUFFICIENT,
    )


def evaluate_predictive_continuation(
    snapshot: MarketSnapshot,
    candidate_symbol: Optional[str],
    *,
    config: Optional[PredictiveContinuationConfig] = None,
) -> Optional[PredictiveContinuationResult]:
    """Score one existing TNA/TZA candidate. Returns None when there is no candidate."""
    if candidate_symbol is None:
        return None
    symbol = str(candidate_symbol).upper()
    if symbol not in CANDIDATE_SYMBOLS:
        return None
    cfg = (config or PredictiveContinuationConfig()).validate()
    iwm_sign = 1 if symbol == "TNA" else -1
    quotes = {name: snapshot.get(name) for name in ("TNA", "TZA", "IWM", "SPY", "QQQ")}
    etf = _window_slice(quotes.get(symbol), 1, cfg)
    iwm = _window_slice(quotes.get("IWM"), iwm_sign, cfg)
    primary = etf if etf.sufficient else iwm
    if not primary.sufficient:
        return _insufficient(symbol)

    disagreement = False
    if etf.sufficient and iwm.sufficient and etf.second is not None and iwm.second is not None:
        if (etf.second >= cfg.disagreement_pct and iwm.second <= -cfg.disagreement_pct) or (
            iwm.second >= cfg.disagreement_pct and etf.second <= -cfg.disagreement_pct
        ):
            disagreement = True

    velocity = _velocity_score(primary.first, primary.second, cfg)
    acceleration = _acceleration_score(primary.first, primary.second, cfg)
    persistence_quote = quotes.get(symbol) if etf.sufficient else quotes.get("IWM")
    persistence_sign = 1 if etf.sufficient else iwm_sign
    persistence = _persistence_score(persistence_quote, persistence_sign, cfg)
    confirmation = _confirmation_score(quotes, symbol=symbol, iwm_sign=iwm_sign, config=cfg)
    exhaustion = _exhaustion_score(primary, cfg)
    reversal = _reversal_score(primary, disagreement=disagreement, config=cfg)
    raw = (
        cfg.velocity_weight * velocity
        + cfg.acceleration_weight * acceleration
        + cfg.persistence_weight * persistence
        + cfg.confirmation_weight * confirmation
        - cfg.exhaustion_penalty_weight * exhaustion
        - cfg.reversal_penalty_weight * reversal
    )
    probability = round(_clamp(raw), 1)
    phase = _phase(
        total=primary.total,
        first=primary.first,
        second=primary.second,
        exhaustion=exhaustion,
        reversal=reversal,
        config=cfg,
    )
    passed = probability >= cfg.pass_threshold
    return PredictiveContinuationResult(
        continuation_probability=probability,
        momentum_phase=phase,
        acceleration_score=_round1(acceleration),
        velocity_score=_round1(velocity),
        persistence_score=_round1(persistence),
        exhaustion_score=_round1(exhaustion),
        reversal_risk_score=_round1(reversal),
        prediction_passed=passed,
        prediction_reason=_reason(phase, passed, exhaustion, reversal, cfg),
    )


def _fmt_score(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    rounded = round(float(value), 1)
    if rounded == int(rounded):
        return str(int(rounded))
    return f"{rounded:.1f}"


def format_shadow_lines(
    symbol: str,
    candidate_score: Optional[float],
    result: PredictiveContinuationResult,
) -> List[str]:
    """Example shadow block for one existing candidate. Analytics only."""
    return [
        "--- predictive continuation (analytics only) ---",
        "candidate:",
        symbol,
        "candidate_score:",
        _fmt_score(candidate_score),
        "continuation_probability:",
        _fmt_score(result.continuation_probability),
        "phase:",
        result.momentum_phase,
        "acceleration:",
        _fmt_score(result.acceleration_score),
        "velocity:",
        _fmt_score(result.velocity_score),
        "exhaustion:",
        _fmt_score(result.exhaustion_score),
        "reversal_risk:",
        _fmt_score(result.reversal_risk_score),
        "prediction:",
        "PASS" if result.prediction_passed else "SKIP",
        "orders_submitted: 0",
        "writes_to_database: false",
        "analytics_only: true",
    ]
