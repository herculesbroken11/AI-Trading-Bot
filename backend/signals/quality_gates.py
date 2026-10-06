"""
Signal quality gates (Checkpoint 2.15) — deterministic anti-chop / continuation filters.

Applied after the v1 scoring has produced a TNA/TZA candidate. A candidate can
only be turned into SKIP here, never created or flipped, so the gates can only
make the engine more conservative. All limits are multiples of the existing
momentum_threshold_pct (default 0.03%) and are not configurable from the
environment. Signal generation only — no order, broker or execution access.

Window metrics come from the stream: first_mid (window start), the current mid
and, when available, the window high/low mid. Snapshots stored before 2.15 have
no window high/low; the fading check is then skipped (other gates still apply).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Mapping, Optional, Tuple

from backend.signals.models import SignalReason, SymbolQuote

CLEAR_MOMENTUM_X = 1.0  # IWM window move >= 1x threshold: clear continuation
STRONG_MOMENTUM_X = 2.0  # >= 2x: no longer "mild"; full continuation credit
AGAINST_MOMENTUM_X = 1.0 / 3.0  # IWM moving against the candidate by >= 1/3x: pullback
PAIR_STRONG_X = 3.0  # TNA up and TZA down (or mirror) each >= 3x in-window: strong pair
CONFIRM_MOVE_X = 0.5  # SPY / QQQ in-window move >= 0.5x in the candidate direction confirms
FADE_RETRACE = 0.6  # gave back >= 60% of the window's best move: fading
OVEREXTENDED_PCT = 1.5  # IWM >= 1.5% beyond day open (or previous close)
STALL_EXTENSION_PCT = 0.5  # extended >= 0.5% ...
STALL_RANGE_X = 0.5  # ... while the whole window ranged < 0.5x: stalling
PULLBACK_PENALTY_WEIGHT = 0.25  # confidence penalty = 0.25 * pullback_risk_score (max 25)


def _pct(value: Optional[float], reference: Optional[float]) -> Optional[float]:
    if value is None or reference is None or reference <= 0:
        return None
    return (value - reference) / reference * 100.0


def _clamp01(value: Optional[float]) -> float:
    if value is None:
        return 0.0
    return max(0.0, min(1.0, value))


def _signed(value: Optional[float], sign: int) -> Optional[float]:
    return None if value is None else value * sign


def _fmt(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:+.3f}%"


@dataclass
class QualityAssessment:
    direction: int  # +1 bullish / TNA, -1 bearish / TZA
    continuation_score: float
    confirmation_score: float
    chop_risk_score: float
    pullback_risk_score: float
    confidence_penalty: float
    metrics: Dict[str, Optional[float]] = field(default_factory=dict)
    window_confirmations: int = 0
    levels_confirm: bool = False
    pair_strong: bool = False


def assess_quality(
    direction: int,
    quotes: Mapping[str, Optional[SymbolQuote]],
    trends: Mapping[str, int],
    momentum_threshold_pct: float,
) -> QualityAssessment:
    """Scores (0-100) for the given direction. Moves are signed so positive = in the direction."""
    s = 1 if direction >= 0 else -1
    thr = momentum_threshold_pct
    iwm, tna, tza = quotes["IWM"], quotes["TNA"], quotes["TZA"]

    def window_move(quote: Optional[SymbolQuote]) -> Optional[float]:
        return _pct(quote.mid, quote.first_mid) if quote else None

    iwm_move = _signed(window_move(iwm), s)
    tna_move, tza_move = window_move(tna), window_move(tza)
    favoured, inverse = (tna_move, tza_move) if s == 1 else (tza_move, tna_move)
    pair_move = None if favoured is None or inverse is None else (favoured - inverse) / 2.0
    pair_strong = (
        favoured is not None and inverse is not None and favoured >= PAIR_STRONG_X * thr and inverse <= -PAIR_STRONG_X * thr
    )
    confirm_moves = {sym: _signed(window_move(quotes[sym]), s) for sym in ("SPY", "QQQ")}

    reference = iwm.day_open if iwm and iwm.day_open is not None else (iwm.prev_close if iwm else None)
    extension = _signed(_pct(iwm.mid if iwm else None, reference), s)

    peak = None
    if iwm is not None:
        peak = iwm.window_high_mid if s == 1 else iwm.window_low_mid
    peak_move = _signed(_pct(peak, iwm.first_mid if iwm else None), s)
    retrace = None
    if peak_move is not None and iwm_move is not None and peak_move >= CLEAR_MOMENTUM_X * thr:
        retrace = max(0.0, (peak_move - iwm_move) / peak_move)
    window_range = None
    if iwm is not None and iwm.window_high_mid is not None and iwm.window_low_mid is not None:
        window_range = _pct(iwm.window_high_mid, iwm.window_low_mid)

    strong = STRONG_MOMENTUM_X * thr
    continuation = 60.0 * _clamp01((iwm_move or 0.0) / strong) + 40.0 * _clamp01((pair_move or 0.0) / (PAIR_STRONG_X * thr))

    confirm_band = CONFIRM_MOVE_X * thr
    confirmation = 0.0
    chop = 30.0 * (1.0 - _clamp01((iwm_move or 0.0) / strong))
    window_confirmations = 0
    for sym in ("SPY", "QQQ"):
        move = confirm_moves[sym]
        if trends.get(sym) == s:
            confirmation += 25.0
        else:
            chop += 10.0
        confirmation += 25.0 * _clamp01((move or 0.0) / (2.0 * confirm_band))
        if move is not None and move >= confirm_band:
            window_confirmations += 1
        elif move is not None and move <= -confirm_band:
            chop += 25.0
        else:
            chop += 12.5

    pullback = 0.0
    if iwm_move is not None and iwm_move < 0:
        pullback = max(pullback, 60.0 * _clamp01(-iwm_move / strong))
    if pair_move is not None and pair_move < 0:
        pullback = max(pullback, 60.0 * _clamp01(-pair_move / (PAIR_STRONG_X * thr)))
    pullback += 40.0 * _clamp01(retrace)
    pullback = min(100.0, pullback)

    return QualityAssessment(
        direction=s,
        continuation_score=round(continuation, 1),
        confirmation_score=round(confirmation, 1),
        chop_risk_score=round(min(100.0, chop), 1),
        pullback_risk_score=round(pullback, 1),
        confidence_penalty=round(PULLBACK_PENALTY_WEIGHT * pullback, 1),
        metrics={
            "iwm_window_move_pct": iwm_move,
            "tna_window_move_pct": tna_move,
            "tza_window_move_pct": tza_move,
            "pair_window_move_pct": pair_move,
            "spy_window_move_pct": confirm_moves["SPY"],
            "qqq_window_move_pct": confirm_moves["QQQ"],
            "iwm_extension_pct": extension,
            "iwm_window_peak_move_pct": peak_move,
            "iwm_window_retrace_ratio": retrace,
            "iwm_window_range_pct": window_range,
        },
        window_confirmations=window_confirmations,
        levels_confirm=all(trends.get(sym) == s for sym in ("SPY", "QQQ")),
        pair_strong=pair_strong,
    )


def gate_failure(
    assessment: QualityAssessment,
    *,
    momentum_threshold_pct: float,
    winning_score: float,
    entry_threshold: float,
) -> Optional[Tuple[SignalReason, str]]:
    """First failing gate for a TNA/TZA candidate, or None if it passes. Checked most specific first."""
    thr = momentum_threshold_pct
    m = assessment.metrics
    iwm_move = m["iwm_window_move_pct"]
    extension = m["iwm_extension_pct"]
    retrace = m["iwm_window_retrace_ratio"]
    window_range = m["iwm_window_range_pct"]
    word = "bullish" if assessment.direction == 1 else "bearish"

    if iwm_move is not None and iwm_move <= -AGAINST_MOMENTUM_X * thr:
        return (
            SignalReason.PULLBACK_RISK,
            f"IWM moved {_fmt(iwm_move)} against the {word} candidate during the window (pullback/reversal)",
        )
    if retrace is not None and retrace >= FADE_RETRACE:
        return (
            SignalReason.PULLBACK_RISK,
            f"IWM gave back {retrace:.0%} of its {_fmt(m['iwm_window_peak_move_pct'])} window move (fading)",
        )
    if extension is not None and extension >= OVEREXTENDED_PCT and (iwm_move is None or iwm_move < STRONG_MOMENTUM_X * thr):
        return (
            SignalReason.OVEREXTENDED_OR_STALLING,
            f"IWM {_fmt(extension)} beyond its reference (>= {OVEREXTENDED_PCT:g}%) without fresh acceleration "
            f"(window {_fmt(iwm_move)} < {STRONG_MOMENTUM_X * thr:g}%)",
        )
    if (
        extension is not None
        and extension >= STALL_EXTENSION_PCT
        and window_range is not None
        and window_range < STALL_RANGE_X * thr
    ):
        return (
            SignalReason.OVEREXTENDED_OR_STALLING,
            f"IWM extended {_fmt(extension)} but the window only ranged {window_range:.3f}% (stalling)",
        )
    if (iwm_move is None or iwm_move < CLEAR_MOMENTUM_X * thr) and not assessment.pair_strong:
        return (
            SignalReason.WEAK_CONTINUATION,
            f"IWM window move {_fmt(iwm_move)} below clear continuation ({CLEAR_MOMENTUM_X * thr:g}%) and "
            "TNA/TZA not strongly confirming in-window; level-only score is not trusted",
        )
    if (iwm_move is None or iwm_move < STRONG_MOMENTUM_X * thr) and (
        not assessment.levels_confirm or assessment.window_confirmations < 2
    ):
        return (
            SignalReason.CHOPPY_CONFIRMATION,
            f"IWM only mildly {word} ({_fmt(iwm_move)}) while SPY/QQQ are mixed or flat "
            f"(SPY {_fmt(m['spy_window_move_pct'])}, QQQ {_fmt(m['qqq_window_move_pct'])} in-window)",
        )
    if winning_score - assessment.confidence_penalty < entry_threshold:
        return (
            SignalReason.PULLBACK_RISK,
            f"confidence {winning_score:g} - pullback penalty {assessment.confidence_penalty:g} "
            f"< entry threshold {entry_threshold:g}",
        )
    return None
