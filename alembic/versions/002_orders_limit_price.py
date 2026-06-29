"""Add limit_price to orders table.

Revision ID: 002_orders_limit_price
Revises: 001_phase2_logging
Create Date: 2026-06-23
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "002_orders_limit_price"
down_revision: Union[str, None] = "001_phase2_logging"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "orders" not in inspector.get_table_names():
        return
    columns = {col["name"] for col in inspector.get_columns("orders")}
    if "limit_price" not in columns:
        op.add_column("orders", sa.Column("limit_price", sa.Float(), nullable=True))


def downgrade() -> None:
    pass
