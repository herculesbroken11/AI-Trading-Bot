"""Local paper trading simulator — no broker HTTP (Phase 2)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Dict, List

from backend.risk.models import ExecutionResult, OrderIntent, RiskContext


class PaperSimulatorError(ValueError):
    """Invalid paper simulation request."""


@dataclass
class PaperFill:
    order_id: str
    symbol: str
    side: str
    quantity: int
    fill_price: float


@dataclass
class PaperSimulator:
    """In-memory paper ledger for offline-safe fills."""

    fills: List[PaperFill] = field(default_factory=list)
    positions: Dict[str, int] = field(default_factory=dict)

    def execute(self, intent: OrderIntent, context: RiskContext) -> ExecutionResult:
        side = intent.side.strip().lower()
        symbol = intent.symbol.strip().upper()
        mode = intent.trading_mode.strip().lower()

        if side != "buy":
            return ExecutionResult(
                success=False,
                status="rejected",
                symbol=symbol,
                side=side,
                quantity=intent.quantity,
                trading_mode=mode,
                message="Paper simulator supports buy orders only in Phase 2",
            )

        fill_price = intent.current_price if intent.current_price is not None else context.current_price
        if fill_price is None or fill_price <= 0:
            return ExecutionResult(
                success=False,
                status="error",
                symbol=symbol,
                side=side,
                quantity=intent.quantity,
                trading_mode=mode,
                message="Fill price must be positive",
            )

        order_id = f"PAPER-{uuid.uuid4()}"
        self.fills.append(
            PaperFill(
                order_id=order_id,
                symbol=symbol,
                side=side,
                quantity=intent.quantity,
                fill_price=fill_price,
            )
        )
        self.positions[symbol] = self.positions.get(symbol, 0) + intent.quantity

        return ExecutionResult(
            success=True,
            status="filled",
            order_id=order_id,
            symbol=symbol,
            side=side,
            quantity=intent.quantity,
            fill_price=fill_price,
            trading_mode=mode,
            message="Paper order filled locally",
            raw={"simulator": "paper", "ledger_fills": len(self.fills)},
        )
