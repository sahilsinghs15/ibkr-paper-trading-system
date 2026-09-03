"""Uppercase positions.leg_*_symbol and per_symbol_limits.symbol.

Revision ID: i3j4k5l6m7n8
Revises: h2i3j4k5l6m7
Create Date: 2026-09-03 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op


revision: str = "i3j4k5l6m7n8"
down_revision: Union[str, Sequence[str], None] = "h2i3j4k5l6m7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Fold mixed-case symbols to the canonical uppercase form (M27/M29/M42)."""
    op.execute(
        """
        DELETE FROM per_symbol_limits a
        USING per_symbol_limits b
        WHERE a.account_id = b.account_id
          AND UPPER(a.symbol) = UPPER(b.symbol)
          AND a.ctid > b.ctid
        """
    )
    op.execute(
        """
        UPDATE per_symbol_limits
        SET symbol = UPPER(symbol)
        WHERE symbol <> UPPER(symbol)
        """
    )
    op.execute(
        """
        UPDATE positions
        SET leg_a_symbol = UPPER(leg_a_symbol)
        WHERE leg_a_symbol IS NOT NULL
          AND leg_a_symbol <> UPPER(leg_a_symbol)
        """
    )
    op.execute(
        """
        UPDATE positions
        SET leg_b_symbol = UPPER(leg_b_symbol)
        WHERE leg_b_symbol IS NOT NULL
          AND leg_b_symbol <> UPPER(leg_b_symbol)
        """
    )


def downgrade() -> None:
    """Cannot restore original mixed-case symbols."""
    pass
