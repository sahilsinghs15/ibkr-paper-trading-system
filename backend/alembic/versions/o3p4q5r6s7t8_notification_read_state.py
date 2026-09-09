"""create user notification read state tables

Revision ID: o3p4q5r6s7t8
Revises: l6m7n8o9p0q1
Create Date: 2026-09-09 17:30:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "o3p4q5r6s7t8"
down_revision: Union[str, Sequence[str], None] = "l6m7n8o9p0q1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_notification_state",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("last_read_all_id", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )

    op.create_table(
        "user_notification_reads",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("event_id", sa.BigInteger(), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["event_log.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "event_id"),
    )
    op.create_index("ix_user_notification_reads_user", "user_notification_reads", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_user_notification_reads_user", table_name="user_notification_reads")
    op.drop_table("user_notification_reads")
    op.drop_table("user_notification_state")
