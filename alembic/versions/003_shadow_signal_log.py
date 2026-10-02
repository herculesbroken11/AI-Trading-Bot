"""Add shadow_signal_log table (shadow mode, observation only — never orders).

Revision ID: 003_shadow_signal_log
Revises: 002_orders_limit_price
Create Date: 2026-10-02
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "003_shadow_signal_log"
down_revision: Union[str, None] = "002_orders_limit_price"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "shadow_signal_log"


def upgrade() -> None:
    bind = op.get_bind()
    if TABLE in sa.inspect(bind).get_table_names():
        return
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("cycle_number", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("decision", sa.String(), nullable=False),
        sa.Column("selected_symbol", sa.String(), nullable=True),
        sa.Column("confidence_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("bullish_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("bearish_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("skip_reason", sa.String(), nullable=True),
        sa.Column("market_regime", sa.String(), nullable=True),
        sa.Column("explanation", sa.Text(), nullable=True),
        sa.Column("quote_age_tna", sa.Float(), nullable=True),
        sa.Column("quote_age_tza", sa.Float(), nullable=True),
        sa.Column("quote_age_iwm", sa.Float(), nullable=True),
        sa.Column("quote_age_spy", sa.Float(), nullable=True),
        sa.Column("quote_age_qqq", sa.Float(), nullable=True),
        sa.Column("tna_mid", sa.Float(), nullable=True),
        sa.Column("tza_mid", sa.Float(), nullable=True),
        sa.Column("iwm_mid", sa.Float(), nullable=True),
        sa.Column("spy_mid", sa.Float(), nullable=True),
        sa.Column("qqq_mid", sa.Float(), nullable=True),
        sa.Column("vix_last", sa.Float(), nullable=True),
        sa.Column("freshness_gate_passed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("raw_snapshot_json", sa.Text(), nullable=True),
        sa.Column("raw_score_json", sa.Text(), nullable=True),
        sa.Column("submitted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("production_execution_blocked", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("followup_seconds", sa.Float(), nullable=True),
        sa.Column("tna_mid_after", sa.Float(), nullable=True),
        sa.Column("tza_mid_after", sa.Float(), nullable=True),
        sa.Column("iwm_mid_after", sa.Float(), nullable=True),
        sa.Column("spy_mid_after", sa.Float(), nullable=True),
        sa.Column("qqq_mid_after", sa.Float(), nullable=True),
        sa.Column("selected_symbol_move_pct", sa.Float(), nullable=True),
        sa.Column("iwm_move_pct", sa.Float(), nullable=True),
        sa.Column("direction_was_correct", sa.Boolean(), nullable=True),
        sa.Column("outcome_note", sa.Text(), nullable=True),
        sa.CheckConstraint("NOT submitted", name="ck_shadow_signal_log_never_submitted"),
        sa.CheckConstraint("production_execution_blocked", name="ck_shadow_signal_log_production_blocked"),
    )
    op.create_index("ix_shadow_signal_log_id", TABLE, ["id"])
    op.create_index("ix_shadow_signal_log_run_id", TABLE, ["run_id"])
    op.create_index("ix_shadow_signal_log_created_at", TABLE, ["created_at"])
    op.create_index("ix_shadow_signal_log_skip_reason", TABLE, ["skip_reason"])


def downgrade() -> None:
    bind = op.get_bind()
    if TABLE in sa.inspect(bind).get_table_names():
        op.drop_table(TABLE)
