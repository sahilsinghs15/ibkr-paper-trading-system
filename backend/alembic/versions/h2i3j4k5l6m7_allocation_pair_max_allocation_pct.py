"""Per-account-model pair allocation percentage.

Revision ID: h2i3j4k5l6m7
Revises: n2o3p4q5r6s7
Create Date: 2026-09-02 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "h2i3j4k5l6m7"
down_revision: Union[str, Sequence[str], None] = "n2o3p4q5r6s7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


DEFAULT_PAIR_MAX_ALLOCATION_PCT = "0.100000"


def upgrade() -> None:
    """Add allocations.pair_max_allocation_pct, backfilled to 10%."""
    op.add_column(
        "allocations",
        sa.Column("pair_max_allocation_pct", sa.Numeric(9, 6), nullable=True),
    )
    op.execute(
        f"""
        UPDATE allocations
        SET pair_max_allocation_pct = {DEFAULT_PAIR_MAX_ALLOCATION_PCT}
        WHERE pair_max_allocation_pct IS NULL
        """
    )
    op.alter_column(
        "allocations",
        "pair_max_allocation_pct",
        nullable=False,
        server_default=sa.text(DEFAULT_PAIR_MAX_ALLOCATION_PCT),
    )
    op.create_check_constraint(
        "ck_allocations_pair_max_allocation_pct_range",
        "allocations",
        "pair_max_allocation_pct > 0 AND pair_max_allocation_pct <= 1",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_allocations_pair_max_allocation_pct_range",
        "allocations",
        type_="check",
    )
    op.drop_column("allocations", "pair_max_allocation_pct")
