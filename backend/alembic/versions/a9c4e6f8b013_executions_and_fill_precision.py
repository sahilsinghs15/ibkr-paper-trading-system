"""Broker executions table, fill-price precision, event idempotency.

Revision ID: a9c4e6f8b013
Revises: f1b3c5d7e902
Create Date: 2026-08-18 21:00:00.000000

Existing trading rows are not rewritten. New executions rows start empty.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a9c4e6f8b013"
down_revision: Union[str, Sequence[str], None] = "f1b3c5d7e902"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "orders",
        "fill_price",
        existing_type=sa.Numeric(18, 4),
        type_=sa.Numeric(18, 8),
        existing_nullable=True,
    )
    op.alter_column(
        "positions",
        "leg_a_entry_mark",
        existing_type=sa.Numeric(18, 4),
        type_=sa.Numeric(18, 8),
        existing_nullable=False,
    )
    op.alter_column(
        "positions",
        "leg_b_entry_mark",
        existing_type=sa.Numeric(18, 4),
        type_=sa.Numeric(18, 8),
        existing_nullable=True,
    )
    op.alter_column(
        "positions",
        "realised_pnl",
        existing_type=sa.Numeric(18, 4),
        type_=sa.Numeric(18, 8),
        existing_nullable=False,
    )
    op.alter_column(
        "positions",
        "commission",
        existing_type=sa.Numeric(18, 4),
        type_=sa.Numeric(18, 8),
        existing_nullable=False,
    )
    op.alter_column(
        "positions",
        "live_pnl",
        existing_type=sa.Numeric(18, 4),
        type_=sa.Numeric(18, 8),
        existing_nullable=False,
    )

    op.add_column(
        "event_log",
        sa.Column("basket_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "event_log",
        sa.Column("idempotency_key", sa.String(), nullable=True),
    )
    op.create_foreign_key(
        "fk_event_log_basket_id",
        "event_log",
        "baskets",
        ["basket_id"],
        ["id"],
    )
    op.create_index("ix_event_log_idempotency_key", "event_log", ["idempotency_key"], unique=True)

    op.create_table(
        "executions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("exec_id", sa.String(), nullable=False),
        sa.Column("order_id", sa.BigInteger(), nullable=True),
        sa.Column("account_id", sa.BigInteger(), nullable=True),
        sa.Column("internal_order_id", sa.String(), nullable=False),
        sa.Column("broker_order_id", sa.String(), nullable=True),
        sa.Column("symbol", sa.String(), nullable=False),
        sa.Column("side", sa.String(), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("price", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("commission", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column("commission_currency", sa.String(), nullable=True),
        sa.Column("realized_pnl", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column("perm_id", sa.BigInteger(), nullable=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("exec_id", name="uq_executions_exec_id"),
    )
    op.create_index("ix_executions_internal_order_id", "executions", ["internal_order_id"])
    op.create_index("ix_executions_order_id", "executions", ["order_id"])


def downgrade() -> None:
    op.drop_index("ix_executions_order_id", table_name="executions")
    op.drop_index("ix_executions_internal_order_id", table_name="executions")
    op.drop_table("executions")
    op.drop_index("ix_event_log_idempotency_key", table_name="event_log")
    op.drop_constraint("fk_event_log_basket_id", "event_log", type_="foreign_key")
    op.drop_column("event_log", "idempotency_key")
    op.drop_column("event_log", "basket_id")
    op.alter_column(
        "orders",
        "fill_price",
        existing_type=sa.Numeric(18, 8),
        type_=sa.Numeric(18, 4),
        existing_nullable=True,
    )
    op.alter_column(
        "positions",
        "leg_a_entry_mark",
        existing_type=sa.Numeric(18, 8),
        type_=sa.Numeric(18, 4),
        existing_nullable=False,
    )
    op.alter_column(
        "positions",
        "leg_b_entry_mark",
        existing_type=sa.Numeric(18, 8),
        type_=sa.Numeric(18, 4),
        existing_nullable=True,
    )
    op.alter_column(
        "positions",
        "realised_pnl",
        existing_type=sa.Numeric(18, 8),
        type_=sa.Numeric(18, 4),
        existing_nullable=False,
    )
    op.alter_column(
        "positions",
        "commission",
        existing_type=sa.Numeric(18, 8),
        type_=sa.Numeric(18, 4),
        existing_nullable=False,
    )
    op.alter_column(
        "positions",
        "live_pnl",
        existing_type=sa.Numeric(18, 8),
        type_=sa.Numeric(18, 4),
        existing_nullable=False,
    )
