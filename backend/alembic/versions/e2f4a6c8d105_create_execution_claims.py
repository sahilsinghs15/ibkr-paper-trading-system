"""Create execution_claims durable dedupe barrier.

Revision ID: e2f4a6c8d105
Revises: d1e2f3a4b5c6
Create Date: 2026-08-22 10:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e2f4a6c8d105"
down_revision: Union[str, Sequence[str], None] = "d1e2f3a4b5c6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "execution_claims",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("dedupe_key", sa.String(), nullable=False),
        sa.Column("account_id", sa.BigInteger(), nullable=True),
        sa.Column("strategy_id", sa.String(), nullable=False),
        sa.Column("signal_id", sa.String(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False, server_default="CLAIMED"),
        sa.Column("attempt_count", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("correlation_id", sa.String(), nullable=True),
        sa.Column("last_note", sa.String(), nullable=True),
        sa.Column(
            "claimed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "uq_execution_claims_dedupe_key",
        "execution_claims",
        ["dedupe_key"],
        unique=True,
    )
    op.create_index("ix_execution_claims_signal_id", "execution_claims", ["signal_id"])
    # Supports the reconciliation sweep over stale held claims.
    op.create_index(
        "ix_execution_claims_state_claimed_at",
        "execution_claims",
        ["state", "claimed_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_execution_claims_state_claimed_at", table_name="execution_claims")
    op.drop_index("ix_execution_claims_signal_id", table_name="execution_claims")
    op.drop_index("uq_execution_claims_dedupe_key", table_name="execution_claims")
    op.drop_table("execution_claims")
