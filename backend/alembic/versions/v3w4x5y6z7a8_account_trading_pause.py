"""Account trading pause columns.

Revision ID: v3w4x5y6z7a8
Revises: u2v3w4x5y6z7
Create Date: 2026-09-11
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "v3w4x5y6z7a8"
down_revision: str | Sequence[str] | None = "u2v3w4x5y6z7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column("trading_paused", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "accounts",
        sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "accounts",
        sa.Column("paused_by", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("accounts", "paused_by")
    op.drop_column("accounts", "paused_at")
    op.drop_column("accounts", "trading_paused")
