"""
Phase 2 live-trading guard.

Blocks production routing and live trading modes before any order execution path.
Not wired into trade_exec.py in Checkpoint 2.0 — tests and future OrderExecutor only.
"""

from __future__ import annotations

from backend.config.settings import Settings


class LiveTradingBlockedError(RuntimeError):
    """Raised when live trading or live mode is requested in Phase 2."""


class BrokerEnvironmentBlockedError(RuntimeError):
    """Raised when Tastytrade environment is not sandbox for order paths."""


class EmergencyHaltActiveError(RuntimeError):
    """Raised when EMERGENCY_HALT is enabled."""


def assert_order_execution_allowed(settings: Settings) -> None:
    """
    Enforce Phase 2 order-path safety rules.

    Call this before any broker HTTP or simulated fill that represents an order.
    """
    if settings.emergency_halt:
        raise EmergencyHaltActiveError(
            "EMERGENCY_HALT is enabled. All order execution is blocked."
        )

    if settings.trading_mode == "live":
        raise LiveTradingBlockedError(
            "TRADING_MODE=live is blocked in Phase 2. "
            "No live orders may be placed."
        )

    if settings.live_trading_enabled:
        raise LiveTradingBlockedError(
            "LIVE_TRADING_ENABLED is true but live trading is blocked in Phase 2."
        )

    env = settings.tastytrade_env.strip().lower()
    if env != "sandbox":
        raise BrokerEnvironmentBlockedError(
            f"TASTYTRADE_ENV={settings.tastytrade_env!r} is not permitted for order "
            "execution in Phase 2. Only sandbox (cert API) is allowed when routing "
            "to Tastytrade; use TRADING_MODE=paper for local simulation."
        )


def is_paper_mode(settings: Settings) -> bool:
    """True when orders must stay in the local paper simulator (no broker HTTP)."""
    return settings.trading_mode.strip().lower() == "paper"


def is_sandbox_broker_mode(settings: Settings) -> bool:
    """True when sandbox cert API may be used (still guarded by assert_order_execution_allowed)."""
    return settings.trading_mode.strip().lower() == "sandbox"
