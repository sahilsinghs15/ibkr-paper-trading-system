"""Add live_pnl to manual_positions for same IBKR mark source as engine.

Revision ID: z7a8b9c0d1e2
Revises: y6z7a8b9c0d1
Create Date: 2026-09-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "z7a8b9c0d1e2"
down_revision: str | Sequence[str] | None = "y6z7a8b9c0d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("manual_positions", sa.Column("live_pnl", sa.Numeric(precision=18, scale=8), nullable=True))


def downgrade() -> None:
    op.drop_column("manual_positions", "live_pnl")
