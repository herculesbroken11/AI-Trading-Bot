"""SQLAlchemy engine and session factory."""

from __future__ import annotations

from typing import Generator, Optional

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

_engine: Optional[Engine] = None
_SessionLocal: Optional[sessionmaker] = None


def configure_engine(database_url: str, *, sql_echo: bool = False) -> None:
    global _engine, _SessionLocal
    _engine = create_engine(
        database_url,
        echo=sql_echo,
        pool_pre_ping=True,
    )
    _SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


def get_engine() -> Engine:
    if _engine is None:
        from backend.config.settings import get_settings

        settings = get_settings()
        configure_engine(settings.database_url, sql_echo=settings.sql_echo)
    return _engine


def get_session_local() -> sessionmaker:
    if _SessionLocal is None:
        get_engine()
    assert _SessionLocal is not None
    return _SessionLocal


def get_db_session() -> Session:
    return get_session_local()()


def get_db() -> Generator[Session, None, None]:
    db = get_db_session()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create tables if they do not exist (dev/test only — use Alembic in production)."""
    from backend.db.base import Base
    import backend.database  # noqa: F401 — legacy models on shared Base
    import backend.db.models  # noqa: F401 — Phase 2 logging models

    get_engine()
    Base.metadata.create_all(bind=get_engine())
