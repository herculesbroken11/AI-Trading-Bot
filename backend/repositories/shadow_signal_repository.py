"""Shadow-mode signal log repository (observation only — never orders)."""

from __future__ import annotations

from typing import List, Optional, Sequence

from sqlalchemy import inspect
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from backend.db.models import ShadowSignalLog
from backend.shadow_mode.models import TRACKED_SYMBOLS, FollowupOutcome, ShadowCycleRecord


class ShadowTableMissingError(RuntimeError):
    pass


def safe_database_label(database_url: str) -> str:
    """Database URL with the password hidden (never print credentials)."""
    try:
        return make_url(database_url).render_as_string(hide_password=True)
    except Exception:
        return "configured (unparseable URL hidden)"


def open_shadow_repository(database_url: str, *, create_table: bool, sql_echo: bool = False) -> "ShadowSignalRepository":
    """
    create_table=True creates only shadow_signal_log if missing (non-destructive;
    migration 003_shadow_signal_log skips existing tables). False raises
    ShadowTableMissingError instead.
    """
    from backend.db.session import configure_engine, get_db_session, get_engine

    configure_engine(database_url, sql_echo=sql_echo)
    engine = get_engine()
    if ShadowSignalLog.__tablename__ not in inspect(engine).get_table_names():
        if not create_table:
            raise ShadowTableMissingError(
                "shadow_signal_log table not found; run: alembic upgrade head"
            )
        ShadowSignalLog.__table__.create(bind=engine, checkfirst=True)
    return ShadowSignalRepository(get_db_session())


class ShadowSignalRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def log_signal(self, record: ShadowCycleRecord) -> ShadowSignalLog:
        record.assert_safe()
        row = ShadowSignalLog(
            run_id=record.run_id,
            cycle_number=record.cycle_number,
            created_at=record.created_at.replace(tzinfo=None),
            decision=record.decision,
            selected_symbol=record.selected_symbol,
            confidence_score=record.confidence_score,
            bullish_score=record.bullish_score,
            bearish_score=record.bearish_score,
            skip_reason=record.skip_reason,
            market_regime=record.market_regime,
            explanation=record.explanation,
            vix_last=record.vix_last,
            freshness_gate_passed=record.freshness_gate_passed,
            raw_snapshot_json=record.raw_snapshot_json,
            raw_score_json=record.raw_score_json,
            submitted=False,
            production_execution_blocked=True,
        )
        for symbol in TRACKED_SYMBOLS:
            setattr(row, f"quote_age_{symbol.lower()}", record.quote_ages.get(symbol))
            setattr(row, f"{symbol.lower()}_mid", record.mids.get(symbol))
        self._session.add(row)
        self._session.commit()
        self._session.refresh(row)
        record.db_id = row.id
        return row

    def update_followup(self, log_id: int, outcome: FollowupOutcome) -> Optional[ShadowSignalLog]:
        row = self._session.get(ShadowSignalLog, log_id)
        if row is None:
            return None
        row.followup_seconds = outcome.followup_seconds
        for symbol in TRACKED_SYMBOLS:
            setattr(row, f"{symbol.lower()}_mid_after", outcome.mids_after.get(symbol))
        row.selected_symbol_move_pct = outcome.selected_symbol_move_pct
        row.iwm_move_pct = outcome.iwm_move_pct
        row.direction_was_correct = outcome.direction_was_correct
        row.outcome_note = outcome.outcome_note
        row.submitted = False
        row.production_execution_blocked = True
        self._session.commit()
        self._session.refresh(row)
        return row

    def list_signals(
        self,
        *,
        run_id: Optional[str] = None,
        run_ids: Optional[Sequence[str]] = None,
        limit: int = 100,
    ) -> List[ShadowSignalLog]:
        query = self._session.query(ShadowSignalLog)
        if run_id:
            query = query.filter(ShadowSignalLog.run_id == run_id)
        if run_ids:
            query = query.filter(ShadowSignalLog.run_id.in_(list(run_ids)))
        return (
            query.order_by(ShadowSignalLog.created_at.desc(), ShadowSignalLog.id.desc())
            .limit(max(1, int(limit)))
            .all()
        )
