"""Phase 2 blocks on legacy trading mutations until new stack is wired."""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from flask import jsonify

from backend.config.settings import Settings

# Legacy broker/bot mutations stay blocked until a future checkpoint explicitly enables them.
PHASE2_LEGACY_TRADING_BLOCKED = True

BLOCKED_MESSAGE = (
    "Trading endpoints are blocked during Phase 2 safety refactor. "
    "Use paper execution tests only."
)


def trading_mutation_block_response(
    *,
    status_code: int = 423,
    message: str = BLOCKED_MESSAGE,
) -> Tuple[Any, int]:
    return (
        jsonify(
            {
                "success": False,
                "status": "blocked",
                "message": message,
            }
        ),
        status_code,
    )


def legacy_trading_block_response(settings: Settings) -> Optional[Tuple[Any, int]]:
    """Return a Flask response tuple when legacy trading must not run."""
    if PHASE2_LEGACY_TRADING_BLOCKED:
        return trading_mutation_block_response()

    if settings.emergency_halt:
        return trading_mutation_block_response(
            message="EMERGENCY_HALT is active. All trading mutations are blocked.",
        )

    return None


def assert_legacy_trading_blocked(settings: Settings) -> None:
    """Raise if legacy trading is attempted while Phase 2 gate is active."""
    if PHASE2_LEGACY_TRADING_BLOCKED or settings.emergency_halt:
        raise RuntimeError(BLOCKED_MESSAGE)


def is_trading_mutation_blocked(settings: Settings) -> bool:
    return PHASE2_LEGACY_TRADING_BLOCKED or settings.emergency_halt
