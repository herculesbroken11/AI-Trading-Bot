from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base
from sqlalchemy import Column, Integer, String, Float, DateTime, Text, Boolean
from datetime import datetime
import os

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///tradebot.db")

engine = create_async_engine(DATABASE_URL, echo=True)
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()

class Trade(Base):
    __tablename__ = "trades"

    trade_id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String, index=True)
    side = Column(String)  # "buy" or "sell"
    entry_price = Column(Float)
    exit_price = Column(Float, nullable=True)
    quantity = Column(Integer, default=1)
    pnl = Column(Float, default=0.0)
    confidence = Column(Float)
    status = Column(String, default="open")  # "open", "closed"
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

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()

