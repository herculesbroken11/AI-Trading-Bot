"""
SQLAlchemy models for Phase 2 logging and bot state.

Legacy trade/prediction models remain in backend/database.py for compatibility.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, Column, DateTime, Float, Integer, String, Text

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


class ShadowSignalLog(Base):
    """Shadow-mode (observation only) signal decisions and follow-up movement. Never orders."""

    __tablename__ = "shadow_signal_log"
    __table_args__ = (
        CheckConstraint("NOT submitted", name="ck_shadow_signal_log_never_submitted"),
        CheckConstraint("production_execution_blocked", name="ck_shadow_signal_log_production_blocked"),
    )

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(String, nullable=False, index=True)
    cycle_number = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    decision = Column(String, nullable=False)  # bullish | bearish | skip
    selected_symbol = Column(String, nullable=True)  # TNA | TZA | null
    confidence_score = Column(Float, nullable=False, default=0.0)
    bullish_score = Column(Float, nullable=False, default=0.0)
    bearish_score = Column(Float, nullable=False, default=0.0)
    skip_reason = Column(String, nullable=True, index=True)
    market_regime = Column(String, nullable=True)
    explanation = Column(Text, nullable=True)
    quote_age_tna = Column(Float, nullable=True)
    quote_age_tza = Column(Float, nullable=True)
    quote_age_iwm = Column(Float, nullable=True)
    quote_age_spy = Column(Float, nullable=True)
    quote_age_qqq = Column(Float, nullable=True)
    tna_mid = Column(Float, nullable=True)
    tza_mid = Column(Float, nullable=True)
    iwm_mid = Column(Float, nullable=True)
    spy_mid = Column(Float, nullable=True)
    qqq_mid = Column(Float, nullable=True)
    vix_last = Column(Float, nullable=True)
    freshness_gate_passed = Column(Boolean, nullable=False, default=False)
    raw_snapshot_json = Column(Text, nullable=True)
    raw_score_json = Column(Text, nullable=True)
    submitted = Column(Boolean, nullable=False, default=False)
    production_execution_blocked = Column(Boolean, nullable=False, default=True)
    followup_seconds = Column(Float, nullable=True)
    tna_mid_after = Column(Float, nullable=True)
    tza_mid_after = Column(Float, nullable=True)
    iwm_mid_after = Column(Float, nullable=True)
    spy_mid_after = Column(Float, nullable=True)
    qqq_mid_after = Column(Float, nullable=True)
    selected_symbol_move_pct = Column(Float, nullable=True)
    iwm_move_pct = Column(Float, nullable=True)
    direction_was_correct = Column(Boolean, nullable=True)
    outcome_note = Column(Text, nullable=True)
    # Market session metadata (migration 004); NULL on rows logged before 2.14.
    session_label = Column(String, nullable=True, index=True)
    session_is_regular_hours = Column(Boolean, nullable=True)
    session_is_near_close = Column(Boolean, nullable=True)
    session_minutes_to_close = Column(Float, nullable=True)
    session_guard_passed = Column(Boolean, nullable=True)
    session_guard_reason = Column(Text, nullable=True)
