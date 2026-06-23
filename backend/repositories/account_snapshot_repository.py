"""Account snapshot persistence (optional)."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from backend.db.models import AccountSnapshot
from backend.utils.redact import redact_dict


class AccountSnapshotRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def record_snapshot(
        self,
        *,
        mode: str,
        buying_power: Optional[float],
        cash_available: Optional[float] = None,
        open_positions_count: int = 0,
        positions: Optional[Dict[str, Any]] = None,
    ) -> AccountSnapshot:
        record = AccountSnapshot(
            mode=mode,
            buying_power=buying_power,
            cash_available=cash_available,
            open_positions_count=open_positions_count,
            positions_json=json.dumps(redact_dict(positions or {})),
            created_at=datetime.utcnow(),
        )
        self._session.add(record)
        self._session.commit()
        self._session.refresh(record)
        return record

    def get_recent_snapshots(self, limit: int = 20) -> List[AccountSnapshot]:
        return (
            self._session.query(AccountSnapshot)
            .order_by(AccountSnapshot.created_at.desc())
            .limit(limit)
            .all()
        )
