"""Shadow-mode signal logger: in-memory always, database optional. Never orders."""

from __future__ import annotations

import logging
from typing import Any, List, Optional, Protocol

from backend.shadow_mode.models import FollowupOutcome, ShadowCycleRecord

logger = logging.getLogger(__name__)


class ShadowSignalStore(Protocol):
    def log_signal(self, record: ShadowCycleRecord) -> Any: ...

    def update_followup(self, log_id: int, outcome: FollowupOutcome) -> Any: ...


class ShadowSignalLogger:
    """
    Keeps every cycle record in memory and, when a repository is given
    (--with-db), persists it. A DB write failure is recorded as a warning and
    never stops observation.
    """

    def __init__(self, repository: Optional[ShadowSignalStore] = None) -> None:
        self._repository = repository
        self.records: List[ShadowCycleRecord] = []
        self.warnings: List[str] = []

    @property
    def db_enabled(self) -> bool:
        return self._repository is not None

    def log(self, record: ShadowCycleRecord) -> ShadowCycleRecord:
        record.assert_safe()
        self.records.append(record)
        if self._repository is not None:
            try:
                self._repository.log_signal(record)
            except Exception as exc:  # DB problems must not crash observation
                self._warn(f"cycle {record.cycle_number}: DB log failed ({type(exc).__name__})")
        return record

    def record_followup(self, record: ShadowCycleRecord, outcome: FollowupOutcome) -> None:
        record.apply_followup(outcome)
        record.assert_safe()
        if self._repository is not None and record.db_id is not None:
            try:
                self._repository.update_followup(record.db_id, outcome)
            except Exception as exc:
                self._warn(f"cycle {record.cycle_number}: DB follow-up update failed ({type(exc).__name__})")

    def _warn(self, message: str) -> None:
        logger.warning(message)
        self.warnings.append(message)
