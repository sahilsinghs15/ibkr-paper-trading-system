"""Partial unique index: one armed kill-switch operation per account.

Revision ID: j4k5l6m7n8o9
Revises: i3j4k5l6m7n8
Create Date: 2026-09-03 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op


revision: str = "j4k5l6m7n8o9"
down_revision: Union[str, Sequence[str], None] = "i3j4k5l6m7n8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_ARMED = (
    "ACTIVATING",
    "FLATTENING",
    "RECONCILING",
    "RETRYING",
    "FLAT",
    "COMPLETE",
    "UNRESOLVED",
)


def upgrade() -> None:
    """At most one armed kill_switch_operations row per account_id (M3)."""
    op.execute(
        f"""
        DELETE FROM kill_switch_operations a
        USING kill_switch_operations b
        WHERE a.account_id = b.account_id
          AND a.status IN {str(_ARMED)}
          AND b.status IN {str(_ARMED)}
          AND a.created_at > b.created_at
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_kill_switch_operations_armed_account
        ON kill_switch_operations (account_id)
        WHERE status IN (
            'ACTIVATING',
            'FLATTENING',
            'RECONCILING',
            'RETRYING',
            'FLAT',
            'COMPLETE',
            'UNRESOLVED'
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_kill_switch_operations_armed_account")
