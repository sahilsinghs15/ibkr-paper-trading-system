"""basket atomicity schema

Revision ID: b7c4e8a1d902
Revises: a8f3c1d2e4b5
Create Date: 2026-08-18 16:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b7c4e8a1d902"
down_revision: Union[str, Sequence[str], None] = "a8f3c1d2e4b5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Basket state plus compensation/fill identity on orders."""
    op.create_table(
        "baskets",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("trade_id", sa.String(), nullable=False),
        sa.Column("strategy_id", sa.String(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("intended_leg_count", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
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
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "account_id", "trade_id", "action", name="uq_baskets_account_trade_action"
        ),
    )
    op.create_index("ix_baskets_strategy_state", "baskets", ["strategy_id", "state"])

    op.add_column("orders", sa.Column("basket_id", sa.BigInteger(), nullable=True))
    op.add_column(
        "orders",
        sa.Column(
            "is_compensation",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "orders",
        sa.Column("compensation_of_internal_order_id", sa.String(), nullable=True),
    )
    op.create_foreign_key(
        "fk_orders_basket_id",
        "orders",
        "baskets",
        ["basket_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_orders_basket_id", "orders", type_="foreignkey")
    op.drop_column("orders", "compensation_of_internal_order_id")
    op.drop_column("orders", "is_compensation")
    op.drop_column("orders", "basket_id")
    op.drop_index("ix_baskets_strategy_state", table_name="baskets")
    op.drop_table("baskets")
