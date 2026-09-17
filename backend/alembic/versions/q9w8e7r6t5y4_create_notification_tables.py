"""Create notification_log and notification_deliveries tables for centralized notification system.

Revision ID: q9w8e7r6t5y4
Revises: z7a8b9c0d1e2
Create Date: 2026-09-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "q9w8e7r6t5y4"
down_revision: str | Sequence[str] | None = "z7a8b9c0d1e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. notification_log
    op.create_table(
        "notification_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("notification_id", sa.String(length=64), nullable=False),
        sa.Column("source_event_id", sa.String(length=128), nullable=True),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("dedupe_key", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=32), server_default="PENDING", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("notification_id", name="uq_notification_log_notification_id"),
        sa.UniqueConstraint("dedupe_key", name="uq_notification_log_dedupe_key"),
    )
    op.create_index("ix_notification_log_notification_id", "notification_log", ["notification_id"], unique=True)
    op.create_index("ix_notification_log_source_event_id", "notification_log", ["source_event_id"], unique=False)
    op.create_index("ix_notification_log_category", "notification_log", ["category"], unique=False)
    op.create_index("ix_notification_log_severity", "notification_log", ["severity"], unique=False)
    op.create_index("ix_notification_log_dedupe_key", "notification_log", ["dedupe_key"], unique=True)
    op.create_index("ix_notification_log_status", "notification_log", ["status"], unique=False)
    op.create_index("ix_notification_log_created_at", "notification_log", ["created_at"], unique=False)

    # 2. notification_deliveries
    op.create_table(
        "notification_deliveries",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("delivery_id", sa.String(length=64), nullable=False),
        sa.Column("notification_id", sa.BigInteger(), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("recipient", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="PENDING", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_retries", sa.Integer(), server_default="3", nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_message_id", sa.String(length=128), nullable=True),
        sa.Column("error_details", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["notification_id"],
            ["notification_log.id"],
            name="fk_notification_deliveries_notification_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("delivery_id", name="uq_notification_deliveries_delivery_id"),
        sa.UniqueConstraint("notification_id", "channel", name="uq_notification_delivery_channel"),
    )
    op.create_index("ix_notification_deliveries_delivery_id", "notification_deliveries", ["delivery_id"], unique=True)
    op.create_index("ix_notification_deliveries_notification_id", "notification_deliveries", ["notification_id"], unique=False)
    op.create_index("ix_notification_deliveries_channel", "notification_deliveries", ["channel"], unique=False)
    op.create_index("ix_notification_deliveries_status", "notification_deliveries", ["status"], unique=False)
    op.create_index("ix_notification_deliveries_next_retry_at", "notification_deliveries", ["next_retry_at"], unique=False)
    op.create_index(
        "ix_notification_deliveries_pending_queue",
        "notification_deliveries",
        ["status", "next_retry_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_notification_deliveries_pending_queue", table_name="notification_deliveries")
    op.drop_index("ix_notification_deliveries_next_retry_at", table_name="notification_deliveries")
    op.drop_index("ix_notification_deliveries_status", table_name="notification_deliveries")
    op.drop_index("ix_notification_deliveries_channel", table_name="notification_deliveries")
    op.drop_index("ix_notification_deliveries_notification_id", table_name="notification_deliveries")
    op.drop_index("ix_notification_deliveries_delivery_id", table_name="notification_deliveries")
    op.drop_table("notification_deliveries")

    op.drop_index("ix_notification_log_created_at", table_name="notification_log")
    op.drop_index("ix_notification_log_status", table_name="notification_log")
    op.drop_index("ix_notification_log_dedupe_key", table_name="notification_log")
    op.drop_index("ix_notification_log_severity", table_name="notification_log")
    op.drop_index("ix_notification_log_category", table_name="notification_log")
    op.drop_index("ix_notification_log_source_event_id", table_name="notification_log")
    op.drop_index("ix_notification_log_notification_id", table_name="notification_log")
    op.drop_table("notification_log")
