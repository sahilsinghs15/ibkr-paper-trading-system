"""Per-account open position cap on allocations.

Revision ID: b2d8f4a1c903
Revises: a9c4e6f8b013
Create Date: 2026-08-19 15:30:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b2d8f4a1c903"
down_revision: Union[str, Sequence[str], None] = "a9c4e6f8b013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add max_open_positions to allocations, backfilled from strategies."""
    op.add_column(
        "allocations",
        sa.Column("max_open_positions", sa.Integer(), nullable=True),
    )
    op.execute(
        """
        UPDATE allocations AS a
        SET max_open_positions = s.max_open_positions
        FROM strategies AS s
        WHERE a.strategy_id = s.strategy_id
        """
    )
    op.alter_column("allocations", "max_open_positions", nullable=False)


def downgrade() -> None:
    op.drop_column("allocations", "max_open_positions")
