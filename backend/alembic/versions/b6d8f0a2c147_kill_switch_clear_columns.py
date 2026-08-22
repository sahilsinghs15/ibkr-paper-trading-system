"""Add kill switch clear audit columns.

Revision ID: b6d8f0a2c147
Revises: a4c7e2f10938
Create Date: 2026-08-22 14:30:00.000000

The blocked-account set lived only in process memory, so a restart silently
disarmed every active kill switch. It is now rebuilt from this table on
startup, which requires a durable way to record that an operator explicitly
disarmed an account -- completing a flatten is not the same as clearing it.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b6d8f0a2c147"
down_revision: Union[str, Sequence[str], None] = "a4c7e2f10938"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "kill_switch_operations",
        sa.Column("cleared_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "kill_switch_operations",
        sa.Column("cleared_by", sa.String(), nullable=True),
    )
    # Startup hydration scans by status; account_id is already indexed.
    op.create_index(
        "ix_kill_switch_operations_status_account",
        "kill_switch_operations",
        ["status", "account_id"],
    )
    # Existing rows predate the CLEARED status. Leaving them in their historical
    # status means they re-arm on the next startup, which is the safe direction:
    # an operator can clear deliberately, but nothing silently unblocks.


def downgrade() -> None:
    op.drop_index(
        "ix_kill_switch_operations_status_account",
        table_name="kill_switch_operations",
    )
    op.drop_column("kill_switch_operations", "cleared_by")
    op.drop_column("kill_switch_operations", "cleared_at")
