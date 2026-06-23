"""Risk and order domain models for the Phase 2 execution stack."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

ALLOWED_SYMBOLS = frozenset({"TNA", "TZA"})
ALLOWED_SIDES_PHASE_2 = frozenset({"buy"})
ALLOWED_TRADING_MODES = frozenset({"paper", "sandbox"})


class OrderIntentValidationError(ValueError):
    """Raised when OrderIntent is constructed with invalid fields."""


@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    side: str
    quantity: int
    trading_mode: str
    order_type: str = "Market"
    source: str = "manual"
    reason: str = ""
    current_price: Optional[float] = None
    ai_decision_id: Optional[str] = None
    strategy_signal_id: Optional[str] = None

    def __post_init__(self) -> None:
        symbol = self.symbol.strip().upper()
        side = self.side.strip().lower()
        mode = self.trading_mode.strip().lower()

        if symbol not in ALLOWED_SYMBOLS:
            raise OrderIntentValidationError(
                f"symbol must be one of {sorted(ALLOWED_SYMBOLS)}, got {self.symbol!r}"
            )
        if side not in ALLOWED_SIDES_PHASE_2:
            raise OrderIntentValidationError(
                f"side must be 'buy' in Phase 2, got {self.side!r}"
            )
        if self.quantity <= 0:
            raise OrderIntentValidationError(
                f"quantity must be positive, got {self.quantity}"
            )
        if mode not in ALLOWED_TRADING_MODES:
            raise OrderIntentValidationError(
                f"trading_mode must be paper or sandbox in Phase 2, got {self.trading_mode!r}"
            )
        if self.current_price is not None and self.current_price <= 0:
            raise OrderIntentValidationError(
                f"current_price must be positive when set, got {self.current_price}"
            )


@dataclass
class RiskContext:
    trading_mode: str
    live_trading_enabled: bool
    tastytrade_env: str
    emergency_halt: bool
    buying_power: Optional[float]
    current_price: Optional[float]
    open_positions_count: int
    pending_orders_count: int
    trades_today_count: int
    daily_pnl: float
    market_data_healthy: bool
    max_trades_per_day: int
    max_daily_loss_usd: float
    buying_power_reserve_pct: float
    max_position_pct_of_buying_power: float
    single_position_mode: bool = True
    cash_available: Optional[float] = None
    ai_decision_valid: bool = True
    strategy_signal_valid: bool = True


@dataclass
class ApprovalResult:
    approved: bool
    reason: str
    rejection_code: Optional[str] = None
    normalized_intent: Optional[OrderIntent] = None
    checks: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class ExecutionResult:
    success: bool
    status: str  # approved | rejected | filled | error
    symbol: str
    side: str
    quantity: int
    trading_mode: str
    message: str
    order_id: Optional[str] = None
    fill_price: Optional[float] = None
    raw: Optional[Dict[str, Any]] = None
