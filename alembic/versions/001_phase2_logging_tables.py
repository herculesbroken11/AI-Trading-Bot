"""Phase 2 logging and bot-state tables (non-destructive).

Revision ID: 001_phase2_logging
Revises:
Create Date: 2026-06-23

If tables already exist from init_db(), stamp instead of upgrading:
  alembic stamp 001_phase2_logging
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "001_phase2_logging"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())

    if "orders" not in existing:
        op.create_table(
            "orders",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("order_id", sa.String(), nullable=False),
            sa.Column("broker_order_id", sa.String(), nullable=True),
            sa.Column("mode", sa.String(), nullable=False),
            sa.Column("symbol", sa.String(), nullable=False),
            sa.Column("side", sa.String(), nullable=False),
            sa.Column("quantity", sa.Integer(), nullable=False),
            sa.Column("order_type", sa.String(), nullable=True),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("fill_price", sa.Float(), nullable=True),
            sa.Column("rejection_code", sa.String(), nullable=True),
            sa.Column("message", sa.Text(), nullable=True),
            sa.Column("raw_json", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_orders_id", "orders", ["id"])
        op.create_index("ix_orders_order_id", "orders", ["order_id"], unique=True)
        op.create_index("ix_orders_symbol", "orders", ["symbol"])
        op.create_index("ix_orders_created_at", "orders", ["created_at"])

    if "decision_log" not in existing:
        op.create_table(
            "decision_log",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("decision_type", sa.String(), nullable=False),
            sa.Column("symbol", sa.String(), nullable=True),
            sa.Column("source", sa.String(), nullable=False),
            sa.Column("approved", sa.Boolean(), nullable=True),
            sa.Column("rejection_code", sa.String(), nullable=True),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column("payload_json", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_decision_log_id", "decision_log", ["id"])
        op.create_index("ix_decision_log_symbol", "decision_log", ["symbol"])
        op.create_index("ix_decision_log_created_at", "decision_log", ["created_at"])

    if "error_events" not in existing:
        op.create_table(
            "error_events",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("source", sa.String(), nullable=False),
            sa.Column("message", sa.Text(), nullable=False),
            sa.Column("error_type", sa.String(), nullable=True),
            sa.Column("stack", sa.Text(), nullable=True),
            sa.Column("context_json", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_error_events_id", "error_events", ["id"])
        op.create_index("ix_error_events_source", "error_events", ["source"])
        op.create_index("ix_error_events_created_at", "error_events", ["created_at"])

    if "bot_state" not in existing:
        op.create_table(
            "bot_state",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("running", sa.Boolean(), nullable=True),
            sa.Column("emergency_halt", sa.Boolean(), nullable=True),
            sa.Column("trading_mode", sa.String(), nullable=True),
            sa.Column("active_trade_id", sa.Integer(), nullable=True),
            sa.Column("last_heartbeat_at", sa.DateTime(), nullable=True),
            sa.Column("status_message", sa.Text(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_bot_state_id", "bot_state", ["id"])

    if "account_snapshots" not in existing:
        op.create_table(
            "account_snapshots",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("mode", sa.String(), nullable=False),
            sa.Column("buying_power", sa.Float(), nullable=True),
            sa.Column("cash_available", sa.Float(), nullable=True),
            sa.Column("open_positions_count", sa.Integer(), nullable=True),
            sa.Column("positions_json", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_account_snapshots_id", "account_snapshots", ["id"])
        op.create_index("ix_account_snapshots_created_at", "account_snapshots", ["created_at"])


def downgrade() -> None:
    # Non-destructive policy: downgrade is a no-op in Phase 2.
    pass
