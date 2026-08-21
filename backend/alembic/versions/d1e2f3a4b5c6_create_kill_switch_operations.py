"""Create kill_switch_operations table.

Revision ID: d1e2f3a4b5c6
Revises: c9a1b2c3d4e5
Create Date: 2026-08-21 19:35:00.000000
"""

from typing import Sequence, Union
import alembic.op as op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "d1e2f3a4b5c6"
down_revision: Union[str, None] = "c9a1b2c3d4e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "kill_switch_operations",
        sa.Column(
            "operation_id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
            primary_key=True,
        ),
        sa.Column("account_id", sa.BigInteger(), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("ibkr_account", sa.String(), nullable=False),
        sa.Column("status", sa.String(), server_default="ACTIVATING", nullable=False),
        sa.Column("requested_by", sa.String(), server_default="operator", nullable=False),
        sa.Column("initial_position_count", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("flattened_count", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("working_count", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("retrying_count", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("unresolved_count", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("final_exposure", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_kill_switch_operations_account_id", "kill_switch_operations", ["account_id"])
    op.create_index("ix_kill_switch_operations_status", "kill_switch_operations", ["status"])


def downgrade() -> None:
    op.drop_index("ix_kill_switch_operations_status", table_name="kill_switch_operations")
    op.drop_index("ix_kill_switch_operations_account_id", table_name="kill_switch_operations")
    op.drop_table("kill_switch_operations")
