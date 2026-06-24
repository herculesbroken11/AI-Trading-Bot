"""Routes approved orders to paper simulator or Tastytrade sandbox adapter."""

from __future__ import annotations

from typing import Optional

from backend.adapters.broker.tastytrade_sandbox import TastytradeSandboxAdapter
from backend.config.settings import Settings
from backend.config.tastytrade_urls import ALLOWED_SANDBOX_SYMBOLS, SANDBOX_MAX_ORDER_QUANTITY
from backend.execution.paper_simulator import PaperSimulator
from backend.risk.live_guard import (
    BrokerEnvironmentBlockedError,
    EmergencyHaltActiveError,
    LiveTradingBlockedError,
    assert_order_execution_allowed,
)
from backend.risk.models import ExecutionResult, OrderIntent, RiskContext


class ExecutionRouter:
    """Route approved intents to paper simulator or sandbox adapter."""

    def __init__(
        self,
        paper_simulator: PaperSimulator | None = None,
        sandbox_adapter: Optional[TastytradeSandboxAdapter] = None,
    ) -> None:
        self._paper = paper_simulator or PaperSimulator()
        self._sandbox = sandbox_adapter

    @property
    def paper_simulator(self) -> PaperSimulator:
        return self._paper

    @property
    def sandbox_adapter(self) -> Optional[TastytradeSandboxAdapter]:
        return self._sandbox

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
            return self._route_sandbox(intent, context)

        return self._blocked_result(
            intent,
            f"Unknown trading mode {mode!r}",
            "error",
        )

    def _route_sandbox(self, intent: OrderIntent, context: RiskContext) -> ExecutionResult:
        symbol = intent.symbol.strip().upper()
        side = intent.side.strip().lower()

        if context.emergency_halt:
            return self._blocked_result(intent, "EMERGENCY_HALT is enabled", "error")

        if symbol not in ALLOWED_SANDBOX_SYMBOLS:
            return self._blocked_result(
                intent,
                f"Sandbox route allows only {sorted(ALLOWED_SANDBOX_SYMBOLS)}",
                "rejected",
            )

        if side != "buy":
            return self._blocked_result(
                intent,
                "Sandbox route supports buy-only long equity orders in Phase 2",
                "rejected",
            )

        if intent.quantity <= 0 or intent.quantity > SANDBOX_MAX_ORDER_QUANTITY:
            return self._blocked_result(
                intent,
                f"Sandbox quantity must be 1..{SANDBOX_MAX_ORDER_QUANTITY}",
                "rejected",
            )

        if not self._sandbox:
            return ExecutionResult(
                success=False,
                status="error",
                symbol=symbol,
                side=side,
                quantity=intent.quantity,
                trading_mode="sandbox",
                message="Sandbox adapter is not configured for this router instance",
                raw={"route": "sandbox", "placed": False},
            )

        return self._sandbox.execute_order(intent)

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
