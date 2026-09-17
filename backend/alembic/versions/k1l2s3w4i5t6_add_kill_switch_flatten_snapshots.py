"""Add kill-switch flatten snapshots for account-flatten attribution.

Revision ID: k1l2s3w4i5t6
Revises: c1d2e3f4a5b6
Create Date: 2026-09-16

Captures immutable ledger snapshot at account-flatten initiation so
reconciliation operates against that set, not whatever is OPEN later.
Also documents FLATTENED_PENDING_PRICE risk_state for positions where
broker is flat but execution price not yet available.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "k1l2s3w4i5t6"
down_revision: Union[str, Sequence[str], None] = "c1d2e3f4a5b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "kill_switch_flatten_snapshots",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("operation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("kill_switch_operations.operation_id", ondelete="CASCADE"), nullable=False),
        sa.Column("account_id", sa.BigInteger(), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("position_type", sa.String(length=16), nullable=False),
        sa.Column("trade_id", sa.String(), nullable=False),
        sa.Column("leg_a_symbol", sa.String(), nullable=True),
        sa.Column("leg_a_signed_qty", sa.Numeric(18, 4), nullable=True),
        sa.Column("leg_a_entry_mark", sa.Numeric(18, 8), nullable=True),
        sa.Column("leg_a_instrument_type", sa.String(), nullable=True),
        sa.Column("leg_b_symbol", sa.String(), nullable=True),
        sa.Column("leg_b_signed_qty", sa.Numeric(18, 4), nullable=True),
        sa.Column("leg_b_entry_mark", sa.Numeric(18, 8), nullable=True),
        sa.Column("leg_b_instrument_type", sa.String(), nullable=True),
        sa.Column("symbol", sa.String(), nullable=True),
        sa.Column("con_id", sa.BigInteger(), nullable=True),
        sa.Column("sec_type", sa.String(), nullable=True),
        sa.Column("signed_qty", sa.Numeric(18, 4), nullable=True),
        sa.Column("avg_cost", sa.Numeric(18, 8), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("snapshot_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_kill_switch_flatten_snapshots_operation_id", "kill_switch_flatten_snapshots", ["operation_id"])
    op.create_index("ix_kill_switch_flatten_snapshots_account_trade", "kill_switch_flatten_snapshots", ["account_id", "trade_id"])


def downgrade() -> None:
    op.drop_index("ix_kill_switch_flatten_snapshots_account_trade", table_name="kill_switch_flatten_snapshots")
    op.drop_index("ix_kill_switch_flatten_snapshots_operation_id", table_name="kill_switch_flatten_snapshots")
    op.drop_table("kill_switch_flatten_snapshots")
