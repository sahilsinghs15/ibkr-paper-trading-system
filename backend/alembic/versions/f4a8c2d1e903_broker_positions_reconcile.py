"""Create broker_positions and position_reconcile_runs tables.

Revision ID: f4a8c2d1e903
Revises: e9f2a7b4c610
Create Date: 2026-08-26 14:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f4a8c2d1e903"
down_revision: Union[str, Sequence[str], None] = "e9f2a7b4c610"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "broker_positions",
        sa.Column("ibkr_account", sa.String(), nullable=False),
        sa.Column("con_id", sa.BigInteger(), nullable=False),
        sa.Column("account_id", sa.BigInteger(), sa.ForeignKey("accounts.id"), nullable=True),
        sa.Column("symbol", sa.String(), nullable=False),
        sa.Column("sec_type", sa.String(), nullable=False),
        sa.Column("currency", sa.String(), nullable=False),
        sa.Column("exchange", sa.String(), nullable=False, server_default=""),
        sa.Column("signed_qty", sa.Numeric(18, 4), nullable=False),
        sa.Column("avg_cost", sa.Numeric(18, 8), nullable=False),
        sa.Column(
            "as_of",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("ibkr_account", "con_id"),
        sa.UniqueConstraint("ibkr_account", "con_id", name="uq_broker_positions_account_conid"),
    )
    op.create_index(
        "ix_broker_positions_account_id", "broker_positions", ["account_id"]
    )

    op.create_table(
        "position_reconcile_runs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("timed_out", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("broker_line_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("match_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("ghost_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("orphan_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("drift_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "unmapped_account_count", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column(
            "mismatches",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("position_reconcile_runs")
    op.drop_index("ix_broker_positions_account_id", table_name="broker_positions")
    op.drop_table("broker_positions")
