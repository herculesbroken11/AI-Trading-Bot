"""
TNA / TZA Signal Engine (v1 Checkpoint 2.11; v2 quality gates Checkpoint 2.15) — signal generation only.

Order of evaluation (each step can end in SKIP):
  1. Freshness gate: every required core symbol (TNA, TZA, IWM, SPY, QQQ) must
     have a quote no older than max_quote_age_seconds (default 1.0s) at the
     decision moment. Missing -> missing_required_quote, stale -> stale_market_data.
     No scoring happens when the gate fails.
  2. Quote validity (two-sided, not crossed) and TNA/TZA spread limit.
  3. Conservative scoring: IWM drives direction, SPY/QQQ confirm, TNA/TZA
     consistency, VIX only reduces conviction (never required).
  4. Thresholds: bullish >= 70 and bearish <= 40 -> TNA; mirror -> TZA; else SKIP.
  5. Quality gates (backend.signals.quality_gates): a TNA/TZA candidate must
     show short-window continuation and broad confirmation; pullbacks, fading,
     overextension/stalling and choppy confirmation turn it into SKIP.

This module never imports execution code and never places orders.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from typing import Callable, Dict, List, Mapping, Optional, Tuple

from backend.config.settings import ConfigurationError, Settings
from backend.signals.models import (
    REQUIRED_SIGNAL_SYMBOLS,
    SYMBOL_FOR_DIRECTION,
    VOLATILITY_SYMBOL,
    MarketSnapshot,
    QuoteFreshnessStatus,
    SignalDecision,
    SignalDirection,
    SignalQuality,
    SignalReason,
    SignalScoreBreakdown,
    SymbolQuote,
)
from backend.signals.quality_gates import QualityAssessment, assess_quality, gate_failure

IWM_LEVEL_POINTS = 20.0
IWM_MOMENTUM_POINTS = 20.0
CONFIRMATION_POINTS = 15.0
TNA_TZA_CONSISTENCY_POINTS = 10.0


@dataclass(frozen=True)
class SignalEngineConfig:
    required_symbols: Tuple[str, ...] = REQUIRED_SIGNAL_SYMBOLS
    max_quote_age_seconds: float = 1.0
    entry_score_threshold: float = 70.0
    opposing_score_max: float = 40.0
    min_score_gap: float = 20.0
    max_tradable_spread_pct: float = 0.30
    momentum_threshold_pct: float = 0.03
    level_tolerance_pct: float = 0.01
    vix_high_level: float = 25.0
    vix_extreme_level: float = 35.0
    vix_rising_pct: float = 5.0
    vix_high_penalty: float = 15.0
    vix_rising_penalty: float = 10.0
    partial_disagreement_penalty: float = 10.0
    tna_tza_inconsistency_penalty: float = 10.0

    def validate(self) -> "SignalEngineConfig":
        """Reject unsafe thresholds (configurable, but never looser than safe bounds)."""
        problems: List[str] = []
        missing = [s for s in REQUIRED_SIGNAL_SYMBOLS if s not in self.required_symbols]
        if missing:
            problems.append(f"required_symbols must include {missing}")
        if not 0 < self.max_quote_age_seconds <= 5.0:
            problems.append("max_quote_age_seconds must be in (0, 5]")
        if not 60.0 <= self.entry_score_threshold <= 100.0:
            problems.append("entry_score_threshold must be in [60, 100]")
        if not 0.0 <= self.opposing_score_max <= self.entry_score_threshold - 20.0:
            problems.append("opposing_score_max must be in [0, entry_score_threshold - 20]")
        if self.min_score_gap < 10.0:
            problems.append("min_score_gap must be >= 10")
        if not 0.0 < self.max_tradable_spread_pct <= 2.0:
            problems.append("max_tradable_spread_pct must be in (0, 2]")
        if self.momentum_threshold_pct <= 0:
            problems.append("momentum_threshold_pct must be > 0")
        if not 0 < self.vix_high_level < self.vix_extreme_level:
            problems.append("vix levels must satisfy 0 < vix_high_level < vix_extreme_level")
        if min(self.vix_high_penalty, self.vix_rising_penalty, self.partial_disagreement_penalty) < 0:
            problems.append("penalties must be >= 0")
        if problems:
            raise ConfigurationError("Unsafe signal engine config: " + "; ".join(problems))
        return self

    @classmethod
    def from_settings(
        cls,
        settings: Optional[Settings] = None,
        *,
        env: Optional[Mapping[str, str]] = None,
        **overrides: float,
    ) -> "SignalEngineConfig":
        source = env if env is not None else os.environ
        values: Dict[str, float] = {}
        if settings is not None:
            values["max_quote_age_seconds"] = float(settings.stream_max_quote_age_seconds)
        for key, attr in (
            ("SIGNAL_ENTRY_SCORE_THRESHOLD", "entry_score_threshold"),
            ("SIGNAL_OPPOSING_SCORE_MAX", "opposing_score_max"),
            ("SIGNAL_MIN_SCORE_GAP", "min_score_gap"),
            ("SIGNAL_MAX_SPREAD_PCT", "max_tradable_spread_pct"),
            ("SIGNAL_MOMENTUM_THRESHOLD_PCT", "momentum_threshold_pct"),
        ):
            raw = source.get(key)
            if raw is not None and str(raw).strip():
                try:
                    values[attr] = float(raw)
                except ValueError as exc:
                    raise ConfigurationError(f"Invalid {key}: not a number") from exc
        values.update({k: float(v) for k, v in overrides.items() if v is not None})
        return replace(cls(), **values).validate()

    def to_dict(self) -> Dict[str, object]:
        data = asdict(self)
        data["required_symbols"] = list(self.required_symbols)
        return data


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _pct_change(value: Optional[float], reference: Optional[float]) -> Optional[float]:
    if value is None or reference is None or reference <= 0:
        return None
    return (value - reference) / reference * 100.0


def _level_vote(price: Optional[float], reference: Optional[float], tolerance_pct: float) -> int:
    change = _pct_change(price, reference)
    if change is None:
        return 0
    if change > tolerance_pct:
        return 1
    if change < -tolerance_pct:
        return -1
    return 0


def trend_of(quote: Optional[SymbolQuote], tolerance_pct: float) -> int:
    """+1 above open and previous close, -1 below both, 0 mixed/unknown."""
    if quote is None:
        return 0
    votes = [
        _level_vote(quote.price, ref, tolerance_pct)
        for ref in (quote.day_open, quote.prev_close)
        if ref is not None
    ]
    if not votes:
        return 0
    if all(v == 1 for v in votes):
        return 1
    if all(v == -1 for v in votes):
        return -1
    return 0


_TREND_WORD = {1: "up", -1: "down", 0: "flat/mixed"}


class TnaTzaSignalEngine:
    """Produces SignalDecision objects. Has no order, broker or execution access."""

    IS_SIGNAL_ONLY = True

    def __init__(
        self,
        config: Optional[SignalEngineConfig] = None,
        *,
        wall_clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._config = (config or SignalEngineConfig()).validate()
        self._wall_clock = wall_clock

    @property
    def config(self) -> SignalEngineConfig:
        return self._config

    # -- public ------------------------------------------------------------

    def evaluate_freshness(self, snapshot: MarketSnapshot) -> Dict[str, QuoteFreshnessStatus]:
        cfg = self._config
        statuses: Dict[str, QuoteFreshnessStatus] = {}
        symbols: List[str] = list(cfg.required_symbols) + [
            s for s in snapshot.quotes if s not in cfg.required_symbols
        ]
        for symbol in symbols:
            quote = snapshot.get(symbol)
            required = symbol in cfg.required_symbols
            present = bool(quote and quote.has_quote and quote.quote_age_seconds is not None)
            age = quote.quote_age_seconds if quote else None
            fresh = present and age is not None and age <= cfg.max_quote_age_seconds
            note = ""
            if not required:
                note = "volatility diagnostic only; freshness not required" if symbol == VOLATILITY_SYMBOL else "not required"
            elif not present:
                note = "missing"
            elif not fresh:
                note = "stale"
            statuses[symbol] = QuoteFreshnessStatus(
                symbol=symbol,
                required=required,
                present=present,
                age_seconds=age,
                max_age_seconds=cfg.max_quote_age_seconds,
                fresh=fresh,
                note=note,
            )
        return statuses

    def decide(self, snapshot: MarketSnapshot) -> SignalDecision:
        cfg = self._config
        created_at = self._wall_clock()
        freshness = self.evaluate_freshness(snapshot)
        warnings: List[str] = []

        def skip(
            reason: SignalReason,
            explanation: str,
            *,
            gate_passed: bool,
            regime: str = "unknown",
            breakdown: Optional[SignalScoreBreakdown] = None,
            quality: Optional[SignalQuality] = None,
        ) -> SignalDecision:
            return SignalDecision(
                decision=SignalDirection.SKIP,
                selected_symbol=None,
                confidence_score=0.0,
                bullish_score=breakdown.bullish if breakdown else 0.0,
                bearish_score=breakdown.bearish if breakdown else 0.0,
                skip_reason=reason,
                quote_freshness_by_symbol=freshness,
                market_regime=regime,
                explanation=explanation,
                created_at=created_at,
                freshness_gate_passed=gate_passed,
                score_breakdown=breakdown,
                warnings=warnings,
                thresholds=self._thresholds(),
                quality=quality or SignalQuality.not_evaluated(),
            )

        # 1. Freshness gate — nothing else is computed if it fails.
        required = [freshness[s] for s in cfg.required_symbols]
        missing = [s.symbol for s in required if not s.present]
        stale = [s.symbol for s in required if s.present and not s.fresh]
        if missing:
            detail = f"missing required quote(s): {', '.join(missing)}"
            if stale:
                detail += f"; stale: {', '.join(stale)}"
            return skip(SignalReason.MISSING_REQUIRED_QUOTE, detail + ". No decision computed.", gate_passed=False)
        if stale:
            ages = ", ".join(f"{s}={freshness[s].age_seconds:.3f}s" for s in stale)
            return skip(
                SignalReason.STALE_MARKET_DATA,
                f"stale required quote(s) > {cfg.max_quote_age_seconds:g}s: {ages}. No decision computed.",
                gate_passed=False,
            )

        quotes = {s: snapshot.get(s) for s in cfg.required_symbols}

        # 2. Quote health.
        invalid = [
            s for s, q in quotes.items() if q is None or not q.has_bid_ask or q.ask < q.bid  # type: ignore[operator]
        ]
        if invalid:
            return skip(
                SignalReason.INVALID_QUOTE,
                f"one-sided or crossed quote(s): {', '.join(invalid)}",
                gate_passed=True,
            )
        wide = [
            f"{s} spread {quotes[s].spread_pct:.3f}%"  # type: ignore[union-attr]
            for s in ("TNA", "TZA")
            if (quotes[s].spread_pct or 0.0) > cfg.max_tradable_spread_pct  # type: ignore[union-attr]
        ]
        if wide:
            return skip(
                SignalReason.WIDE_SPREAD,
                f"tradable ETF spread too wide (> {cfg.max_tradable_spread_pct:g}%): {', '.join(wide)}",
                gate_passed=True,
            )
        iwm = quotes["IWM"]
        assert iwm is not None
        if iwm.day_open is None and iwm.prev_close is None:
            return skip(
                SignalReason.INSUFFICIENT_REFERENCE_DATA,
                "IWM day open and previous close unavailable (Summary not received)",
                gate_passed=True,
            )

        # 3. Scoring.
        breakdown = SignalScoreBreakdown()
        tol = cfg.level_tolerance_pct
        iwm_bull = iwm_bear = 0.0
        for name, reference, label in (
            ("iwm_vs_day_open", iwm.day_open, "day open"),
            ("iwm_vs_prev_close", iwm.prev_close, "previous close"),
        ):
            vote = _level_vote(iwm.price, reference, tol)
            change = _pct_change(iwm.price, reference)
            bull = IWM_LEVEL_POINTS if vote == 1 else 0.0
            bear = IWM_LEVEL_POINTS if vote == -1 else 0.0
            detail = (
                f"IWM {change:+.3f}% vs {label}" if change is not None else f"IWM {label} unavailable"
            )
            breakdown.add(name, bull, bear, detail)
            iwm_bull += bull
            iwm_bear += bear

        momentum = _pct_change(iwm.price, iwm.first_mid)
        if momentum is None:
            breakdown.add("iwm_momentum", 0.0, 0.0, "IWM window baseline unavailable")
        else:
            bull = IWM_MOMENTUM_POINTS if momentum >= cfg.momentum_threshold_pct else 0.0
            bear = IWM_MOMENTUM_POINTS if momentum <= -cfg.momentum_threshold_pct else 0.0
            breakdown.add(
                "iwm_momentum",
                bull,
                bear,
                f"IWM {momentum:+.3f}% over window (threshold ±{cfg.momentum_threshold_pct:g}%)",
            )
            iwm_bull += bull
            iwm_bear += bear

        trends = {s: trend_of(quotes[s], tol) for s in ("SPY", "QQQ", "TNA", "TZA")}
        for symbol in ("SPY", "QQQ"):
            trend = trends[symbol]
            breakdown.add(
                f"{symbol.lower()}_confirmation",
                CONFIRMATION_POINTS if trend == 1 else 0.0,
                CONFIRMATION_POINTS if trend == -1 else 0.0,
                f"{symbol} {_TREND_WORD[trend]} vs open/previous close",
            )

        tna_trend, tza_trend = trends["TNA"], trends["TZA"]
        if tna_trend == 1 and tza_trend == -1:
            breakdown.add("tna_tza_consistency", TNA_TZA_CONSISTENCY_POINTS, 0.0, "TNA up, TZA down")
        elif tna_trend == -1 and tza_trend == 1:
            breakdown.add("tna_tza_consistency", 0.0, TNA_TZA_CONSISTENCY_POINTS, "TNA down, TZA up")
        else:
            breakdown.add(
                "tna_tza_consistency",
                0.0,
                0.0,
                f"TNA {_TREND_WORD[tna_trend]}, TZA {_TREND_WORD[tza_trend]}",
            )
            if tna_trend != 0 and tna_trend == tza_trend:
                breakdown.penalize(
                    "tna_tza_inconsistent",
                    cfg.tna_tza_inconsistency_penalty,
                    "TNA and TZA moving the same direction (inverse ETFs disagree)",
                )
                warnings.append("TNA and TZA trends are inconsistent")

        # Broad-market agreement with the IWM direction.
        primary = 1 if iwm_bull > iwm_bear else -1 if iwm_bear > iwm_bull else 0
        disagreeing = [s for s in ("SPY", "QQQ") if primary != 0 and trends[s] == -primary]
        if primary != 0 and len(disagreeing) == 1:
            breakdown.penalize(
                "partial_broad_market_disagreement",
                cfg.partial_disagreement_penalty,
                f"{disagreeing[0]} disagrees with IWM",
            )

        # VIX: warning / conviction reduction only, never required.
        vix = snapshot.get(VOLATILITY_SYMBOL)
        vix_level = vix.price if vix else None
        vix_extreme = False
        vix_elevated = False
        if vix_level is None:
            warnings.append("VIX missing; volatility unknown (not required)")
        else:
            if vix_level >= cfg.vix_extreme_level:
                vix_extreme = True
            elif vix_level >= cfg.vix_high_level:
                vix_elevated = True
                breakdown.penalize("vix_high", cfg.vix_high_penalty, f"VIX {vix_level:g} >= {cfg.vix_high_level:g}")
            vix_change = _pct_change(vix_level, vix.prev_close if vix else None)
            if vix_change is not None and vix_change >= cfg.vix_rising_pct:
                vix_elevated = True
                breakdown.penalize(
                    "vix_rising",
                    cfg.vix_rising_penalty,
                    f"VIX {vix_change:+.2f}% vs previous close (>= {cfg.vix_rising_pct:g}%)",
                )
            if vix_elevated or vix_extreme:
                warnings.append(f"VIX elevated ({vix_level:g}); confidence reduced")

        regime = self._regime(trends, iwm_bull, iwm_bear, vix_extreme or vix_elevated)
        bull, bear = breakdown.bullish, breakdown.bearish
        lean = primary or (1 if bull >= bear else -1)
        lean_quality = self._quality(assess_quality(lean, quotes, trends, cfg.momentum_threshold_pct), "no_candidate")

        if vix_extreme:
            return skip(
                SignalReason.HIGH_VOLATILITY,
                f"VIX {vix_level:g} >= extreme level {cfg.vix_extreme_level:g}",
                gate_passed=True,
                regime=regime,
                breakdown=breakdown,
                quality=lean_quality,
            )
        if primary != 0 and len(disagreeing) == 2:
            return skip(
                SignalReason.BROAD_MARKET_DISAGREEMENT,
                f"SPY and QQQ both disagree with IWM direction ({_TREND_WORD[primary]})",
                gate_passed=True,
                regime=regime,
                breakdown=breakdown,
                quality=lean_quality,
            )

        # 4. Thresholds.
        if bull >= cfg.entry_score_threshold and bear <= cfg.opposing_score_max:
            direction = SignalDirection.BULLISH
        elif bear >= cfg.entry_score_threshold and bull <= cfg.opposing_score_max:
            direction = SignalDirection.BEARISH
        else:
            if abs(bull - bear) < cfg.min_score_gap:
                return skip(
                    SignalReason.UNCLEAR_MARKET_DIRECTION,
                    f"bullish {bull:g} and bearish {bear:g} too close (gap < {cfg.min_score_gap:g})",
                    gate_passed=True,
                    regime=regime,
                    breakdown=breakdown,
                    quality=lean_quality,
                )
            return skip(
                SignalReason.INSUFFICIENT_SIGNAL_STRENGTH,
                f"bullish {bull:g} / bearish {bear:g} below entry threshold "
                f"{cfg.entry_score_threshold:g} (opposing max {cfg.opposing_score_max:g})",
                gate_passed=True,
                regime=regime,
                breakdown=breakdown,
                quality=lean_quality,
            )

        # 5. Quality gates: can only turn the candidate into SKIP.
        symbol = SYMBOL_FOR_DIRECTION[direction]
        winning = bull if direction is SignalDirection.BULLISH else bear
        assessment = assess_quality(
            1 if direction is SignalDirection.BULLISH else -1, quotes, trends, cfg.momentum_threshold_pct
        )
        failure = gate_failure(
            assessment,
            momentum_threshold_pct=cfg.momentum_threshold_pct,
            winning_score=winning,
            entry_threshold=cfg.entry_score_threshold,
        )
        if failure is not None:
            gate_reason, detail = failure
            return skip(
                gate_reason,
                f"quality gate {gate_reason.value}: {symbol} candidate ({winning:g}) skipped — {detail}",
                gate_passed=True,
                regime=regime,
                breakdown=breakdown,
                quality=self._quality(assessment, gate_reason.value),
            )
        if assessment.confidence_penalty > 0:
            warnings.append(f"pullback risk {assessment.pullback_risk_score:g}; confidence reduced by {assessment.confidence_penalty:g}")

        reason = (
            SignalReason.BULLISH_CONFIRMED
            if direction is SignalDirection.BULLISH
            else SignalReason.BEARISH_CONFIRMED
        )
        drivers = "; ".join(c.detail for c in breakdown.components if (c.bullish if direction is SignalDirection.BULLISH else c.bearish) > 0)
        return SignalDecision(
            decision=direction,
            selected_symbol=symbol,
            confidence_score=winning - assessment.confidence_penalty,
            bullish_score=bull,
            bearish_score=bear,
            skip_reason=None,
            quote_freshness_by_symbol=freshness,
            market_regime=regime,
            explanation=(
                f"{reason.value} -> {symbol}: {drivers}; quality gates passed "
                f"(continuation {assessment.continuation_score:g}, confirmation {assessment.confirmation_score:g})"
            ),
            created_at=created_at,
            freshness_gate_passed=True,
            score_breakdown=breakdown,
            warnings=warnings,
            thresholds=self._thresholds(),
            quality=self._quality(assessment, None),
        )

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _quality(assessment: QualityAssessment, reason: Optional[str]) -> SignalQuality:
        return SignalQuality(
            evaluated=True,
            direction=(SignalDirection.BULLISH if assessment.direction == 1 else SignalDirection.BEARISH).value,
            continuation_score=assessment.continuation_score,
            confirmation_score=assessment.confirmation_score,
            chop_risk_score=assessment.chop_risk_score,
            pullback_risk_score=assessment.pullback_risk_score,
            quality_gate_passed=reason is None,
            quality_gate_reason=reason,
            confidence_penalty=assessment.confidence_penalty if reason is None else 0.0,
            metrics=dict(assessment.metrics),
        )

    def _thresholds(self) -> Dict[str, float]:
        cfg = self._config
        return {
            "max_quote_age_seconds": cfg.max_quote_age_seconds,
            "entry_score_threshold": cfg.entry_score_threshold,
            "opposing_score_max": cfg.opposing_score_max,
            "min_score_gap": cfg.min_score_gap,
            "max_tradable_spread_pct": cfg.max_tradable_spread_pct,
        }

    @staticmethod
    def _regime(trends: Mapping[str, int], iwm_bull: float, iwm_bear: float, volatile: bool) -> str:
        if volatile:
            return "high_volatility"
        iwm = 1 if iwm_bull > iwm_bear else -1 if iwm_bear > iwm_bull else 0
        if iwm == 1 and trends["SPY"] == 1 and trends["QQQ"] == 1:
            return "risk_on"
        if iwm == -1 and trends["SPY"] == -1 and trends["QQQ"] == -1:
            return "risk_off"
        return "mixed"
