"""Bot state repository initialization and recovery foundation tests."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.config.settings import Settings
from backend.db.base import Base
import backend.db.models  # noqa: F401
from backend.repositories.bot_state_repository import BotStateRepository


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def _settings(**overrides) -> Settings:
    base = {
        "live_trading_enabled": False,
        "trading_mode": "paper",
        "tastytrade_env": "sandbox",
        "emergency_halt": False,
    }
    base.update(overrides)
    return Settings(**base)


def test_bot_state_initializes_stopped(db_session):
    repo = BotStateRepository(db_session)
    state = repo.ensure_initialized(_settings())
    assert state.running is False
    assert state.active_trade_id is None
    assert state.trading_mode == "paper"
    assert state.status_message == "initialized"


def test_emergency_halt_persisted(db_session):
    repo = BotStateRepository(db_session)
    state = repo.ensure_initialized(_settings(emergency_halt=True))
    assert state.emergency_halt is True
    repo.set_emergency_halt(False)
    assert repo.get_state().emergency_halt is False


def test_active_trade_id_set_and_clear(db_session):
    repo = BotStateRepository(db_session)
    repo.ensure_initialized(_settings())
    repo.set_active_trade_id(99)
    assert repo.get_state().active_trade_id == 99
    repo.clear_active_trade_id()
    assert repo.get_state().active_trade_id is None


def test_heartbeat_updates_timestamp(db_session):
    repo = BotStateRepository(db_session)
    repo.ensure_initialized(_settings())
    before = repo.get_state().last_heartbeat_at
    state = repo.heartbeat("alive")
    assert state.last_heartbeat_at is not None
    if before is not None:
        assert state.last_heartbeat_at >= before


def test_startup_init_does_not_start_bot(monkeypatch):
    """initialize_bot_state_on_startup must not flip running=True."""
    from backend.bootstrap.bot_state_init import initialize_bot_state_on_startup

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()

    monkeypatch.setattr("backend.db.session.get_db_session", lambda: session)
    initialize_bot_state_on_startup(_settings())
    state = BotStateRepository(session).get_state()
    assert state.running is False
    session.close()
