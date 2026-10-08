"""
Signal profiles.

conservative_v2 is the current quality-gated engine and remains the default
everywhere, including shadow mode when no profile is requested.

balanced_v3_shadow is observation/replay only. build_shadow_engine() is the
only supported constructor path for that profile. It is not used by order
routing, pre-submit checks, or public trading routes.
"""

from __future__ import annotations

from typing import Callable, Optional, Union

from backend.config.settings import ConfigurationError, Settings
from backend.signals.tna_tza_signal_engine import SignalEngineConfig, TnaTzaSignalEngine

CONSERVATIVE_V2 = "conservative_v2"
BALANCED_V3_SHADOW = "balanced_v3_shadow"
DEFAULT_SIGNAL_PROFILE = CONSERVATIVE_V2
ALLOWED_SIGNAL_PROFILES = frozenset({CONSERVATIVE_V2, BALANCED_V3_SHADOW})

PROFILE_SCORE_FIELDS = (
    "profile",
    "candidate_score",
    "direction",
    "iwm_momentum_score",
    "pair_confirmation_score",
    "relative_strength_score",
    "broad_window_score",
    "vix_score",
    "chop_risk_score",
    "pullback_risk_score",
    "final_reason",
)


def normalize_signal_profile(value: Optional[str]) -> str:
    raw = (value or DEFAULT_SIGNAL_PROFILE).strip().lower()
    if raw not in ALLOWED_SIGNAL_PROFILES:
        allowed = ", ".join(sorted(ALLOWED_SIGNAL_PROFILES))
        raise ConfigurationError(f"Invalid SIGNAL_PROFILE={value!r}. Allowed: {allowed}")
    return raw


def build_shadow_engine(
    profile: str,
    settings: Optional[Settings] = None,
    *,
    max_quote_age_seconds: Optional[float] = None,
    wall_clock: Optional[Callable] = None,
    mode: str = "shadow",
) -> Union[TnaTzaSignalEngine, "CandidateEngineV3"]:
    """
    Shadow-logger / replay factory.

    conservative_v2 returns the unchanged v2 engine.
    balanced_v3_shadow returns Candidate Engine v3 and refuses any mode other
    than shadow or replay.
    """
    selected = normalize_signal_profile(profile)
    if selected == CONSERVATIVE_V2:
        config = SignalEngineConfig.from_settings(settings, max_quote_age_seconds=max_quote_age_seconds)
        if wall_clock is None:
            return TnaTzaSignalEngine(config)
        return TnaTzaSignalEngine(config, wall_clock=wall_clock)

    from backend.signals.candidate_engine_v3 import CandidateEngineV3, CandidateEngineV3Config

    config_v3 = CandidateEngineV3Config.from_settings(settings, max_quote_age_seconds=max_quote_age_seconds)
    kwargs = {"mode": mode}
    if wall_clock is not None:
        kwargs["wall_clock"] = wall_clock
    return CandidateEngineV3(config_v3, **kwargs)
