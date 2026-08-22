"""Composite index supporting the trade_id in-flight guard in claim_next_jobs.

Revision ID: f3a5b7d9e206
Revises: e2f4a6c8d105
Create Date: 2026-08-22 12:00:00.000000
"""

from typing import Sequence, Union

from alembic import op

revision: str = "f3a5b7d9e206"
down_revision: Union[str, Sequence[str], None] = "e2f4a6c8d105"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # claim_next_jobs runs a correlated NOT EXISTS on (trade_id, status) for
    # every candidate row, on every poll, from every worker. The existing
    # ix_signal_jobs_trade_id index alone leaves status as a heap filter.
    op.create_index(
        "ix_signal_jobs_trade_id_status",
        "signal_jobs",
        ["trade_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_signal_jobs_trade_id_status", table_name="signal_jobs")
