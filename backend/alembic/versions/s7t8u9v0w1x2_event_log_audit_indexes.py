"""Add indexes on event_log for audit query pagination and filtering.

Revision ID: s7t8u9v0w1x2
Revises: r6s7t8u9v0w1
Create Date: 2026-09-09 22:30:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "s7t8u9v0w1x2"
down_revision: str | Sequence[str] | None = "r6s7t8u9v0w1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_event_log_ts_id",
        "event_log",
        ["ts", "id"],
        unique=False,
    )
    op.create_index(
        "ix_event_log_process_ts",
        "event_log",
        ["process", "ts"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_event_log_process_ts", table_name="event_log")
    op.drop_index("ix_event_log_ts_id", table_name="event_log")
