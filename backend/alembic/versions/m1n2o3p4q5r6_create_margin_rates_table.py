"""Directional IBKR margin-rate cache.

Revision ID: m1n2o3p4q5r6
Revises: g1h2i3j4k5l6
Create Date: 2026-09-02 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "m1n2o3p4q5r6"
down_revision: Union[str, Sequence[str], None] = "g1h2i3j4k5l6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "margin_rates",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("symbol", sa.String(), nullable=False),
        sa.Column("instrument_type", sa.String(), nullable=False),
        sa.Column("side", sa.String(), nullable=False),
        sa.Column("rate", sa.Numeric(9, 6), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("probe_notional", sa.Numeric(18, 4), nullable=False),
        sa.Column("init_margin_change", sa.Numeric(18, 4), nullable=False),
        sa.Column(
            "scanned_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("rate > 0 AND rate <= 1", name="ck_margin_rates_rate_range"),
        sa.CheckConstraint("side IN ('BUY', 'SELL')", name="ck_margin_rates_side"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "symbol",
            "instrument_type",
            "side",
            name="uq_margin_rates_symbol_type_side",
        ),
    )


def downgrade() -> None:
    op.drop_table("margin_rates")
