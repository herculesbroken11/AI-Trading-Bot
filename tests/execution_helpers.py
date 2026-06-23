"""Shared fixtures for execution stack tests."""

from backend.risk.models import OrderIntent, RiskContext


def make_intent(**overrides) -> OrderIntent:
    base = {
        "symbol": "TNA",
        "side": "buy",
        "quantity": 10,
        "trading_mode": "paper",
        "current_price": 50.0,
        "reason": "test",
    }
    base.update(overrides)
    return OrderIntent(**base)


def make_context(**overrides) -> RiskContext:
    base = {
        "trading_mode": "paper",
        "live_trading_enabled": False,
        "tastytrade_env": "sandbox",
        "emergency_halt": False,
        "buying_power": 100_000.0,
        "current_price": 50.0,
        "open_positions_count": 0,
        "pending_orders_count": 0,
        "trades_today_count": 0,
        "daily_pnl": 0.0,
        "market_data_healthy": True,
        "max_trades_per_day": 1,
        "max_daily_loss_usd": 500.0,
        "buying_power_reserve_pct": 0.08,
        "max_position_pct_of_buying_power": 0.25,
        "single_position_mode": True,
        "ai_decision_valid": True,
        "strategy_signal_valid": True,
    }
    base.update(overrides)
    return RiskContext(**base)
