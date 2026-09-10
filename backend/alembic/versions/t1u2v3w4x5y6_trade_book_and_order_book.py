"""Trade Book durable table + Order Book enhancements.

Revision ID: t1u2v3w4x5y6
Revises: s7t8u9v0w1x2
Create Date: 2026-09-10 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "t1u2v3w4x5y6"
down_revision: str | Sequence[str] | None = "s7t8u9v0w1x2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "trade_executions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("exec_id", sa.String(), nullable=False),
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("ibkr_account", sa.String(), nullable=False),
        sa.Column("order_id", sa.BigInteger(), nullable=True),
        sa.Column("symbol", sa.String(), nullable=False),
        sa.Column("sec_type", sa.String(), nullable=False),
        sa.Column("exchange", sa.String(), nullable=False),
        sa.Column("currency", sa.String(), nullable=False, server_default="USD"),
        sa.Column("con_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("side", sa.String(), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("price", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("cum_qty", sa.Numeric(precision=18, scale=8), nullable=False, server_default="0"),
        sa.Column("avg_price", sa.Numeric(precision=18, scale=8), nullable=False, server_default="0"),
        sa.Column("broker_order_id", sa.String(), nullable=True),
        sa.Column("perm_id", sa.BigInteger(), nullable=True),
        sa.Column("client_id", sa.Integer(), nullable=True),
        sa.Column("commission", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column("commission_currency", sa.String(), nullable=True),
        sa.Column("realized_pnl", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("correction_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"]),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("exec_id", name="uq_trade_executions_exec_id"),
    )
    op.create_index("ix_trade_executions_account_id", "trade_executions", ["account_id"], unique=False)
    op.create_index("ix_trade_executions_order_id", "trade_executions", ["order_id"], unique=False)
    op.create_index("ix_trade_executions_broker_order_id", "trade_executions", ["broker_order_id"], unique=False)
    op.create_index("ix_trade_executions_account_executed_at", "trade_executions", ["account_id", "executed_at"], unique=False)
    op.create_index("ix_trade_executions_account_symbol", "trade_executions", ["account_id", "symbol"], unique=False)

    # Order Book enhancements - nullable, additive
    op.add_column("orders", sa.Column("sec_type", sa.String(), nullable=True))
    op.add_column("orders", sa.Column("exchange", sa.String(), nullable=True))
    op.add_column("orders", sa.Column("currency", sa.String(), nullable=True))
    op.add_column("orders", sa.Column("order_type", sa.String(), nullable=True, server_default="LIMIT"))
    op.add_column("orders", sa.Column("perm_id", sa.BigInteger(), nullable=True))
    op.add_column("orders", sa.Column("avg_fill_price", sa.Numeric(precision=18, scale=8), nullable=True))
    op.add_column("orders", sa.Column("remaining_qty", sa.Numeric(precision=18, scale=4), nullable=True))
    op.add_column("orders", sa.Column("outside_rth", sa.Boolean(), nullable=True))
    op.add_column("orders", sa.Column("time_in_force", sa.String(), nullable=True))
    op.add_column("orders", sa.Column("rejection_reason", sa.String(), nullable=True))
    op.add_column("orders", sa.Column("cancel_reason", sa.String(), nullable=True))
    op.create_index("ix_orders_account_updated_at", "orders", ["account_id", "updated_at"], unique=False)
    op.create_index("ix_orders_symbol", "orders", ["symbol"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_orders_symbol", table_name="orders")
    op.drop_index("ix_orders_account_updated_at", table_name="orders")
    op.drop_column("orders", "cancel_reason")
    op.drop_column("orders", "rejection_reason")
    op.drop_column("orders", "time_in_force")
    op.drop_column("orders", "outside_rth")
    op.drop_column("orders", "remaining_qty")
    op.drop_column("orders", "avg_fill_price")
    op.drop_column("orders", "perm_id")
    op.drop_column("orders", "order_type")
    op.drop_column("orders", "currency")
    op.drop_column("orders", "exchange")
    op.drop_column("orders", "sec_type")
    op.drop_index("ix_trade_executions_account_symbol", table_name="trade_executions")
    op.drop_index("ix_trade_executions_account_executed_at", table_name="trade_executions")
    op.drop_index("ix_trade_executions_broker_order_id", table_name="trade_executions")
    op.drop_index("ix_trade_executions_order_id", table_name="trade_executions")
    op.drop_index("ix_trade_executions_account_id", table_name="trade_executions")
    op.drop_table("trade_executions")
