"""Add perm_id to manual_orders (M1-C).

Revision ID: x5y6z7a8b9c0
Revises: w4x5y6z7a8b9
Create Date: 2026-09-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "x5y6z7a8b9c0"
down_revision: str | Sequence[str] | None = "w4x5y6z7a8b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("manual_orders", sa.Column("perm_id", sa.BigInteger(), nullable=True))
    op.create_index("ix_manual_orders_account_perm_id", "manual_orders", ["account_id", "perm_id"])


def downgrade() -> None:
    op.drop_index("ix_manual_orders_account_perm_id", table_name="manual_orders")
    op.drop_column("manual_orders", "perm_id")
