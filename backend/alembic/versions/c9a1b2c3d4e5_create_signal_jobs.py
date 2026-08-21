"""Create signal_jobs durable inbox queue table.

Revision ID: c9a1b2c3d4e5
Revises: c8e1a4b7d205
Create Date: 2026-08-21 17:30:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "c9a1b2c3d4e5"
down_revision: Union[str, Sequence[str], None] = "c8e1a4b7d205"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "signal_jobs",
        sa.Column(
            "job_id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("signal_id", sa.String(), nullable=False),
        sa.Column("strategy_id", sa.String(), nullable=False),
        sa.Column("trade_id", sa.String(), nullable=True, index=True),
        sa.Column("status", sa.String(), nullable=False, server_default="RECEIVED"),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("account_scope", sa.String(), nullable=True),
        sa.Column("raw_payload", JSONB, nullable=False),
        sa.Column("capture_data", JSONB, nullable=True),
        sa.Column("correlation_id", sa.String(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("worker_id", sa.String(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("idempotency_key", name="uq_signal_jobs_idempotency_key"),
    )
    op.create_index(
        "idx_signal_jobs_status_lease",
        "signal_jobs",
        ["status", "lease_expires_at"],
    )
    op.create_index(
        "idx_signal_jobs_strategy_status",
        "signal_jobs",
        ["strategy_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("idx_signal_jobs_strategy_status", table_name="signal_jobs")
    op.drop_index("idx_signal_jobs_status_lease", table_name="signal_jobs")
    op.drop_table("signal_jobs")
