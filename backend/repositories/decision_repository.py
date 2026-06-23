"""Decision and risk-approval logging repository."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from backend.db.models import DecisionLog
from backend.utils.redact import redact_dict


class DecisionRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def log_decision(
        self,
        *,
        decision_type: str,
        source: str,
        symbol: Optional[str] = None,
        approved: Optional[bool] = None,
        rejection_code: Optional[str] = None,
        reason: str = "",
        payload: Optional[Dict[str, Any]] = None,
    ) -> DecisionLog:
        record = DecisionLog(
            decision_type=decision_type,
            symbol=symbol.upper() if symbol else None,
            source=source,
            approved=approved,
            rejection_code=rejection_code,
            reason=reason,
            payload_json=json.dumps(redact_dict(payload or {})),
            created_at=datetime.utcnow(),
        )
        self._session.add(record)
        self._session.commit()
        self._session.refresh(record)
        return record

    def log_risk_approval(
        self,
        *,
        symbol: str,
        source: str,
        reason: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> DecisionLog:
        return self.log_decision(
            decision_type="risk_approval",
            source=source,
            symbol=symbol,
            approved=True,
            reason=reason,
            payload=payload,
        )

    def log_risk_rejection(
        self,
        *,
        symbol: str,
        source: str,
        rejection_code: str,
        reason: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> DecisionLog:
        return self.log_decision(
            decision_type="risk_rejection",
            source=source,
            symbol=symbol,
            approved=False,
            rejection_code=rejection_code,
            reason=reason,
            payload=payload,
        )

    def get_recent_decisions(self, limit: int = 50) -> List[DecisionLog]:
        return (
            self._session.query(DecisionLog)
            .order_by(DecisionLog.created_at.desc())
            .limit(limit)
            .all()
        )
