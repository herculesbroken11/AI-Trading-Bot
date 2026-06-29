"""Order persistence repository."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from backend.db.models import OrderRecord
from backend.utils.redact import redact_dict


class OrderRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create_pending_order(
        self,
        *,
        order_id: str,
        mode: str,
        symbol: str,
        side: str,
        quantity: int,
        order_type: str = "Market",
        limit_price: Optional[float] = None,
        message: str = "",
        raw: Optional[Dict[str, Any]] = None,
    ) -> OrderRecord:
        record = OrderRecord(
            order_id=order_id,
            mode=mode,
            symbol=symbol.upper(),
            side=side.lower(),
            quantity=quantity,
            order_type=order_type,
            limit_price=limit_price,
            status="pending",
            message=message,
            raw_json=json.dumps(redact_dict(raw or {})),
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        self._session.add(record)
        self._session.commit()
        self._session.refresh(record)
        return record

    def mark_order_filled(
        self,
        order_id: str,
        *,
        fill_price: float,
        broker_order_id: Optional[str] = None,
        limit_price: Optional[float] = None,
        raw: Optional[Dict[str, Any]] = None,
    ) -> Optional[OrderRecord]:
        record = self._session.query(OrderRecord).filter(OrderRecord.order_id == order_id).first()
        if not record:
            return None
        record.status = "filled"
        record.fill_price = fill_price
        record.broker_order_id = broker_order_id
        if limit_price is not None:
            record.limit_price = limit_price
        if raw:
            record.raw_json = json.dumps(redact_dict(raw))
        record.updated_at = datetime.utcnow()
        self._session.commit()
        self._session.refresh(record)
        return record

    def mark_order_rejected(
        self,
        order_id: str,
        *,
        rejection_code: str,
        message: str,
        mode: str = "paper",
        symbol: str = "",
        side: str = "buy",
        quantity: int = 0,
        order_type: str = "Market",
        limit_price: Optional[float] = None,
        raw: Optional[Dict[str, Any]] = None,
    ) -> Optional[OrderRecord]:
        record = self._session.query(OrderRecord).filter(OrderRecord.order_id == order_id).first()
        if not record:
            record = OrderRecord(
                order_id=order_id,
                mode=mode,
                symbol=symbol.upper() if symbol else "",
                side=side.lower() if side else "buy",
                quantity=quantity,
                order_type=order_type,
                limit_price=limit_price,
                status="rejected",
            )
            self._session.add(record)
        record.status = "rejected"
        record.rejection_code = rejection_code
        record.message = message
        if limit_price is not None:
            record.limit_price = limit_price
        if raw:
            record.raw_json = json.dumps(redact_dict(raw))
        record.updated_at = datetime.utcnow()
        self._session.commit()
        self._session.refresh(record)
        return record

    def mark_order_error(
        self,
        order_id: str,
        *,
        message: str,
        rejection_code: str = "EXECUTION_ERROR",
        raw: Optional[Dict[str, Any]] = None,
    ) -> Optional[OrderRecord]:
        record = self._session.query(OrderRecord).filter(OrderRecord.order_id == order_id).first()
        if not record:
            return None
        record.status = "error"
        record.rejection_code = rejection_code
        record.message = message
        if raw:
            record.raw_json = json.dumps(redact_dict(raw))
        record.updated_at = datetime.utcnow()
        self._session.commit()
        self._session.refresh(record)
        return record

    def finalize_execution(
        self,
        pending_order_id: str,
        final_order_id: str,
        *,
        status: str,
        fill_price: Optional[float] = None,
        limit_price: Optional[float] = None,
        broker_order_id: Optional[str] = None,
        raw: Optional[Dict[str, Any]] = None,
    ) -> Optional[OrderRecord]:
        record = self._session.query(OrderRecord).filter(OrderRecord.order_id == pending_order_id).first()
        if not record:
            return self.mark_order_filled(
                final_order_id,
                fill_price=float(fill_price or 0),
                broker_order_id=broker_order_id,
                limit_price=limit_price,
                raw=raw,
            )
        record.order_id = final_order_id
        record.status = status
        record.fill_price = fill_price
        record.broker_order_id = broker_order_id
        if limit_price is not None:
            record.limit_price = limit_price
        if raw:
            record.raw_json = json.dumps(redact_dict(raw))
        record.updated_at = datetime.utcnow()
        self._session.commit()
        self._session.refresh(record)
        return record

    def finalize_paper_fill(
        self,
        pending_order_id: str,
        paper_order_id: str,
        *,
        fill_price: float,
        raw: Optional[Dict[str, Any]] = None,
    ) -> Optional[OrderRecord]:
        return self.finalize_execution(
            pending_order_id,
            paper_order_id,
            status="filled",
            fill_price=fill_price,
            raw=raw,
        )

    def get_by_order_id(self, order_id: str) -> Optional[OrderRecord]:
        return self._session.query(OrderRecord).filter(OrderRecord.order_id == order_id).first()

    def get_by_broker_order_id(self, broker_order_id: str) -> Optional[OrderRecord]:
        return (
            self._session.query(OrderRecord)
            .filter(OrderRecord.broker_order_id == str(broker_order_id))
            .first()
        )

    def mark_order_cancelled(
        self,
        *,
        broker_order_id: str,
        raw: Optional[Dict[str, Any]] = None,
    ) -> Optional[OrderRecord]:
        record = self.get_by_broker_order_id(broker_order_id)
        if not record:
            record = self.get_by_order_id(f"SANDBOX-{broker_order_id}")
        if not record:
            return None
        record.status = "cancelled"
        record.message = "Sandbox order cancelled"
        if raw:
            record.raw_json = json.dumps(redact_dict(raw))
        record.updated_at = datetime.utcnow()
        self._session.commit()
        self._session.refresh(record)
        return record

    def mark_cancel_failed(
        self,
        *,
        broker_order_id: str,
        message: str,
        raw: Optional[Dict[str, Any]] = None,
    ) -> Optional[OrderRecord]:
        record = self.get_by_broker_order_id(broker_order_id)
        if not record:
            record = self.get_by_order_id(f"SANDBOX-{broker_order_id}")
        if not record:
            return None
        record.status = "cancel_failed"
        record.message = message
        if raw:
            record.raw_json = json.dumps(redact_dict(raw))
        record.updated_at = datetime.utcnow()
        self._session.commit()
        self._session.refresh(record)
        return record

    def get_recent_orders(self, limit: int = 50) -> List[OrderRecord]:
        return (
            self._session.query(OrderRecord)
            .order_by(OrderRecord.created_at.desc())
            .limit(limit)
            .all()
        )
