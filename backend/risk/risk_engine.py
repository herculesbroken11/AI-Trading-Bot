"""Centralized pre-trade risk approval (Phase 2)."""

from __future__ import annotations

from typing import Callable, List, Optional, Tuple

from backend.config.settings import ALLOWED_TRADING_MODES_PHASE_2
from backend.risk.models import (
    ALLOWED_SIDES_PHASE_2,
    ALLOWED_SYMBOLS,
    ApprovalResult,
    OrderIntent,
    RiskContext,
)

CheckFn = Callable[[OrderIntent, RiskContext], Tuple[bool, str, Optional[str]]]


class RiskEngineConfigurationError(RuntimeError):
    """Invalid risk-engine configuration supplied by caller."""


class RiskEngine:
    """Approve or reject order intents before any execution routing."""

    def approve(self, intent: OrderIntent, context: RiskContext) -> ApprovalResult:
        self._validate_context_config(context)

        checks: List[dict] = []
        for name, check_fn in self._checks():
            passed, message, rejection_code = check_fn(intent, context)
            checks.append({"name": name, "passed": passed, "message": message})
            if not passed:
                return ApprovalResult(
                    approved=False,
                    reason=message,
                    rejection_code=rejection_code,
                    checks=checks,
                )

        normalized = OrderIntent(
            symbol=intent.symbol.strip().upper(),
            side=intent.side.strip().lower(),
            quantity=intent.quantity,
            trading_mode=intent.trading_mode.strip().lower(),
            order_type=intent.order_type,
            limit_price=intent.limit_price,
            source=intent.source,
            reason=intent.reason,
            current_price=intent.current_price,
            ai_decision_id=intent.ai_decision_id,
            strategy_signal_id=intent.strategy_signal_id,
        )
        return ApprovalResult(
            approved=True,
            reason="All risk checks passed",
            normalized_intent=normalized,
            checks=checks,
        )

    def _validate_context_config(self, context: RiskContext) -> None:
        if context.max_trades_per_day < 0:
            raise RiskEngineConfigurationError("max_trades_per_day must be non-negative")
        if context.max_daily_loss_usd < 0:
            raise RiskEngineConfigurationError("max_daily_loss_usd must be non-negative")
        if not 0 <= context.buying_power_reserve_pct < 1:
            raise RiskEngineConfigurationError(
                "buying_power_reserve_pct must be in [0, 1)"
            )
        if not 0 < context.max_position_pct_of_buying_power <= 1:
            raise RiskEngineConfigurationError(
                "max_position_pct_of_buying_power must be in (0, 1]"
            )

    def _checks(self) -> List[Tuple[str, CheckFn]]:
        return [
            ("emergency_halt", self._check_emergency_halt),
            ("trading_mode_allowed", self._check_trading_mode),
            ("live_trading_blocked", self._check_live_trading_blocked),
            ("tastytrade_env_sandbox", self._check_tastytrade_env),
            ("symbol_allowed", self._check_symbol),
            ("side_buy_only", self._check_side),
            ("quantity_positive", self._check_quantity),
            ("current_price_positive", self._check_current_price),
            ("buying_power_present", self._check_buying_power_present),
            ("buying_power_positive", self._check_buying_power_positive),
            ("order_cost_within_reserve", self._check_reserve),
            ("order_cost_within_max_position", self._check_max_position),
            ("max_trades_per_day", self._check_max_trades),
            ("max_daily_loss", self._check_daily_loss),
            ("single_position_mode", self._check_single_position),
            ("no_pending_orders", self._check_pending_orders),
            ("market_data_healthy", self._check_market_data),
            ("ai_decision_valid", self._check_ai_decision),
            ("strategy_signal_valid", self._check_strategy_signal),
        ]

    @staticmethod
    def _resolve_price(intent: OrderIntent, context: RiskContext) -> Optional[float]:
        if intent.current_price is not None:
            return intent.current_price
        return context.current_price

    @staticmethod
    def _check_emergency_halt(intent: OrderIntent, context: RiskContext) -> Tuple[bool, str, Optional[str]]:
        if context.emergency_halt:
            return False, "Emergency halt is active", "EMERGENCY_HALT"
        return True, "Emergency halt is off", None

    @staticmethod
    def _check_trading_mode(intent: OrderIntent, context: RiskContext) -> Tuple[bool, str, Optional[str]]:
        mode = context.trading_mode.strip().lower()
        if mode not in ALLOWED_TRADING_MODES_PHASE_2:
            return False, f"Trading mode {context.trading_mode!r} is not allowed in Phase 2", "TRADING_MODE_BLOCKED"
        return True, "Trading mode allowed", None

    @staticmethod
    def _check_live_trading_blocked(intent: OrderIntent, context: RiskContext) -> Tuple[bool, str, Optional[str]]:
        if context.live_trading_enabled:
            return False, "Live trading is disabled in Phase 2", "LIVE_TRADING_BLOCKED"
        if context.trading_mode.strip().lower() == "live":
            return False, "TRADING_MODE=live is blocked in Phase 2", "LIVE_TRADING_BLOCKED"
        return True, "Live trading blocked as required", None

    @staticmethod
    def _check_tastytrade_env(intent: OrderIntent, context: RiskContext) -> Tuple[bool, str, Optional[str]]:
        env = context.tastytrade_env.strip().lower()
        if env != "sandbox":
            return (
                False,
                "Tastytrade environment must be sandbox for order execution in Phase 2",
                "BROKER_ENV_BLOCKED",
            )
        return True, "Tastytrade environment is sandbox", None

    @staticmethod
    def _check_symbol(intent: OrderIntent, context: RiskContext) -> Tuple[bool, str, Optional[str]]:
        symbol = intent.symbol.strip().upper()
        if symbol not in ALLOWED_SYMBOLS:
            return False, f"Symbol {intent.symbol!r} is not allowed", "INVALID_SYMBOL"
        return True, "Symbol allowed", None

    @staticmethod
    def _check_side(intent: OrderIntent, context: RiskContext) -> Tuple[bool, str, Optional[str]]:
        side = intent.side.strip().lower()
        if side not in ALLOWED_SIDES_PHASE_2:
            return False, f"Side {intent.side!r} is not allowed in Phase 2 (buy only)", "INVALID_SIDE"
        return True, "Side is buy", None

    @staticmethod
    def _check_quantity(intent: OrderIntent, context: RiskContext) -> Tuple[bool, str, Optional[str]]:
        if intent.quantity <= 0:
            return False, "Quantity must be positive", "INVALID_QUANTITY"
        return True, "Quantity is positive", None

    @staticmethod
    def _check_current_price(intent: OrderIntent, context: RiskContext) -> Tuple[bool, str, Optional[str]]:
        price = RiskEngine._resolve_price(intent, context)
        if price is None or price <= 0:
            return False, "current_price must be positive before order approval", "INVALID_PRICE"
        return True, "Current price is valid", None

    @staticmethod
    def _check_buying_power_present(intent: OrderIntent, context: RiskContext) -> Tuple[bool, str, Optional[str]]:
        if context.buying_power is None:
            return False, "buying_power is required and must not use fallback values", "BUYING_POWER_MISSING"
        return True, "Buying power is present", None

    @staticmethod
    def _check_buying_power_positive(intent: OrderIntent, context: RiskContext) -> Tuple[bool, str, Optional[str]]:
        if context.buying_power is None or context.buying_power <= 0:
            return False, "buying_power must be positive", "BUYING_POWER_INVALID"
        return True, "Buying power is positive", None

    @staticmethod
    def _check_reserve(intent: OrderIntent, context: RiskContext) -> Tuple[bool, str, Optional[str]]:
        price = RiskEngine._resolve_price(intent, context)
        assert price is not None
        cost = intent.quantity * price
        max_spend = context.buying_power * (1.0 - context.buying_power_reserve_pct)
        if cost > max_spend:
            return (
                False,
                "Estimated order cost exceeds buying power after reserve",
                "INSUFFICIENT_BUYING_POWER_RESERVE",
            )
        return True, "Order cost within buying power reserve", None

    @staticmethod
    def _check_max_position(intent: OrderIntent, context: RiskContext) -> Tuple[bool, str, Optional[str]]:
        price = RiskEngine._resolve_price(intent, context)
        assert price is not None
        cost = intent.quantity * price
        max_cost = context.buying_power * context.max_position_pct_of_buying_power
        if cost > max_cost:
            return (
                False,
                "Estimated order cost exceeds max position size",
                "MAX_POSITION_EXCEEDED",
            )
        return True, "Order cost within max position limit", None

    @staticmethod
    def _check_max_trades(intent: OrderIntent, context: RiskContext) -> Tuple[bool, str, Optional[str]]:
        if context.trades_today_count >= context.max_trades_per_day:
            return False, "Maximum trades per day reached", "MAX_TRADES_PER_DAY"
        return True, "Trades today within limit", None

    @staticmethod
    def _check_daily_loss(intent: OrderIntent, context: RiskContext) -> Tuple[bool, str, Optional[str]]:
        if context.daily_pnl <= -abs(context.max_daily_loss_usd):
            return False, "Daily loss limit reached", "MAX_DAILY_LOSS"
        return True, "Daily PnL within limit", None

    @staticmethod
    def _check_single_position(intent: OrderIntent, context: RiskContext) -> Tuple[bool, str, Optional[str]]:
        if context.single_position_mode and context.open_positions_count > 0:
            return False, "Single-position mode: open position already exists", "OPEN_POSITION_EXISTS"
        return True, "Single-position check passed", None

    @staticmethod
    def _check_pending_orders(intent: OrderIntent, context: RiskContext) -> Tuple[bool, str, Optional[str]]:
        if context.pending_orders_count > 0:
            return False, "Pending orders must be cleared before new order", "PENDING_ORDERS"
        return True, "No pending orders", None

    @staticmethod
    def _check_market_data(intent: OrderIntent, context: RiskContext) -> Tuple[bool, str, Optional[str]]:
        if not context.market_data_healthy:
            return False, "Market data is unhealthy", "MARKET_DATA_UNHEALTHY"
        return True, "Market data is healthy", None

    @staticmethod
    def _check_ai_decision(intent: OrderIntent, context: RiskContext) -> Tuple[bool, str, Optional[str]]:
        if intent.ai_decision_id is not None and not context.ai_decision_valid:
            return False, "AI decision is invalid or missing", "AI_DECISION_INVALID"
        return True, "AI decision valid or not required", None

    @staticmethod
    def _check_strategy_signal(intent: OrderIntent, context: RiskContext) -> Tuple[bool, str, Optional[str]]:
        if intent.strategy_signal_id is not None and not context.strategy_signal_valid:
            return False, "Strategy signal is invalid or missing", "STRATEGY_SIGNAL_INVALID"
        return True, "Strategy signal valid or not required", None
