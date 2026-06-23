from sqlalchemy import Column, Integer, String, Float, DateTime, Text, Boolean
from datetime import datetime

from backend.db.base import Base
from backend.db.session import configure_engine, get_db, get_engine, get_session_local

# Backward-compatible session factory: SessionLocal() returns a new Session.
SessionLocal = get_session_local


class Trade(Base):
    __tablename__ = "trades"

    trade_id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String, index=True)
    side = Column(String)
    entry_price = Column(Float)
    exit_price = Column(Float, nullable=True)
    quantity = Column(Integer, default=1)
    pnl = Column(Float, default=0.0)
    confidence = Column(Float)
    status = Column(String, default="open")
    ai_reasoning = Column(Text, nullable=True)
    take_profit = Column(Float, default=0.05)
    early_exit_target = Column(Float, default=0.01)
    stop_loss = Column(Float, default=0.05)
    trailing_stop = Column(Float, nullable=True)
    exit_reason = Column(String, nullable=True)
    partial_exit_pct = Column(Float, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    entry_time = Column(DateTime, default=datetime.utcnow)
    closed_at = Column(DateTime, nullable=True)


class AIPrediction(Base):
    __tablename__ = "ai_predictions"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String, index=True)
    direction = Column(String)
    confidence = Column(Float)
    entry_price = Column(Float, nullable=True)
    take_profit = Column(Float, nullable=True)
    early_exit_profit = Column(Float, nullable=True)
    stop_loss = Column(Float, nullable=True)
    use_trailing_stop = Column(Boolean, default=False)
    skip_trade = Column(Boolean, default=False)
    analysis_json = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class TrendSignal(Base):
    __tablename__ = "trend_signals"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String, index=True)
    macro_trend = Column(String)
    seasonal_bias = Column(String, nullable=True)
    institutional_flow = Column(String, nullable=True)
    bias = Column(String, nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class OAuthToken(Base):
    __tablename__ = "oauth_tokens"

    id = Column(Integer, primary_key=True, index=True)
    provider = Column(String, index=True)
    access_token = Column(Text, nullable=False)
    refresh_token = Column(Text, nullable=True)
    token_type = Column(String, nullable=True)
    scope = Column(String, nullable=True)
    expires_in = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, index=True)


def init_db() -> None:
    """Create all tables (legacy + Phase 2 logging). Dev/test only without Alembic."""
    from backend.config.settings import get_settings

    import backend.db.models  # noqa: F401 — Phase 2 logging models

    settings = get_settings()
    configure_engine(settings.database_url, sql_echo=settings.sql_echo)
    Base.metadata.create_all(bind=get_engine())
