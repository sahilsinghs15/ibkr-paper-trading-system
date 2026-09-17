"""Create instance daily credit usage ledger.

Revision ID: c1d2e3f4a5b6
Revises: z7a8b9c0d1e2
Create Date: 2026-09-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c1d2e3f4a5b6"
down_revision: str | Sequence[str] | None = "z7a8b9c0d1e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "instance_daily_credit_usage",
        sa.Column("usage_date", sa.Date(), nullable=False),
        sa.Column("instance_id", sa.String(length=32), nullable=False),
        sa.Column("aws_account_id", sa.String(length=16), nullable=True),
        sa.Column("region", sa.String(length=32), nullable=True),
        sa.Column("eni_id", sa.String(length=32), nullable=True),
        sa.Column("public_ipv4", sa.String(length=64), nullable=True),
        sa.Column("eip_allocation_id", sa.String(length=32), nullable=True),
        sa.Column("ec2_cost_usd", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("public_ipv4_cost_usd", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("total_cost_usd", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="ACTUAL"),
        sa.Column("source", sa.String(length=64), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("usage_date", name="pk_instance_daily_credit_usage"),
        sa.CheckConstraint("status IN ('ACTUAL','FAILED')", name="ck_instance_daily_credit_status"),
    )
    op.create_index(
        "ix_instance_daily_credit_usage_date",
        "instance_daily_credit_usage",
        ["usage_date"],
    )


def downgrade() -> None:
    op.drop_index("ix_instance_daily_credit_usage_date", table_name="instance_daily_credit_usage")
    op.drop_table("instance_daily_credit_usage")
