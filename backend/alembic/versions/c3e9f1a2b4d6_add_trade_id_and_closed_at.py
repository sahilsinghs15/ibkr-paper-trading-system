"""add trade_id closed_at and fractional qty

Revision ID: c3e9f1a2b4d6
Revises: af6ded376ee5
Create Date: 2026-08-18 10:40:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c3e9f1a2b4d6"
down_revision: Union[str, Sequence[str], None] = "af6ded376ee5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add Model Blue persistence columns without dropping existing schema."""
    op.add_column("signals", sa.Column("trade_id", sa.String(), nullable=True))
    op.create_index("ix_signals_trade_id", "signals", ["trade_id"], unique=False)

    op.add_column("orders", sa.Column("trade_id", sa.String(), nullable=True))
    op.add_column("orders", sa.Column("internal_order_id", sa.String(), nullable=True))
    op.create_index("ix_orders_trade_id", "orders", ["trade_id"], unique=False)
    op.create_index("ix_orders_internal_order_id", "orders", ["internal_order_id"], unique=True)

    op.alter_column(
        "orders",
        "quantity",
        existing_type=sa.Integer(),
        type_=sa.Numeric(18, 4),
        existing_nullable=False,
    )
    op.alter_column(
        "orders",
        "fill_qty",
        existing_type=sa.Integer(),
        type_=sa.Numeric(18, 4),
        existing_nullable=True,
    )

    op.add_column("positions", sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True))
    op.alter_column(
        "positions",
        "leg_a_signed_qty",
        existing_type=sa.Integer(),
        type_=sa.Numeric(18, 4),
        existing_nullable=False,
    )
    op.alter_column(
        "positions",
        "leg_b_signed_qty",
        existing_type=sa.Integer(),
        type_=sa.Numeric(18, 4),
        existing_nullable=True,
    )


def downgrade() -> None:
    """Reverse Model Blue persistence columns."""
    op.alter_column(
        "positions",
        "leg_b_signed_qty",
        existing_type=sa.Numeric(18, 4),
        type_=sa.Integer(),
        existing_nullable=True,
    )
    op.alter_column(
        "positions",
        "leg_a_signed_qty",
        existing_type=sa.Numeric(18, 4),
        type_=sa.Integer(),
        existing_nullable=False,
    )
    op.drop_column("positions", "closed_at")

    op.alter_column(
        "orders",
        "fill_qty",
        existing_type=sa.Numeric(18, 4),
        type_=sa.Integer(),
        existing_nullable=True,
    )
    op.alter_column(
        "orders",
        "quantity",
        existing_type=sa.Numeric(18, 4),
        type_=sa.Integer(),
        existing_nullable=False,
    )
    op.drop_index("ix_orders_internal_order_id", table_name="orders")
    op.drop_index("ix_orders_trade_id", table_name="orders")
    op.drop_column("orders", "internal_order_id")
    op.drop_column("orders", "trade_id")

    op.drop_index("ix_signals_trade_id", table_name="signals")
    op.drop_column("signals", "trade_id")
