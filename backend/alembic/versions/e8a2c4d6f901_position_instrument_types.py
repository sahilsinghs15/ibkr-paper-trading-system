"""Add per-leg instrument_type on Model Blue pair positions.

Revision ID: e8a2c4d6f901
Revises: b7c4e8a1d902
Create Date: 2026-08-18 17:30:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e8a2c4d6f901"
down_revision: Union[str, Sequence[str], None] = "b7c4e8a1d902"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "positions",
        sa.Column(
            "leg_a_instrument_type",
            sa.String(),
            nullable=False,
            server_default="STK",
        ),
    )
    op.add_column(
        "positions",
        sa.Column("leg_b_instrument_type", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("positions", "leg_b_instrument_type")
    op.drop_column("positions", "leg_a_instrument_type")
