"""
Signal Engine (v1 Checkpoint 2.11, v2 quality gates Checkpoint 2.15): TNA / TZA /
SKIP decisions from read-only DXLink data with a strict freshness gate. Signal
generation only — this package never imports execution code or places orders.
"""

from backend.signals.candidate_engine_v3 import CandidateEngineV3, CandidateEngineV3Config
from backend.signals.models import (
    QUALITY_FIELDS,
    QUALITY_GATE_REASONS,
    REQUIRED_SIGNAL_SYMBOLS,
    SIGNAL_ENGINE_VERSION,
    MarketSnapshot,
    QuoteFreshnessStatus,
    SignalDecision,
    SignalDirection,
    SignalQuality,
    SignalReason,
    SignalScoreBreakdown,
    SymbolQuote,
)
from backend.signals.profiles import (
    ALLOWED_SIGNAL_PROFILES,
    BALANCED_V3_SHADOW,
    CONSERVATIVE_V2,
    DEFAULT_SIGNAL_PROFILE,
    PROFILE_SCORE_FIELDS,
    build_shadow_engine,
    normalize_signal_profile,
)
from backend.signals.tna_tza_signal_engine import SignalEngineConfig, TnaTzaSignalEngine

__all__ = [
    "ALLOWED_SIGNAL_PROFILES",
    "BALANCED_V3_SHADOW",
    "CONSERVATIVE_V2",
    "DEFAULT_SIGNAL_PROFILE",
    "PROFILE_SCORE_FIELDS",
    "CandidateEngineV3",
    "CandidateEngineV3Config",
    "QUALITY_FIELDS",
    "QUALITY_GATE_REASONS",
    "REQUIRED_SIGNAL_SYMBOLS",
    "SIGNAL_ENGINE_VERSION",
    "MarketSnapshot",
    "QuoteFreshnessStatus",
    "SignalDecision",
    "SignalDirection",
    "SignalEngineConfig",
    "SignalQuality",
    "SignalReason",
    "SignalScoreBreakdown",
    "SymbolQuote",
    "TnaTzaSignalEngine",
    "build_shadow_engine",
    "normalize_signal_profile",
]
