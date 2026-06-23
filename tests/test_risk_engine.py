"""Tests for RiskEngine (Checkpoint 2.1)."""

import pytest

from backend.risk.models import OrderIntent, OrderIntentValidationError
from backend.risk.risk_engine import RiskEngine, RiskEngineConfigurationError
from tests.execution_helpers import make_context, make_intent

ENGINE = RiskEngine()


def test_valid_paper_tna_buy_approved():
    result = ENGINE.approve(make_intent(symbol="TNA"), make_context())
    assert result.approved is True
    assert result.normalized_intent is not None
    assert result.normalized_intent.symbol == "TNA"


def test_valid_paper_tza_buy_approved():
    result = ENGINE.approve(
        make_intent(symbol="TZA", current_price=20.0),
        make_context(current_price=20.0),
    )
    assert result.approved is True
    assert result.normalized_intent.symbol == "TZA"


def test_tza_sell_rejected():
    with pytest.raises(OrderIntentValidationError):
        make_intent(symbol="TZA", side="sell")


def test_tna_sell_rejected():
    with pytest.raises(OrderIntentValidationError):
        make_intent(symbol="TNA", side="sell")


def test_invalid_symbol_rejected():
    with pytest.raises(OrderIntentValidationError):
        make_intent(symbol="SPY")


def test_quantity_zero_rejected():
    with pytest.raises(OrderIntentValidationError):
        make_intent(quantity=0)


def test_current_price_none_rejected():
    result = ENGINE.approve(
        make_intent(current_price=None),
        make_context(current_price=None),
    )
    assert result.approved is False
    assert result.rejection_code == "INVALID_PRICE"


def test_buying_power_none_rejected():
    result = ENGINE.approve(make_intent(), make_context(buying_power=None))
    assert result.approved is False
    assert result.rejection_code == "BUYING_POWER_MISSING"


def test_buying_power_zero_rejected():
    result = ENGINE.approve(make_intent(), make_context(buying_power=0))
    assert result.approved is False
    assert result.rejection_code == "BUYING_POWER_INVALID"


def test_insufficient_buying_power_rejected():
    result = ENGINE.approve(
        make_intent(quantity=1000, current_price=100.0),
        make_context(buying_power=1000.0, max_position_pct_of_buying_power=0.25),
    )
    assert result.approved is False
    assert result.rejection_code in {"MAX_POSITION_EXCEEDED", "INSUFFICIENT_BUYING_POWER_RESERVE"}


def test_emergency_halt_rejects():
    result = ENGINE.approve(make_intent(), make_context(emergency_halt=True))
    assert result.approved is False
    assert result.rejection_code == "EMERGENCY_HALT"


def test_live_mode_rejected():
    result = ENGINE.approve(
        make_intent(trading_mode="paper"),
        make_context(trading_mode="live"),
    )
    assert result.approved is False
    assert result.rejection_code == "TRADING_MODE_BLOCKED"


def test_production_tastytrade_env_rejected():
    result = ENGINE.approve(make_intent(), make_context(tastytrade_env="production"))
    assert result.approved is False
    assert result.rejection_code == "BROKER_ENV_BLOCKED"


def test_max_trades_per_day_enforced():
    result = ENGINE.approve(make_intent(), make_context(trades_today_count=1, max_trades_per_day=1))
    assert result.approved is False
    assert result.rejection_code == "MAX_TRADES_PER_DAY"


def test_max_daily_loss_enforced():
    result = ENGINE.approve(make_intent(), make_context(daily_pnl=-500.0, max_daily_loss_usd=500.0))
    assert result.approved is False
    assert result.rejection_code == "MAX_DAILY_LOSS"


def test_open_position_rejected_single_position_mode():
    result = ENGINE.approve(make_intent(), make_context(open_positions_count=1))
    assert result.approved is False
    assert result.rejection_code == "OPEN_POSITION_EXISTS"


def test_pending_order_rejected():
    result = ENGINE.approve(make_intent(), make_context(pending_orders_count=1))
    assert result.approved is False
    assert result.rejection_code == "PENDING_ORDERS"


def test_unhealthy_market_data_rejected():
    result = ENGINE.approve(make_intent(), make_context(market_data_healthy=False))
    assert result.approved is False
    assert result.rejection_code == "MARKET_DATA_UNHEALTHY"


def test_invalid_ai_decision_rejected():
    result = ENGINE.approve(
        make_intent(ai_decision_id="dec-1"),
        make_context(ai_decision_valid=False),
    )
    assert result.approved is False
    assert result.rejection_code == "AI_DECISION_INVALID"


def test_invalid_strategy_signal_rejected():
    result = ENGINE.approve(
        make_intent(strategy_signal_id="sig-1"),
        make_context(strategy_signal_valid=False),
    )
    assert result.approved is False
    assert result.rejection_code == "STRATEGY_SIGNAL_INVALID"


def test_invalid_risk_config_raises():
    with pytest.raises(RiskEngineConfigurationError):
        ENGINE.approve(make_intent(), make_context(max_trades_per_day=-1))
