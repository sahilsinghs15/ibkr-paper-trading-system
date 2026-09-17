"""Add scope to kill_switch_operations and update armed index to (account_id, scope).

Revision ID: l1m2n3o4p5q6
Revises: k1l2s3w4i5t6
Create Date: 2026-09-17

Adds scope ('engine' | 'account' | 'manual') to kill_switch_operations.
Updates armed partial unique index from (account_id) to (account_id, scope)
to allow independent operation of engine, account, and manual kill-switch scopes.
Adds manual_position_id and side to kill_switch_flatten_snapshots.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "l1m2n3o4p5q6"
down_revision: Union[str, Sequence[str], None] = "k1l2s3w4i5t6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Add scope column to kill_switch_operations with default 'engine'
    op.add_column(
        "kill_switch_operations",
        sa.Column("scope", sa.String(length=32), nullable=False, server_default="engine"),
    )

    # Backfill account-scoped operations
    op.execute(
        "UPDATE kill_switch_operations SET scope = 'account' WHERE requested_by = 'operator_account_flatten'"
    )

    # 2. Update armed unique index to include scope
    op.execute("DROP INDEX IF EXISTS uq_kill_switch_operations_armed_account")
    op.execute(
        """
        CREATE UNIQUE INDEX uq_kill_switch_operations_armed_account_scope
        ON kill_switch_operations (account_id, scope)
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

    # 3. Add manual_position_id and side columns to kill_switch_flatten_snapshots
    op.add_column(
        "kill_switch_flatten_snapshots",
        sa.Column("manual_position_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "kill_switch_flatten_snapshots",
        sa.Column("side", sa.String(length=8), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("kill_switch_flatten_snapshots", "side")
    op.drop_column("kill_switch_flatten_snapshots", "manual_position_id")

    op.execute("DROP INDEX IF EXISTS uq_kill_switch_operations_armed_account_scope")
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

    op.drop_column("kill_switch_operations", "scope")
