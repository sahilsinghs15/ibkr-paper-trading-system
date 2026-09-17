"""Add cancel_exposure to accounts.

Revision ID: c9d0e1f2a3b4
Revises: b2c3d4e5f6g7
Create Date: 2026-05-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c9d0e1f2a3b4"
down_revision: str | Sequence[str] | None = "b2c3d4e5f6g7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column(
            "cancel_exposure",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            default=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("accounts", "cancel_exposure")
