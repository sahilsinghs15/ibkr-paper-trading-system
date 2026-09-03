"""Default margin_settings.check_enabled to true.

Revision ID: k5l6m7n8o9p0
Revises: j4k5l6m7n8o9
Create Date: 2026-09-03 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "k5l6m7n8o9p0"
down_revision: str | Sequence[str] | None = "j4k5l6m7n8o9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "margin_settings",
        "check_enabled",
        existing_type=sa.Boolean(),
        existing_nullable=False,
        server_default=sa.true(),
    )
    op.execute(sa.text("UPDATE margin_settings SET check_enabled = true WHERE id = 1"))


def downgrade() -> None:
    op.execute(sa.text("UPDATE margin_settings SET check_enabled = false WHERE id = 1"))
    op.alter_column(
        "margin_settings",
        "check_enabled",
        existing_type=sa.Boolean(),
        existing_nullable=False,
        server_default=sa.false(),
    )
