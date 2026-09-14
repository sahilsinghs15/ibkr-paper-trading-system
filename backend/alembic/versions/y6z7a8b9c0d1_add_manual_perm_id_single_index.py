"""Add single-column perm_id index for manual_orders (fix drift).

Revision ID: y6z7a8b9c0d1
Revises: x5y6z7a8b9c0
Create Date: 2026-09-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "y6z7a8b9c0d1"
down_revision: str | Sequence[str] | None = "x5y6z7a8b9c0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Single-column index for perm_id-only lookups (e.g. handle_order_status without account_id)
    # Composite (account_id, perm_id) already exists from x5y6z7a8b9c0
    op.create_index("ix_manual_orders_perm_id", "manual_orders", ["perm_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_manual_orders_perm_id", table_name="manual_orders")
