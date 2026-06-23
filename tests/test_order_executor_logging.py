"""OrderExecutor repository-backed logging tests."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.db.base import Base
import backend.database  # noqa: F401
import backend.db.models  # noqa: F401
from backend.execution.execution_router import ExecutionRouter
from backend.execution.order_executor import OrderExecutor
from backend.execution.paper_simulator import PaperSimulator
from backend.repositories.decision_repository import DecisionRepository
from backend.repositories.error_repository import ErrorRepository
from backend.repositories.order_repository import OrderRepository
from backend.risk.models import OrderIntent, RiskContext
from tests.execution_helpers import make_context


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def _executor(session) -> OrderExecutor:
    paper = PaperSimulator()
    router = ExecutionRouter(paper_simulator=paper)
    return OrderExecutor(
        router=router,
        order_repository=OrderRepository(session),
        decision_repository=DecisionRepository(session),
        error_repository=ErrorRepository(session),
    )


def _paper_context(**overrides):
    return make_context(trading_mode="paper", **overrides)


def test_executor_logs_paper_fill(db_session):
    executor = _executor(db_session)
    intent = OrderIntent(
        symbol="TNA",
        side="buy",
        quantity=2,
        trading_mode="paper",
        current_price=50.0,
        source="test",
    )
    result = executor.execute(intent, _paper_context())
    assert result.success is True
    assert result.status == "filled"
    assert result.order_id.startswith("PAPER-")

    orders = OrderRepository(db_session).get_recent_orders()
    assert len(orders) == 1
    assert orders[0].status == "filled"
    assert orders[0].order_id.startswith("PAPER-")
    assert orders[0].fill_price == 50.0

    decisions = DecisionRepository(db_session).get_recent_decisions()
    assert any(d.decision_type == "risk_approval" for d in decisions)


def test_executor_logs_risk_rejection(db_session):
    executor = _executor(db_session)
    intent = OrderIntent(
        symbol="TNA",
        side="buy",
        quantity=1,
        trading_mode="paper",
        current_price=50.0,
        source="test",
    )
    result = executor.execute(intent, _paper_context(buying_power=None))
    assert result.status == "rejected"

    decisions = DecisionRepository(db_session).get_recent_decisions()
    assert any(d.decision_type == "risk_rejection" for d in decisions)

    orders = OrderRepository(db_session).get_recent_orders()
    assert len(orders) == 1
    assert orders[0].status == "rejected"
    assert orders[0].symbol == "TNA"


def test_executor_logs_error_on_router_failure(db_session, monkeypatch):
    executor = _executor(db_session)

    def boom(*args, **kwargs):
        raise RuntimeError("simulated router failure")

    monkeypatch.setattr(executor.router, "route", boom)

    intent = OrderIntent(
        symbol="TZA",
        side="buy",
        quantity=1,
        trading_mode="paper",
        current_price=30.0,
        source="test",
    )
    result = executor.execute(intent, _paper_context(current_price=30.0))
    assert result.status == "error"

    errors = ErrorRepository(db_session).get_recent_errors()
    assert len(errors) >= 1
    assert errors[0].source == "order_executor.execute"

    orders = OrderRepository(db_session).get_recent_orders()
    assert len(orders) == 1
    assert orders[0].status == "error"
