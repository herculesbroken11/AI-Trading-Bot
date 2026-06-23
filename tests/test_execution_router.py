"""Tests for ExecutionRouter (Checkpoint 2.1)."""

import importlib

from backend.execution.execution_router import ExecutionRouter
from backend.execution.paper_simulator import PaperSimulator
from tests.execution_helpers import make_context, make_intent


def test_paper_mode_routes_to_paper_simulator():
    paper = PaperSimulator()
    router = ExecutionRouter(paper_simulator=paper)
    intent = make_intent(trading_mode="paper")
    result = router.route(intent, make_context(trading_mode="paper"))
    assert result.success is True
    assert result.status == "filled"
    assert len(paper.fills) == 1


def test_live_mode_blocked():
    router = ExecutionRouter()
    intent = make_intent(trading_mode="paper")
    context = make_context(trading_mode="live")
    result = router.route(intent, context)
    assert result.success is False
    assert result.status == "rejected"


def test_production_tastytrade_env_blocked():
    router = ExecutionRouter()
    result = router.route(make_intent(), make_context(tastytrade_env="production"))
    assert result.success is False
    assert "sandbox" in result.message.lower()


def test_sandbox_mode_does_not_place_real_order():
    router = ExecutionRouter()
    result = router.route(
        make_intent(trading_mode="sandbox"),
        make_context(trading_mode="sandbox"),
    )
    assert result.success is False
    assert result.status == "error"
    assert "not wired" in result.message.lower()
    assert result.raw.get("placed") is False


def test_old_trade_exec_not_imported_by_router():
    source_path = importlib.import_module("backend.execution.execution_router").__file__
    with open(source_path, encoding="utf-8") as fp:
        text = fp.read()
    assert "trade_exec" not in text

    order_executor_path = importlib.import_module("backend.execution.order_executor").__file__
    with open(order_executor_path, encoding="utf-8") as fp:
        lines = [line for line in fp.readlines() if not line.strip().startswith("#")]
    joined = "".join(lines)
    assert "import" not in joined or "trade_exec" not in joined
