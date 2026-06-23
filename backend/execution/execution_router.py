"""Routes approved orders to paper simulator or blocked sandbox/live paths."""

from __future__ import annotations

from backend.config.settings import Settings
from backend.execution.paper_simulator import PaperSimulator
from backend.risk.live_guard import (
    BrokerEnvironmentBlockedError,
    EmergencyHaltActiveError,
    LiveTradingBlockedError,
    assert_order_execution_allowed,
)
from backend.risk.models import ExecutionResult, OrderIntent, RiskContext


class ExecutionRouter:
    """Route approved intents to the correct execution backend."""

    def __init__(self, paper_simulator: PaperSimulator | None = None) -> None:
        self._paper = paper_simulator or PaperSimulator()

    @property
    def paper_simulator(self) -> PaperSimulator:
        return self._paper

    def route(self, intent: OrderIntent, context: RiskContext) -> ExecutionResult:
        mode = intent.trading_mode.strip().lower()
        symbol = intent.symbol.strip().upper()
        side = intent.side.strip().lower()

        try:
            assert_order_execution_allowed(self._settings_from_context(context))
        except EmergencyHaltActiveError as exc:
            return self._blocked_result(intent, str(exc), "error")
        except LiveTradingBlockedError as exc:
            return self._blocked_result(intent, str(exc), "rejected")
        except BrokerEnvironmentBlockedError as exc:
            return self._blocked_result(intent, str(exc), "rejected")

        if mode == "live":
            return self._blocked_result(
                intent,
                "TRADING_MODE=live is blocked in Phase 2",
                "rejected",
            )

        if mode == "paper":
            return self._paper.execute(intent, context)

        if mode == "sandbox":
            return ExecutionResult(
                success=False,
                status="error",
                symbol=symbol,
                side=side,
                quantity=intent.quantity,
                trading_mode=mode,
                message=(
                    "Sandbox broker routing is not wired in Phase 2 Checkpoint 2.1. "
                    "No sandbox order was placed."
                ),
                raw={"route": "sandbox", "placed": False},
            )

        return self._blocked_result(
            intent,
            f"Unknown trading mode {mode!r}",
            "error",
        )

    @staticmethod
    def _settings_from_context(context: RiskContext) -> Settings:
        return Settings(
            live_trading_enabled=context.live_trading_enabled,
            trading_mode=context.trading_mode.strip().lower(),
            tastytrade_env=context.tastytrade_env.strip().lower(),
            emergency_halt=context.emergency_halt,
        )

    @staticmethod
    def _blocked_result(intent: OrderIntent, message: str, status: str) -> ExecutionResult:
        return ExecutionResult(
            success=False,
            status=status,
            symbol=intent.symbol.strip().upper(),
            side=intent.side.strip().lower(),
            quantity=intent.quantity,
            trading_mode=intent.trading_mode.strip().lower(),
            message=message,
        )
