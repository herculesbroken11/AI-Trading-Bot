"""Error event logging repository."""

from __future__ import annotations

import json
import traceback
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from backend.db.models import ErrorEvent
from backend.utils.redact import redact_dict


class ErrorRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def log_error(
        self,
        *,
        source: str,
        message: str,
        error_type: Optional[str] = None,
        exc: Optional[BaseException] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> ErrorEvent:
        record = ErrorEvent(
            source=source,
            message=message,
            error_type=error_type or (type(exc).__name__ if exc else None),
            stack=traceback.format_exc() if exc else None,
            context_json=json.dumps(redact_dict(context or {})),
            created_at=datetime.utcnow(),
        )
        self._session.add(record)
        self._session.commit()
        self._session.refresh(record)
        return record

    def get_recent_errors(self, limit: int = 50) -> List[ErrorEvent]:
        return (
            self._session.query(ErrorEvent)
            .order_by(ErrorEvent.created_at.desc())
            .limit(limit)
            .all()
        )
