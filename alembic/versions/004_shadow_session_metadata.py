"""Add market session metadata columns to shadow_signal_log (all nullable; old rows untouched).

Revision ID: 004_shadow_session_metadata
Revises: 003_shadow_signal_log
Create Date: 2026-10-03
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "004_shadow_session_metadata"
down_revision: Union[str, None] = "003_shadow_signal_log"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "shadow_signal_log"
INDEX = "ix_shadow_signal_log_session_label"


def _columns():
    return [
        sa.Column("session_label", sa.String(), nullable=True),
        sa.Column("session_is_regular_hours", sa.Boolean(), nullable=True),
        sa.Column("session_is_near_close", sa.Boolean(), nullable=True),
        sa.Column("session_minutes_to_close", sa.Float(), nullable=True),
        sa.Column("session_guard_passed", sa.Boolean(), nullable=True),
        sa.Column("session_guard_reason", sa.Text(), nullable=True),
    ]


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if TABLE not in inspector.get_table_names():
        return
    existing = {col["name"] for col in inspector.get_columns(TABLE)}
    for column in _columns():
        if column.name not in existing:
            op.add_column(TABLE, column)
    indexes = {ix["name"] for ix in inspector.get_indexes(TABLE)}
    if INDEX not in indexes:
        op.create_index(INDEX, TABLE, ["session_label"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if TABLE not in inspector.get_table_names():
        return
    if INDEX in {ix["name"] for ix in inspector.get_indexes(TABLE)}:
        op.drop_index(INDEX, table_name=TABLE)
    existing = {col["name"] for col in inspector.get_columns(TABLE)}
    with op.batch_alter_table(TABLE) as batch:
        for column in reversed(_columns()):
            if column.name in existing:
                batch.drop_column(column.name)
