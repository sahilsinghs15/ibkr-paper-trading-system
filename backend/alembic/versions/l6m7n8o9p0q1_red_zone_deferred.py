"""Red Zone deferred state.

Revision ID: l6m7n8o9p0q1
Revises: k5l6m7n8o9p0
Create Date: 2026-09-08
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "l6m7n8o9p0q1"
down_revision: Union[str, Sequence[str], None] = "k5l6m7n8o9p0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("signal_jobs", sa.Column("deferred_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("signal_jobs", sa.Column("deferral_reason", sa.Text(), nullable=True))
    op.add_column("signal_jobs", sa.Column("reference_price", sa.Numeric(18, 4), nullable=True))
    op.add_column("signal_jobs", sa.Column("resolved_session_close", sa.DateTime(timezone=True), nullable=True))
    op.add_column("signal_jobs", sa.Column("applied_buffer_seconds", sa.Integer(), nullable=True))
    op.add_column("signal_jobs", sa.Column("deferred_session_count", sa.Integer(), nullable=False, server_default="0"))
    op.create_index("ix_signal_jobs_status_deferred_at", "signal_jobs", ["status", "deferred_at"])


def downgrade() -> None:
    op.drop_index("ix_signal_jobs_status_deferred_at", table_name="signal_jobs")
    op.drop_column("signal_jobs", "deferred_session_count")
    op.drop_column("signal_jobs", "applied_buffer_seconds")
    op.drop_column("signal_jobs", "resolved_session_close")
    op.drop_column("signal_jobs", "reference_price")
    op.drop_column("signal_jobs", "deferral_reason")
    op.drop_column("signal_jobs", "deferred_at")
