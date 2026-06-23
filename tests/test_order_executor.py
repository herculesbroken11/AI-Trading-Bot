"""Tests for OrderExecutor (Checkpoint 2.1)."""

from unittest.mock import MagicMock

from backend.execution.execution_router import ExecutionRouter
from backend.execution.order_executor import OrderExecutor
from backend.execution.paper_simulator import PaperSimulator
from backend.risk.risk_engine import RiskEngine
from tests.execution_helpers import make_context, make_intent


def test_risk_engine_runs_before_router():
    risk = RiskEngine()
    router = MagicMock(spec=ExecutionRouter)
    executor = OrderExecutor(risk_engine=risk, router=router)

    result = executor.execute(make_intent(), make_context(emergency_halt=True))

    assert result.status == "rejected"
    router.route.assert_not_called()


def test_rejected_risk_does_not_call_router():
    router = MagicMock(spec=ExecutionRouter)
    executor = OrderExecutor(router=router)
    result = executor.execute(make_intent(), make_context(buying_power=None))
    assert result.success is False
    router.route.assert_not_called()


def test_approved_paper_order_fills_through_paper_simulator():
    paper = PaperSimulator()
    router = ExecutionRouter(paper_simulator=paper)
    executor = OrderExecutor(router=router)

    result = executor.execute(make_intent(symbol="TNA", quantity=5), make_context())

    assert result.success is True
    assert result.status == "filled"
    assert result.symbol == "TNA"
    assert result.side == "buy"
    assert result.quantity == 5
    assert result.trading_mode == "paper"
    assert result.order_id.startswith("PAPER-")


def test_tza_paper_order_fills():
    paper = PaperSimulator()
    router = ExecutionRouter(paper_simulator=paper)
    executor = OrderExecutor(router=router)
    result = executor.execute(
        make_intent(symbol="TZA", quantity=3, current_price=18.0),
        make_context(current_price=18.0),
    )
    assert result.success is True
    assert result.symbol == "TZA"
    assert result.side == "buy"
