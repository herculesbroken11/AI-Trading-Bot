"""
Shadow mode (Checkpoint 2.12): bounded, manual observation of Signal Engine
decisions with follow-up market movement. Logging only — never places,
routes or submits orders.
"""

from backend.shadow_mode.logger import ShadowSignalLogger
from backend.shadow_mode.models import (
    FollowupOutcome,
    ShadowCycleError,
    ShadowCycleRecord,
    ShadowRunSummary,
    ShadowSafetyError,
    compute_followup_outcome,
)
from backend.shadow_mode.report import summarize_shadow_logs
from backend.shadow_mode.runner import (
    MAX_CYCLES,
    ShadowModeRunner,
    ShadowRunConfig,
    validate_shadow_environment,
)

__all__ = [
    "MAX_CYCLES",
    "FollowupOutcome",
    "ShadowCycleError",
    "ShadowCycleRecord",
    "ShadowModeRunner",
    "ShadowRunConfig",
    "ShadowRunSummary",
    "ShadowSafetyError",
    "ShadowSignalLogger",
    "compute_followup_outcome",
    "summarize_shadow_logs",
    "validate_shadow_environment",
]
