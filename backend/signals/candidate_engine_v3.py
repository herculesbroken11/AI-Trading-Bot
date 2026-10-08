"""
Candidate Engine v3 (balanced_v3_shadow) — shadow and replay only.

Produces a TNA candidate, a TZA candidate, or SKIP from short-window movement.
SPY/QQQ session-open and previous-close disagreement is not a hard block.
Those indexes are judged on the same short window as IWM and the TNA/TZA pair.
A strong opposite move inside that window is a SKIP.

This module never imports execution code and never places orders.
It refuses to construct itself for any mode other than shadow or replay.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from backend.config.settings import ConfigurationError, Settings
from backend.signals.models import (
    REQUIRED_SIGNAL_SYMBOLS,
    VOLATILITY_SYMBOL,
    MarketSnapshot,
    SignalDecision,
    SignalDirection,
    SignalQuality,
    SignalReason,
    SignalScoreBreakdown,
    SymbolQuote,
)
from backend.signals.profiles import BALANCED_V3_SHADOW, PROFILE_SCORE_FIELDS
from backend.signals.tna_tza_signal_engine import SignalEngineConfig, TnaTzaSignalEngine

ENGINE_VERSION = "balanced_v3_shadow"
ALLOWED_MODES = frozenset({"shadow", "replay"})

IWM_POINTS = 30.0
PAIR_POINTS = 25.0
RELATIVE_POINTS = 20.0
BROAD_POINTS = 15.0
VIX_POINTS = 10.0
RELATIVE_FULL_EDGE_PCT = 0.30


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _pct(value: Optional[float], reference: Optional[float]) -> Optional[float]:
    if value is None or reference is None or reference <= 0:
        return None
    return (value - reference) / reference * 100.0


def _window_move(quote: Optional[SymbolQuote]) -> Optional[float]:
    if quote is None:
        return None
    return _pct(quote.mid, quote.first_mid)


def _fmt(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:+.3f}%"


def _as_float(value: object) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


@dataclass(frozen=True)
class CandidateEngineV3Config:
    """Fixed balanced thresholds. Entry stays at 70; environment cannot lower it."""

    max_quote_age_seconds: float = 1.0
    max_tradable_spread_pct: float = 0.30
    momentum_threshold_pct: float = 0.03
    entry_score_threshold: float = 70.0
    vix_calm_level: float = 20.0
    vix_high_level: float = 25.0
    vix_extreme_level: float = 35.0
    vix_rising_pct: float = 5.0
    flat_band_pct: float = 0.02
    rise_min_pct: float = 0.02
    pair_confirm_spread_pct: float = 0.08
    strong_oppose_pct: float = 0.05
    very_strong_oppose_pct: float = 0.15
    severe_chop_score: float = 60.0
    pullback_block_score: float = 40.0
    direction_flip_extra_points: float = 10.0
    # Mid-window must already show at least this fraction of the momentum threshold.
    mid_persist_fraction: float = 0.5

    def validate(self) -> "CandidateEngineV3Config":
        problems: List[str] = []
        if not 0 < self.max_quote_age_seconds <= 5.0:
            problems.append("max_quote_age_seconds must be in (0, 5]")
        if not 0.0 < self.max_tradable_spread_pct <= 2.0:
            problems.append("max_tradable_spread_pct must be in (0, 2]")
        if self.momentum_threshold_pct <= 0:
            problems.append("momentum_threshold_pct must be > 0")
        if self.entry_score_threshold < 70.0 or self.entry_score_threshold > 100.0:
            problems.append("entry_score_threshold must stay in [70, 100]")
        if not 0 < self.vix_calm_level <= self.vix_high_level < self.vix_extreme_level:
            problems.append("VIX levels must satisfy 0 < calm <= high < extreme")
        if self.severe_chop_score < 50.0:
            problems.append("severe_chop_score cannot be loosened below 50")
        if self.pullback_block_score < 40.0:
            problems.append("pullback_block_score cannot be loosened below 40")
        if self.strong_oppose_pct <= 0 or self.very_strong_oppose_pct < self.strong_oppose_pct:
            problems.append("opposition thresholds must satisfy 0 < strong <= very_strong")
        if self.direction_flip_extra_points < 10.0:
            problems.append("direction_flip_extra_points cannot be loosened below 10")
        if not 0 < self.mid_persist_fraction <= 1.0:
            problems.append("mid_persist_fraction must be in (0, 1]")
        if problems:
            raise ConfigurationError("Unsafe balanced_v3_shadow config: " + "; ".join(problems))
        return self

    @classmethod
    def from_settings(
        cls,
        settings: Optional[Settings] = None,
        *,
        max_quote_age_seconds: Optional[float] = None,
    ) -> "CandidateEngineV3Config":
        age = cls.max_quote_age_seconds
        if max_quote_age_seconds is not None:
            age = float(max_quote_age_seconds)
        elif settings is not None:
            age = float(settings.stream_max_quote_age_seconds)
        return cls(max_quote_age_seconds=age).validate()

    def to_dict(self) -> Dict[str, object]:
        return {
            "profile": BALANCED_V3_SHADOW,
            "shadow_only": True,
            "max_quote_age_seconds": self.max_quote_age_seconds,
            "max_tradable_spread_pct": self.max_tradable_spread_pct,
            "momentum_threshold_pct": self.momentum_threshold_pct,
            "entry_score_threshold": self.entry_score_threshold,
            "vix_calm_level": self.vix_calm_level,
            "vix_high_level": self.vix_high_level,
            "vix_extreme_level": self.vix_extreme_level,
            "strong_oppose_pct": self.strong_oppose_pct,
            "very_strong_oppose_pct": self.very_strong_oppose_pct,
            "severe_chop_score": self.severe_chop_score,
            "pullback_block_score": self.pullback_block_score,
        }


@dataclass
class _SideScore:
    direction: int
    name: str
    iwm_momentum_score: float
    pair_confirmation_score: float
    relative_strength_score: float
    broad_window_score: float
    vix_score: float
    chop_risk_score: float
    pullback_risk_score: float
    candidate_score: float
    block_reason: Optional[SignalReason]
    detail: str
    metrics: Dict[str, object]

    @property
    def eligible(self) -> bool:
        return self.block_reason is None


def _iwm_momentum_score(signed: Optional[float], threshold: float) -> float:
    if signed is None or signed < threshold:
        return 0.0
    fraction = min(1.0, (signed - threshold) / threshold)
    return round(15.0 + 15.0 * fraction, 1)


def _pair_confirmation_score(
    favoured: Optional[float],
    inverse: Optional[float],
    *,
    threshold: float,
    flat_band: float,
    rise_min: float,
    pair_spread_min: float,
) -> float:
    """Favoured ETF must be rising, or the pair spread must confirm, and the inverse ETF must be flat or down."""
    if favoured is None or inverse is None:
        return 0.0
    if inverse > flat_band:
        return 0.0
    spread = favoured - inverse
    if favoured >= threshold and inverse <= -threshold:
        return PAIR_POINTS
    if favoured >= rise_min and inverse <= -threshold:
        return 22.0
    if favoured >= threshold:
        return 18.0
    if spread >= pair_spread_min and favoured >= 0.0:
        return 16.0
    if favoured >= rise_min:
        return 12.0
    return 0.0


def _relative_strength_score(favoured: Optional[float], inverse: Optional[float]) -> float:
    if favoured is None or inverse is None:
        return 0.0
    edge = favoured - inverse
    return round(RELATIVE_POINTS * min(1.0, max(0.0, edge) / RELATIVE_FULL_EDGE_PCT), 1)


def _broad_credit(signed: Optional[float], threshold: float) -> float:
    half = BROAD_POINTS / 2.0
    if signed is None:
        return 4.0
    if signed >= threshold:
        return half
    if signed >= 0:
        return 5.0
    if signed >= -threshold:
        return 3.0
    return 1.0


def _broad_window_score(spy_signed: Optional[float], qqq_signed: Optional[float], threshold: float) -> float:
    return round(_broad_credit(spy_signed, threshold) + _broad_credit(qqq_signed, threshold), 1)


def _strongly_opposed(
    spy_signed: Optional[float],
    qqq_signed: Optional[float],
    *,
    each: float,
    very: float,
) -> bool:
    """True when both indexes are against the trade, or one is very strongly against, inside the window."""
    if spy_signed is not None and qqq_signed is not None and spy_signed <= -each and qqq_signed <= -each:
        return True
    return any(move is not None and move <= -very for move in (spy_signed, qqq_signed))


def _window_range(quote: Optional[SymbolQuote]) -> Optional[float]:
    if quote is None or quote.window_high_mid is None or quote.window_low_mid is None:
        return None
    return _pct(quote.window_high_mid, quote.window_low_mid)


def _retrace(quote: Optional[SymbolQuote], sign: int, threshold: float) -> Optional[float]:
    if quote is None or quote.first_mid is None:
        return None
    peak = quote.window_high_mid if sign == 1 else quote.window_low_mid
    peak_move = None if peak is None else _pct(peak, quote.first_mid)
    current = _window_move(quote)
    if peak_move is None or current is None:
        return None
    peak_signed = peak_move * sign
    current_signed = current * sign
    if peak_signed < threshold or peak_signed <= 0:
        return None
    return max(0.0, (peak_signed - current_signed) / peak_signed)


def _chop_risk_score(raw_move: Optional[float], window_range: Optional[float]) -> float:
    if window_range is None or raw_move is None:
        return 0.0
    if window_range >= 0.12 and abs(raw_move) < max(0.03, window_range * 0.35):
        return 80.0
    if window_range >= 0.08 and abs(raw_move) < 0.03:
        return 65.0
    return 0.0


def _pullback_risk_score(signed: Optional[float], retrace: Optional[float]) -> float:
    if retrace is not None and retrace >= 0.60:
        return 80.0
    if signed is not None and signed < 0:
        return 70.0
    if retrace is not None and retrace >= 0.40:
        return 50.0
    if retrace is not None and retrace >= 0.25:
        return 25.0
    return 0.0


_OMITTED = object()
STABILITY_FIELDS = (
    "window_start_move",
    "window_mid_move",
    "window_final_move",
    "final_10s_move",
    "final_10s_reversal",
    "selected_etf_final_10s_move",
    "pair_final_10s_confirmation",
)
MIN_PATH_SPAN_SECONDS = 8.0


def _samples(quote: Optional[SymbolQuote]) -> List[Tuple[float, float]]:
    if quote is None or not quote.mid_path:
        return []
    return [(float(t), float(mid)) for t, mid in quote.mid_path if mid is not None and mid > 0]


def _move_near(samples: List[Tuple[float, float]], first: Optional[float], target_t: float) -> Optional[float]:
    if not samples or first is None or first <= 0:
        return None
    point = min(samples, key=lambda item: abs(item[0] - target_t))
    if abs(point[0] - target_t) > 3.0:
        return None
    return _pct(point[1], first)


def _final_10s_move(quote: Optional[SymbolQuote], samples: List[Tuple[float, float]]) -> Optional[float]:
    if quote is None or quote.mid is None or not samples:
        return None
    end_t = samples[-1][0]
    if end_t < 10.0:
        return None
    target = end_t - 10.0
    point = min(samples, key=lambda item: abs(item[0] - target))
    if abs(point[0] - target) > 2.5 or point[1] <= 0:
        return None
    return _pct(quote.mid, point[1])


def _stability_metrics(
    quotes: Mapping[str, Optional[SymbolQuote]],
    sign: int,
    favoured_symbol: str,
    inverse_symbol: str,
    *,
    threshold: float,
    flat_band: float,
    persist_fraction: float,
) -> Dict[str, object]:
    """Raw window percents. Positive means that symbol rose. Missing path leaves the gates unevaluated."""
    iwm = quotes.get("IWM")
    favoured = quotes.get(favoured_symbol)
    inverse = quotes.get(inverse_symbol)
    iwm_samples = _samples(iwm)
    fav_samples = _samples(favoured)
    inv_samples = _samples(inverse)
    span = iwm_samples[-1][0] if iwm_samples else 0.0
    have_mid = span >= MIN_PATH_SPAN_SECONDS
    start = _move_near(iwm_samples, iwm.first_mid if iwm else None, span * 0.25) if have_mid else None
    mid = _move_near(iwm_samples, iwm.first_mid if iwm else None, span * 0.50) if have_mid else None
    etf_mid = _move_near(fav_samples, favoured.first_mid if favoured else None, span * 0.50) if have_mid else None
    final = _window_move(iwm)
    iwm_10s = _final_10s_move(iwm, iwm_samples)
    etf_10s = _final_10s_move(favoured, fav_samples)
    inv_10s = _final_10s_move(inverse, inv_samples)
    reversal = None
    if iwm_10s is not None or etf_10s is not None:
        iwm_against = iwm_10s is not None and iwm_10s * sign <= -threshold
        etf_against = etf_10s is not None and etf_10s <= -threshold
        reversal = iwm_against or etf_against
    pair_ok = None
    if etf_10s is not None and inv_10s is not None:
        pair_ok = etf_10s >= -flat_band and inv_10s <= flat_band
    persistence_failed = None
    if have_mid or reversal is not None:
        failed = bool(reversal)
        if mid is not None and mid * sign < threshold * persist_fraction:
            failed = True
        if etf_mid is not None and etf_mid <= -threshold:
            failed = True
        if not have_mid and reversal is False:
            failed = False
        persistence_failed = failed
    return {
        "window_start_move": start,
        "window_mid_move": mid,
        "window_final_move": final,
        "final_10s_move": iwm_10s,
        "final_10s_reversal": reversal,
        "selected_etf_final_10s_move": etf_10s,
        "pair_final_10s_confirmation": pair_ok,
        "_persistence_failed": persistence_failed,
        "_etf_mid_move": etf_mid,
    }


def _empty_scores(reason: str) -> Dict[str, object]:
    scores: Dict[str, object] = {name: 0.0 for name in PROFILE_SCORE_FIELDS}
    scores["profile"] = BALANCED_V3_SHADOW
    scores["direction"] = SignalDirection.SKIP.value
    scores["final_reason"] = reason
    return scores


class CandidateEngineV3:
    """TNA / TZA / SKIP from short-window confirmation. Shadow and replay only."""

    IS_SHADOW_ONLY = True
    ALLOWS_EXECUTION = False
    IS_SIGNAL_ONLY = True
    PROFILE = BALANCED_V3_SHADOW
    ENGINE_VERSION = ENGINE_VERSION

    def __init__(
        self,
        config: Optional[CandidateEngineV3Config] = None,
        *,
        wall_clock: Callable[[], datetime] = _utc_now,
        mode: str = "shadow",
    ) -> None:
        if mode not in ALLOWED_MODES:
            raise ConfigurationError(
                "balanced_v3_shadow can only run in shadow or replay mode and cannot authorize orders"
            )
        self.mode = mode
        self._config = (config or CandidateEngineV3Config()).validate()
        self._wall_clock = wall_clock
        self._previous_direction: Optional[str] = None

    @property
    def config(self) -> CandidateEngineV3Config:
        return self._config

    def note_shadow_cycle(self, decision: SignalDecision) -> None:
        """Remember the last logged shadow candidate so the next cycle can apply the flip cooldown."""
        if decision.decision in (SignalDirection.BULLISH, SignalDirection.BEARISH):
            self._previous_direction = decision.decision.value
        else:
            self._previous_direction = None

    def decide(
        self,
        snapshot: MarketSnapshot,
        *,
        previous_direction: Any = _OMITTED,
        apply_path_filters: bool = True,
    ) -> SignalDecision:
        cfg = self._config
        created_at = self._wall_clock()
        freshness = self._freshness(snapshot)

        def finish(
            *,
            direction: SignalDirection,
            symbol: Optional[str],
            reason: Optional[SignalReason],
            explanation: str,
            gate_passed: bool,
            regime: str,
            bull: float,
            bear: float,
            scores: Mapping[str, object],
            breakdown: Optional[SignalScoreBreakdown] = None,
            quality: Optional[SignalQuality] = None,
            warnings: Optional[List[str]] = None,
        ) -> SignalDecision:
            winning = bull if direction is SignalDirection.BULLISH else bear if direction is SignalDirection.BEARISH else 0.0
            return SignalDecision(
                decision=direction,
                selected_symbol=symbol,
                confidence_score=winning,
                bullish_score=bull,
                bearish_score=bear,
                skip_reason=reason,
                quote_freshness_by_symbol=freshness,
                market_regime=regime,
                explanation=explanation,
                created_at=created_at,
                freshness_gate_passed=gate_passed,
                score_breakdown=breakdown,
                warnings=list(warnings or []),
                thresholds=self._thresholds(),
                quality=quality or SignalQuality.not_evaluated(),
                engine_version=ENGINE_VERSION,
                profile_scores=dict(scores),
            )

        required = [freshness[symbol] for symbol in REQUIRED_SIGNAL_SYMBOLS]
        missing = [status.symbol for status in required if not status.present]
        stale = [status.symbol for status in required if status.present and not status.fresh]
        if missing or stale:
            reason = SignalReason.MISSING_REQUIRED_QUOTE if missing else SignalReason.STALE_MARKET_DATA
            detail = []
            if missing:
                detail.append("missing required quote(s): " + ", ".join(missing))
            if stale:
                ages = ", ".join(f"{symbol}={freshness[symbol].age_seconds:.3f}s" for symbol in stale)
                detail.append(f"stale required quote(s) > {cfg.max_quote_age_seconds:g}s: {ages}")
            return finish(
                direction=SignalDirection.SKIP,
                symbol=None,
                reason=reason,
                explanation=f"{reason.value}: {'; '.join(detail)}. No candidate computed.",
                gate_passed=False,
                regime="unknown",
                bull=0.0,
                bear=0.0,
                scores=_empty_scores(reason.value),
            )

        quotes = {symbol: snapshot.get(symbol) for symbol in REQUIRED_SIGNAL_SYMBOLS}
        invalid = [
            symbol
            for symbol, quote in quotes.items()
            if quote is None or not quote.has_bid_ask or quote.ask < quote.bid  # type: ignore[operator]
        ]
        if invalid:
            reason = SignalReason.INVALID_QUOTE
            return finish(
                direction=SignalDirection.SKIP,
                symbol=None,
                reason=reason,
                explanation=f"one-sided or crossed quote(s): {', '.join(invalid)}",
                gate_passed=True,
                regime="unknown",
                bull=0.0,
                bear=0.0,
                scores=_empty_scores(reason.value),
            )
        wide = [
            f"{symbol} spread {quotes[symbol].spread_pct:.3f}%"  # type: ignore[union-attr]
            for symbol in ("TNA", "TZA")
            if (quotes[symbol].spread_pct or 0.0) > cfg.max_tradable_spread_pct  # type: ignore[union-attr]
        ]
        if wide:
            reason = SignalReason.WIDE_SPREAD
            return finish(
                direction=SignalDirection.SKIP,
                symbol=None,
                reason=reason,
                explanation=f"tradable ETF spread too wide (> {cfg.max_tradable_spread_pct:g}%): {', '.join(wide)}",
                gate_passed=True,
                regime="unknown",
                bull=0.0,
                bear=0.0,
                scores=_empty_scores(reason.value),
            )

        moves = {symbol: _window_move(quotes[symbol]) for symbol in REQUIRED_SIGNAL_SYMBOLS}
        if moves["IWM"] is None:
            reason = SignalReason.INSUFFICIENT_REFERENCE_DATA
            return finish(
                direction=SignalDirection.SKIP,
                symbol=None,
                reason=reason,
                explanation="IWM short-window baseline unavailable",
                gate_passed=True,
                regime="unknown",
                bull=0.0,
                bear=0.0,
                scores=_empty_scores(reason.value),
            )

        warnings: List[str] = []
        vix_score, vix_block, vix_note = self._vix(snapshot)
        if vix_note:
            warnings.append(vix_note)

        bull = self._score_side(+1, moves, quotes, vix_score)
        bear = self._score_side(-1, moves, quotes, vix_score)
        lean = bull if (moves["IWM"] or 0.0) > 0 else bear if (moves["IWM"] or 0.0) < 0 else bull
        remembered = self._previous_direction if previous_direction is _OMITTED else previous_direction
        if vix_block is not None:
            return self._skip_side(
                finish,
                lean,
                bull,
                bear,
                reason=vix_block,
                regime="high_volatility",
                warnings=warnings,
                explanation=vix_note or "VIX is not calm",
            )

        eligible = [side for side in (bull, bear) if side.eligible]
        chosen: Optional[_SideScore] = None
        if len(eligible) == 1:
            chosen = eligible[0]
        elif len(eligible) > 1:
            eligible.sort(key=lambda side: side.candidate_score, reverse=True)
            if eligible[0].candidate_score - eligible[1].candidate_score < 10.0:
                return self._skip_side(
                    finish,
                    eligible[0],
                    bull,
                    bear,
                    reason=SignalReason.UNCLEAR_MARKET_DIRECTION,
                    regime="mixed",
                    warnings=warnings,
                    explanation="bullish and bearish candidate scores are too close",
                )
            chosen = eligible[0]
        if chosen is not None:
            blocked = self._confirmation_block(chosen, apply_path_filters=apply_path_filters, previous=remembered)
            if blocked is None:
                return self._candidate(finish, chosen, bull, bear, warnings)
            reason, detail = blocked
            return self._skip_side(
                finish,
                chosen,
                bull,
                bear,
                reason=reason,
                regime="mixed",
                warnings=warnings,
                explanation=detail,
            )
        return self._skip_side(
            finish,
            lean,
            bull,
            bear,
            reason=lean.block_reason or SignalReason.INSUFFICIENT_SIGNAL_STRENGTH,
            regime="mixed",
            warnings=warnings,
            explanation=lean.detail,
        )

    def _freshness(self, snapshot: MarketSnapshot):
        gate = TnaTzaSignalEngine(
            SignalEngineConfig(max_quote_age_seconds=self._config.max_quote_age_seconds),
            wall_clock=self._wall_clock,
        )
        return gate.evaluate_freshness(snapshot)

    def _vix(self, snapshot: MarketSnapshot) -> Tuple[float, Optional[SignalReason], Optional[str]]:
        cfg = self._config
        quote = snapshot.get(VOLATILITY_SYMBOL)
        level = quote.price if quote else None
        if level is None:
            return 7.0, None, "VIX missing; volatility unknown (not required)"
        change = _pct(level, quote.prev_close if quote else None)
        rising = change is not None and change >= cfg.vix_rising_pct
        if level >= cfg.vix_extreme_level:
            return 0.0, SignalReason.HIGH_VOLATILITY, f"VIX {level:g} >= extreme level {cfg.vix_extreme_level:g}"
        if level >= cfg.vix_high_level or rising:
            change_text = "" if change is None else f", change {change:+.2f}%"
            return 0.0, SignalReason.HIGH_VOLATILITY, f"VIX {level:g} is not calm{change_text}"
        if level < cfg.vix_calm_level and (change is None or change < 2.0):
            return VIX_POINTS, None, None
        return 6.0, None, f"VIX {level:g} is acceptable but not fully calm"

    def _score_side(
        self,
        sign: int,
        moves: Mapping[str, Optional[float]],
        quotes: Mapping[str, Optional[SymbolQuote]],
        vix_score: float,
    ) -> _SideScore:
        cfg = self._config
        name = "bullish" if sign == 1 else "bearish"
        favoured_symbol, inverse_symbol = ("TNA", "TZA") if sign == 1 else ("TZA", "TNA")
        iwm = moves["IWM"]
        signed_iwm = None if iwm is None else iwm * sign
        # Raw ETF moves: positive means that ETF is rising. Symbol choice already
        # picks the favoured leg, so these are not multiplied by direction.
        favoured = moves[favoured_symbol]
        inverse = moves[inverse_symbol]
        spy = None if moves["SPY"] is None else moves["SPY"] * sign
        qqq = None if moves["QQQ"] is None else moves["QQQ"] * sign
        iwm_quote = quotes["IWM"]
        window_range = _window_range(iwm_quote)
        retrace = _retrace(iwm_quote, sign, cfg.momentum_threshold_pct)

        iwm_score = _iwm_momentum_score(signed_iwm, cfg.momentum_threshold_pct)
        pair_score = _pair_confirmation_score(
            favoured,
            inverse,
            threshold=cfg.momentum_threshold_pct,
            flat_band=cfg.flat_band_pct,
            rise_min=cfg.rise_min_pct,
            pair_spread_min=cfg.pair_confirm_spread_pct,
        )
        relative_score = _relative_strength_score(favoured, inverse)
        broad_score = _broad_window_score(spy, qqq, cfg.momentum_threshold_pct)
        chop = _chop_risk_score(iwm, window_range)
        pullback = _pullback_risk_score(signed_iwm, retrace)
        opposed = _strongly_opposed(
            spy,
            qqq,
            each=cfg.strong_oppose_pct,
            very=cfg.very_strong_oppose_pct,
        )
        total = round(min(100.0, iwm_score + pair_score + relative_score + broad_score + vix_score), 1)
        stability = _stability_metrics(
            quotes,
            sign,
            favoured_symbol,
            inverse_symbol,
            threshold=cfg.momentum_threshold_pct,
            flat_band=cfg.flat_band_pct,
            persist_fraction=cfg.mid_persist_fraction,
        )

        # Raw IWM sign is mandatory. A rising IWM cannot select TZA, and a falling IWM cannot select TNA.
        # There is no inverse-pair override.
        raw_supports = iwm is not None and (
            (sign == 1 and iwm >= cfg.momentum_threshold_pct) or (sign == -1 and iwm <= -cfg.momentum_threshold_pct)
        )
        if not raw_supports:
            reason: Optional[SignalReason] = SignalReason.INSUFFICIENT_SIGNAL_STRENGTH
            need = f">= +{cfg.momentum_threshold_pct:g}%" if sign == 1 else f"<= -{cfg.momentum_threshold_pct:g}%"
            detail = f"IWM window {_fmt(iwm)} does not support {name} (need {need})"
        elif pair_score <= 0:
            reason = SignalReason.INSUFFICIENT_SIGNAL_STRENGTH
            detail = (
                f"{favoured_symbol} {_fmt(favoured)} / {inverse_symbol} {_fmt(inverse)} "
                f"does not confirm the {name} pair (inverse ETF must be flat or down)"
            )
        elif opposed:
            reason = SignalReason.BROAD_MARKET_DISAGREEMENT
            detail = (
                f"SPY {_fmt(moves['SPY'])} and QQQ {_fmt(moves['QQQ'])} are strongly opposite "
                f"in-window for a {name} candidate"
            )
        elif pullback >= cfg.pullback_block_score:
            reason = SignalReason.PULLBACK_RISK
            retrace_text = "n/a" if retrace is None else f"{retrace:.0%}"
            detail = f"pullback_risk_score {pullback:g} (window retrace {retrace_text})"
        elif chop >= cfg.severe_chop_score:
            reason = SignalReason.CHOPPY_CONFIRMATION
            detail = f"severe chop_risk_score {chop:g} (IWM window range {_fmt(window_range)})"
        elif total < cfg.entry_score_threshold:
            reason = SignalReason.INSUFFICIENT_SIGNAL_STRENGTH
            detail = f"{name} candidate score {total:g} < {cfg.entry_score_threshold:g}"
        else:
            reason = None
            detail = (
                f"{name} candidate score {total:g}: IWM window {_fmt(iwm)}, "
                f"{favoured_symbol} {_fmt(favoured)}, {inverse_symbol} {_fmt(inverse)}, "
                f"SPY {_fmt(moves['SPY'])}, QQQ {_fmt(moves['QQQ'])}"
            )

        return _SideScore(
            direction=sign,
            name=name,
            iwm_momentum_score=iwm_score,
            pair_confirmation_score=pair_score,
            relative_strength_score=relative_score,
            broad_window_score=broad_score,
            vix_score=vix_score,
            chop_risk_score=chop,
            pullback_risk_score=pullback,
            candidate_score=total,
            block_reason=reason,
            detail=detail,
            metrics={
                "iwm_window_move_pct": iwm,
                "iwm_signed_move_pct": signed_iwm,
                "tna_window_move_pct": moves["TNA"],
                "tza_window_move_pct": moves["TZA"],
                "spy_window_move_pct": moves["SPY"],
                "qqq_window_move_pct": moves["QQQ"],
                "iwm_window_range_pct": window_range,
                "iwm_window_retrace_ratio": retrace,
                **stability,
            },
        )

    def _scores_for(self, side: _SideScore, *, direction: str, final_reason: str) -> Dict[str, object]:
        scores = {
            "profile": BALANCED_V3_SHADOW,
            "candidate_score": side.candidate_score,
            "direction": direction,
            "iwm_momentum_score": side.iwm_momentum_score,
            "pair_confirmation_score": side.pair_confirmation_score,
            "relative_strength_score": side.relative_strength_score,
            "broad_window_score": side.broad_window_score,
            "vix_score": side.vix_score,
            "chop_risk_score": side.chop_risk_score,
            "pullback_risk_score": side.pullback_risk_score,
            "final_reason": final_reason,
        }
        for name in STABILITY_FIELDS:
            scores[name] = side.metrics.get(name)
        missing = [name for name in PROFILE_SCORE_FIELDS if name not in scores]
        if missing:
            raise RuntimeError(f"profile score fields missing: {missing}")
        return scores

    def _confirmation_block(
        self,
        side: _SideScore,
        *,
        apply_path_filters: bool,
        previous: Optional[str],
    ) -> Optional[Tuple[SignalReason, str]]:
        """Shadow confirmation after a candidate already passed the direction and score gates."""
        raw = side.metrics.get("iwm_window_move_pct")
        if side.direction == 1 and (raw is None or float(raw) <= 0):
            return SignalReason.INSUFFICIENT_SIGNAL_STRENGTH, f"IWM window {_fmt(raw if isinstance(raw, (int, float)) else None)} is not positive"
        if side.direction == -1 and (raw is None or float(raw) >= 0):
            return SignalReason.INSUFFICIENT_SIGNAL_STRENGTH, f"IWM window {_fmt(raw if isinstance(raw, (int, float)) else None)} is not negative"
        if not apply_path_filters:
            return None
        if side.metrics.get("_persistence_failed") is True:
            return (
                SignalReason.LATE_REVERSAL_RISK,
                "late_reversal_risk: IWM or the selected ETF did not keep the same direction through the mid-window "
                f"and the final 10s (IWM mid {_fmt(_as_float(side.metrics.get('window_mid_move')))}, "
                f"IWM final 10s {_fmt(_as_float(side.metrics.get('final_10s_move')))}, "
                f"ETF final 10s {_fmt(_as_float(side.metrics.get('selected_etf_final_10s_move')))})",
            )
        opposite = (previous == SignalDirection.BULLISH.value and side.direction == -1) or (
            previous == SignalDirection.BEARISH.value and side.direction == 1
        )
        required = self._config.entry_score_threshold + self._config.direction_flip_extra_points
        if opposite and side.candidate_score < required:
            return (
                SignalReason.DIRECTION_FLIP_COOLDOWN,
                f"direction_flip_cooldown: {side.name} score {side.candidate_score:g} < {required:g} "
                f"after a {previous} cycle",
            )
        return None

    def _breakdown(self, bull: _SideScore, bear: _SideScore) -> SignalScoreBreakdown:
        breakdown = SignalScoreBreakdown()
        for name in (
            "iwm_momentum_score",
            "pair_confirmation_score",
            "relative_strength_score",
            "broad_window_score",
            "vix_score",
        ):
            breakdown.add(name, getattr(bull, name), getattr(bear, name), name)
        breakdown.add(
            "chop_risk_score",
            0.0,
            0.0,
            f"bull {bull.chop_risk_score:g} / bear {bear.chop_risk_score:g} (higher means more chop)",
        )
        breakdown.add(
            "pullback_risk_score",
            0.0,
            0.0,
            f"bull {bull.pullback_risk_score:g} / bear {bear.pullback_risk_score:g} (higher means more pullback risk)",
        )
        return breakdown

    def _quality(self, side: _SideScore, passed: bool, reason: Optional[SignalReason] = None) -> SignalQuality:
        gate_reason = None
        if not passed:
            gate_reason = reason.value if reason is not None else (side.block_reason.value if side.block_reason else "no_candidate")
        return SignalQuality(
            evaluated=True,
            direction=side.name,
            continuation_score=side.iwm_momentum_score,
            confirmation_score=side.pair_confirmation_score,
            chop_risk_score=side.chop_risk_score,
            pullback_risk_score=side.pullback_risk_score,
            quality_gate_passed=passed,
            quality_gate_reason=gate_reason,
            metrics=dict(side.metrics),
        )

    def _candidate(self, finish, side: _SideScore, bull: _SideScore, bear: _SideScore, warnings: List[str]):
        direction = SignalDirection.BULLISH if side.direction == 1 else SignalDirection.BEARISH
        reason = SignalReason.BULLISH_CONFIRMED if side.direction == 1 else SignalReason.BEARISH_CONFIRMED
        symbol = "TNA" if side.direction == 1 else "TZA"
        regime = "risk_on" if side.direction == 1 else "risk_off"
        if side.broad_window_score < 10.0:
            regime = "mixed"
        return finish(
            direction=direction,
            symbol=symbol,
            reason=None,
            explanation=f"{reason.value} -> {symbol}: {side.detail}",
            gate_passed=True,
            regime=regime,
            bull=bull.candidate_score,
            bear=bear.candidate_score,
            scores=self._scores_for(side, direction=direction.value, final_reason=reason.value),
            breakdown=self._breakdown(bull, bear),
            quality=self._quality(side, True),
            warnings=warnings,
        )

    def _skip_side(
        self,
        finish,
        side: _SideScore,
        bull: _SideScore,
        bear: _SideScore,
        *,
        reason: SignalReason,
        regime: str,
        warnings: List[str],
        explanation: str,
    ):
        return finish(
            direction=SignalDirection.SKIP,
            symbol=None,
            reason=reason,
            explanation=f"{reason.value}: {explanation}",
            gate_passed=True,
            regime=regime,
            bull=bull.candidate_score,
            bear=bear.candidate_score,
            scores=self._scores_for(side, direction=SignalDirection.SKIP.value, final_reason=reason.value),
            breakdown=self._breakdown(bull, bear),
            quality=self._quality(side, False, reason),
            warnings=warnings,
        )

    def _thresholds(self) -> Dict[str, float]:
        cfg = self._config
        return {
            "max_quote_age_seconds": cfg.max_quote_age_seconds,
            "entry_score_threshold": cfg.entry_score_threshold,
            "momentum_threshold_pct": cfg.momentum_threshold_pct,
            "max_tradable_spread_pct": cfg.max_tradable_spread_pct,
        }
