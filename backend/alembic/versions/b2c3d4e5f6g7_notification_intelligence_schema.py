"""Add event_type, correlation_id, and suppressed_reason to notification_log.

Revision ID: b2c3d4e5f6g7
Revises: q9w8e7r6t5y4
Create Date: 2026-09-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b2c3d4e5f6g7"
down_revision: str | Sequence[str] | None = "q9w8e7r6t5y4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "notification_log",
        sa.Column(
            "event_type",
            sa.String(length=64),
            server_default="UNKNOWN",
            nullable=False,
        ),
    )
    op.add_column(
        "notification_log",
        sa.Column("correlation_id", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "notification_log",
        sa.Column("suppressed_reason", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_notification_log_event_type",
        "notification_log",
        ["event_type"],
        unique=False,
    )
    op.create_index(
        "ix_notification_log_correlation_id",
        "notification_log",
        ["correlation_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_notification_log_correlation_id", table_name="notification_log")
    op.drop_index("ix_notification_log_event_type", table_name="notification_log")
    op.drop_column("notification_log", "suppressed_reason")
    op.drop_column("notification_log", "correlation_id")
    op.drop_column("notification_log", "event_type")
