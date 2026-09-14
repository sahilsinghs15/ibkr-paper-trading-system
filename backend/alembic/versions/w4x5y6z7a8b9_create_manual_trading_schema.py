"""Create manual trading schema (M0.5).

Revision ID: w4x5y6z7a8b9
Revises: v3w4x5y6z7a8
Create Date: 2026-09-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "w4x5y6z7a8b9"
down_revision: str | Sequence[str] | None = "v3w4x5y6z7a8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. manual_orders
    op.create_table(
        "manual_orders",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("ibkr_account", sa.String(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("internal_order_id", sa.String(length=64), nullable=False),
        sa.Column("trade_id", sa.String(length=64), nullable=False),
        sa.Column("broker_order_id", sa.String(length=64), nullable=True),
        sa.Column("con_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("sec_type", sa.String(length=16), nullable=False, server_default="STK"),
        sa.Column("exchange", sa.String(length=32), nullable=False, server_default="SMART"),
        sa.Column("currency", sa.String(length=8), nullable=False, server_default="USD"),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("order_type", sa.String(length=16), nullable=False),
        sa.Column("limit_price", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column("stop_price", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column("tif", sa.String(length=16), nullable=False, server_default="DAY"),
        sa.Column("outside_rth", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="PENDING_SUBMIT"),
        sa.Column("source", sa.String(length=16), nullable=False, server_default="manual"),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("reject_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("internal_order_id", name="uq_manual_orders_internal_order_id"),
        sa.UniqueConstraint("account_id", "idempotency_key", name="uq_manual_orders_account_idempotency"),
    )
    op.create_index("ix_manual_orders_account_id", "manual_orders", ["account_id"], unique=False)
    op.create_index("ix_manual_orders_internal_order_id", "manual_orders", ["internal_order_id"], unique=True)
    op.create_index("ix_manual_orders_trade_id", "manual_orders", ["trade_id"], unique=False)
    op.create_index("ix_manual_orders_broker_order_id", "manual_orders", ["broker_order_id"], unique=False)
    op.create_index("ix_manual_orders_symbol", "manual_orders", ["symbol"], unique=False)
    op.create_index("ix_manual_orders_status", "manual_orders", ["status"], unique=False)
    op.create_index("ix_manual_orders_account_status", "manual_orders", ["account_id", "status"], unique=False)
    op.create_index("ix_manual_orders_account_created_at", "manual_orders", ["account_id", "created_at"], unique=False)

    # 2. manual_executions
    op.create_table(
        "manual_executions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("manual_order_id", sa.BigInteger(), nullable=False),
        sa.Column("exec_id", sa.String(length=128), nullable=False),
        sa.Column("broker_order_id", sa.String(length=64), nullable=True),
        sa.Column("quantity", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("price", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("commission", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column("commission_currency", sa.String(length=8), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False, server_default="manual"),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["manual_order_id"], ["manual_orders.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("exec_id", name="uq_manual_executions_exec_id"),
    )
    op.create_index("ix_manual_executions_manual_order_id", "manual_executions", ["manual_order_id"], unique=False)
    op.create_index("ix_manual_executions_executed_at", "manual_executions", ["executed_at"], unique=False)

    # 3. manual_positions
    op.create_table(
        "manual_positions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("trade_id", sa.String(length=64), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("con_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("sec_type", sa.String(length=16), nullable=False, server_default="STK"),
        sa.Column("currency", sa.String(length=8), nullable=False, server_default="USD"),
        sa.Column("signed_qty", sa.Numeric(precision=18, scale=4), nullable=False, server_default="0"),
        sa.Column("avg_cost", sa.Numeric(precision=18, scale=8), nullable=False, server_default="0"),
        sa.Column("realized_pnl", sa.Numeric(precision=18, scale=8), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="OPEN"),
        sa.Column("source", sa.String(length=16), nullable=False, server_default="manual"),
        sa.Column("opened_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "trade_id", name="uq_manual_positions_account_trade"),
    )
    op.create_index("ix_manual_positions_account_id", "manual_positions", ["account_id"], unique=False)
    op.create_index("ix_manual_positions_trade_id", "manual_positions", ["trade_id"], unique=False)
    op.create_index("ix_manual_positions_symbol", "manual_positions", ["symbol"], unique=False)
    op.create_index("ix_manual_positions_status", "manual_positions", ["status"], unique=False)
    op.create_index("ix_manual_positions_account_status", "manual_positions", ["account_id", "status"], unique=False)
    op.create_index("ix_manual_positions_account_symbol", "manual_positions", ["account_id", "symbol"], unique=False)

    # 4. manual_halt_state
    op.create_table(
        "manual_halt_state",
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("halted", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("halted_by", sa.String(length=64), nullable=True),
        sa.Column("halted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"]),
        sa.PrimaryKeyConstraint("account_id"),
    )

    # 5. manual_audit_events
    op.create_table(
        "manual_audit_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_manual_audit_events_account_id", "manual_audit_events", ["account_id"], unique=False)
    op.create_index("ix_manual_audit_events_action", "manual_audit_events", ["action"], unique=False)
    op.create_index("ix_manual_audit_events_account_action", "manual_audit_events", ["account_id", "action"], unique=False)
    op.create_index("ix_manual_audit_events_created_at", "manual_audit_events", ["created_at"], unique=False)


def downgrade() -> None:
    op.drop_table("manual_audit_events")
    op.drop_table("manual_halt_state")
    op.drop_table("manual_positions")
    op.drop_table("manual_executions")
    op.drop_table("manual_orders")
