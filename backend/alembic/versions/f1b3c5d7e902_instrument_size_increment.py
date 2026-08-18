"""Add optional IBKR size_increment on instruments master.

Revision ID: f1b3c5d7e902
Revises: e8a2c4d6f901
Create Date: 2026-08-18 20:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f1b3c5d7e902"
down_revision: Union[str, Sequence[str], None] = "e8a2c4d6f901"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "instruments",
        sa.Column("size_increment", sa.Numeric(precision=18, scale=8), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("instruments", "size_increment")
