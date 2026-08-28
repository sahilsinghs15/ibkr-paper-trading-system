"""Add basket critical recovery columns.

Revision ID: a1b2c3d4e567
Revises: f4a8c2d1e903
Create Date: 2026-08-27 15:30:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e567"
down_revision: Union[str, Sequence[str], None] = "f4a8c2d1e903"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("baskets", sa.Column("recovery_status", sa.String(), nullable=True))
    op.add_column("baskets", sa.Column("recovery_detail", sa.String(), nullable=True))
    op.add_column(
        "baskets",
        sa.Column("recovered_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("baskets", "recovered_at")
    op.drop_column("baskets", "recovery_detail")
    op.drop_column("baskets", "recovery_status")
