"""Bot runtime state persistence."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from backend.db.models import BotStateRecord


class BotStateRepository:
    SINGLETON_ID = 1

    def __init__(self, session: Session) -> None:
        self._session = session

    def _get_or_create(self) -> BotStateRecord:
        record = (
            self._session.query(BotStateRecord)
            .filter(BotStateRecord.id == self.SINGLETON_ID)
            .first()
        )
        if not record:
            record = BotStateRecord(
                id=self.SINGLETON_ID,
                running=False,
                emergency_halt=False,
                trading_mode="paper",
                status_message="initialized",
            )
            self._session.add(record)
            self._session.commit()
            self._session.refresh(record)
        return record

    def get_state(self) -> BotStateRecord:
        return self._get_or_create()

    def set_running(self, *, trading_mode: str, status_message: str = "running") -> BotStateRecord:
        record = self._get_or_create()
        record.running = True
        record.trading_mode = trading_mode
        record.status_message = status_message
        record.last_heartbeat_at = datetime.utcnow()
        record.updated_at = datetime.utcnow()
        self._session.commit()
        self._session.refresh(record)
        return record

    def set_stopped(self, status_message: str = "stopped") -> BotStateRecord:
        record = self._get_or_create()
        record.running = False
        record.status_message = status_message
        record.updated_at = datetime.utcnow()
        self._session.commit()
        self._session.refresh(record)
        return record

    def set_emergency_halt(self, active: bool = True) -> BotStateRecord:
        record = self._get_or_create()
        record.emergency_halt = active
        record.status_message = "emergency_halt" if active else "halt_cleared"
        record.updated_at = datetime.utcnow()
        self._session.commit()
        self._session.refresh(record)
        return record

    def set_active_trade_id(self, trade_id: int) -> BotStateRecord:
        record = self._get_or_create()
        record.active_trade_id = trade_id
        record.updated_at = datetime.utcnow()
        self._session.commit()
        self._session.refresh(record)
        return record

    def clear_active_trade_id(self) -> BotStateRecord:
        record = self._get_or_create()
        record.active_trade_id = None
        record.updated_at = datetime.utcnow()
        self._session.commit()
        self._session.refresh(record)
        return record
