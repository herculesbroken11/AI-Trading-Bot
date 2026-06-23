"""Repository persistence tests using in-memory SQLite."""

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.db.base import Base
import backend.database  # noqa: F401 — legacy models
import backend.db.models  # noqa: F401
from backend.repositories.bot_state_repository import BotStateRepository
from backend.repositories.decision_repository import DecisionRepository
from backend.repositories.error_repository import ErrorRepository
from backend.repositories.order_repository import OrderRepository


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def test_order_repository_lifecycle(db_session):
    repo = OrderRepository(db_session)
    pending = repo.create_pending_order(
        order_id="PAPER-test-1",
        mode="paper",
        symbol="TNA",
        side="buy",
        quantity=10,
        message="pending",
    )
    assert pending.status == "pending"

    filled = repo.mark_order_filled("PAPER-test-1", fill_price=50.0)
    assert filled.status == "filled"
    assert filled.fill_price == 50.0

    recent = repo.get_recent_orders()
    assert len(recent) == 1


def test_decision_repository_logs_risk_rejection(db_session):
    repo = DecisionRepository(db_session)
    record = repo.log_risk_rejection(
        symbol="TZA",
        source="test",
        rejection_code="INVALID_SIDE",
        reason="sell not allowed",
        payload={"access_token": "secret-token"},
    )
    payload = json.loads(record.payload_json)
    assert payload["access_token"] == "[REDACTED]"


def test_error_repository_redacts_context(db_session):
    repo = ErrorRepository(db_session)
    record = repo.log_error(
        source="test",
        message="failure",
        context={"password": "abc123", "symbol": "TNA"},
    )
    ctx = json.loads(record.context_json)
    assert ctx["password"] == "[REDACTED]"
    assert ctx["symbol"] == "TNA"


def test_bot_state_repository_restart_fields(db_session):
    repo = BotStateRepository(db_session)
    state = repo.set_running(trading_mode="paper")
    assert state.running is True
    repo.set_active_trade_id(42)
    state = repo.get_state()
    assert state.active_trade_id == 42
    repo.clear_active_trade_id()
    assert repo.get_state().active_trade_id is None
    repo.set_stopped()
    assert repo.get_state().running is False
