"""ExecutionRouter sandbox routing tests."""

from unittest.mock import MagicMock

from backend.adapters.broker.tastytrade_sandbox import TastytradeSandboxAdapter
from backend.execution.order_executor import OrderExecutor
from backend.execution.execution_router import ExecutionRouter
from backend.risk.risk_engine import RiskEngine
from backend.risk.models import ExecutionResult
from tests.execution_helpers import make_context, make_intent


def test_sandbox_routes_to_adapter():
    mock_adapter = MagicMock(spec=TastytradeSandboxAdapter)
    mock_adapter.execute_order.return_value = ExecutionResult(
        success=True,
        status="filled",
        order_id="SANDBOX-123",
        symbol="TNA",
        side="buy",
        quantity=1,
        fill_price=1.0,
        trading_mode="sandbox",
        message="ok",
    )
    router = ExecutionRouter(sandbox_adapter=mock_adapter)
    intent = make_intent(symbol="TNA", trading_mode="sandbox", quantity=1)
    context = make_context(trading_mode="sandbox")
    result = router.route(intent, context)
    assert result.success is True
    mock_adapter.execute_order.assert_called_once()


def test_sandbox_rejects_sell():
    router = ExecutionRouter(sandbox_adapter=MagicMock())
    intent = make_intent(symbol="TNA", trading_mode="sandbox", side="buy")
    intent_sell = make_intent(symbol="TNA", trading_mode="sandbox")
    # OrderIntent validation blocks sell at construction — test router guard via mock side
    from backend.risk.models import OrderIntent

    class FakeIntent:
        symbol = "TNA"
        side = "sell"
        quantity = 1
        trading_mode = "sandbox"
        order_type = "Market"

    result = router.route(FakeIntent(), make_context(trading_mode="sandbox"))
    assert result.success is False
    assert "buy" in result.message.lower()


def test_sandbox_rejects_symbol_outside_tna_tza():
    router = ExecutionRouter(sandbox_adapter=MagicMock())

    class FakeIntent:
        symbol = "SPY"
        side = "buy"
        quantity = 1
        trading_mode = "sandbox"
        order_type = "Market"

    result = router.route(FakeIntent(), make_context(trading_mode="sandbox"))
    assert result.success is False
    assert "TNA" in result.message or "TZA" in result.message


def test_sandbox_rejects_quantity_over_max():
    router = ExecutionRouter(sandbox_adapter=MagicMock())
    intent = make_intent(symbol="TNA", trading_mode="sandbox", quantity=5)
    result = router.route(intent, make_context(trading_mode="sandbox"))
    assert result.success is False


def test_sandbox_risk_engine_runs_before_adapter():
    """Sandbox orders must pass RiskEngine before ExecutionRouter calls the adapter."""
    risk = RiskEngine()
    mock_adapter = MagicMock()
    mock_adapter.execute_order.return_value = ExecutionResult(
        success=True,
        status="filled",
        order_id="SANDBOX-1",
        symbol="TNA",
        side="buy",
        quantity=1,
        trading_mode="sandbox",
        message="ok",
    )
    router = ExecutionRouter(sandbox_adapter=mock_adapter)
    executor = OrderExecutor(risk_engine=risk, router=router)
    result = executor.execute(
        make_intent(trading_mode="sandbox", quantity=1),
        make_context(trading_mode="sandbox", emergency_halt=True),
    )
    assert result.status == "rejected"
    mock_adapter.execute_order.assert_not_called()


def test_sandbox_without_adapter_returns_error():
    router = ExecutionRouter()
    result = router.route(
        make_intent(trading_mode="sandbox", quantity=1),
        make_context(trading_mode="sandbox"),
    )
    assert result.success is False
    assert "not configured" in result.message.lower()
