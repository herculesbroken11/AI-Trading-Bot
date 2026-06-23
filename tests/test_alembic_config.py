"""Alembic configuration and migration metadata tests."""

from pathlib import Path

import sqlalchemy as sa

REQUIRED_TABLES = {
    "orders",
    "decision_log",
    "error_events",
    "bot_state",
    "account_snapshots",
}


def test_alembic_revision_file_exists():
    path = Path("alembic/versions/001_phase2_logging_tables.py")
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert 'revision: str = "001_phase2_logging"' in text


def test_target_metadata_includes_required_tables():
    import backend.database  # noqa: F401
    import backend.db.models  # noqa: F401
    from backend.db.base import Base

    table_names = set(Base.metadata.tables.keys())
    assert REQUIRED_TABLES.issubset(table_names)


def test_migration_upgrade_is_non_destructive():
    path = Path("alembic/versions/001_phase2_logging_tables.py")
    text = path.read_text(encoding="utf-8")
    assert "drop_table" not in text
    assert '"orders" not in existing' in text


def test_alembic_env_uses_settings_not_hardcoded_url():
    text = Path("alembic/env.py").read_text(encoding="utf-8")
    assert "load_settings" in text
    assert "settings.database_url" in text
    assert "print(" not in text
