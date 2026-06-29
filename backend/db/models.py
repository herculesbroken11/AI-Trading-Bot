"""
SQLAlchemy models for Phase 2 logging and bot state.

Legacy trade/prediction models remain in backend/database.py for compatibility.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Float, Integer, String, Text

from backend.db.base import Base


class OrderRecord(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(String, unique=True, index=True, nullable=False)
    broker_order_id = Column(String, nullable=True)
    mode = Column(String, nullable=False)  # paper | sandbox | live
    symbol = Column(String, index=True, nullable=False)
    side = Column(String, nullable=False)
    quantity = Column(Integer, nullable=False)
    order_type = Column(String, default="Market")
    status = Column(String, nullable=False)  # pending | submitted | filled | rejected | error | cancelled
    limit_price = Column(Float, nullable=True)
    fill_price = Column(Float, nullable=True)
    rejection_code = Column(String, nullable=True)
    message = Column(Text, nullable=True)
    raw_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class DecisionLog(Base):
    __tablename__ = "decision_log"

    id = Column(Integer, primary_key=True, index=True)
    decision_type = Column(String, nullable=False)
    symbol = Column(String, nullable=True, index=True)
    source = Column(String, nullable=False)
    approved = Column(Boolean, nullable=True)
    rejection_code = Column(String, nullable=True)
    reason = Column(Text, nullable=True)
    payload_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class ErrorEvent(Base):
    __tablename__ = "error_events"

    id = Column(Integer, primary_key=True, index=True)
    source = Column(String, nullable=False, index=True)
    message = Column(Text, nullable=False)
    error_type = Column(String, nullable=True)
    stack = Column(Text, nullable=True)
    context_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class BotStateRecord(Base):
    __tablename__ = "bot_state"

    id = Column(Integer, primary_key=True, index=True)
    running = Column(Boolean, default=False)
    emergency_halt = Column(Boolean, default=False)
    trading_mode = Column(String, default="paper")
    active_trade_id = Column(Integer, nullable=True)
    last_heartbeat_at = Column(DateTime, nullable=True)
    status_message = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AccountSnapshot(Base):
    __tablename__ = "account_snapshots"

    id = Column(Integer, primary_key=True, index=True)
    mode = Column(String, nullable=False)
    buying_power = Column(Float, nullable=True)
    cash_available = Column(Float, nullable=True)
    open_positions_count = Column(Integer, default=0)
    positions_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
