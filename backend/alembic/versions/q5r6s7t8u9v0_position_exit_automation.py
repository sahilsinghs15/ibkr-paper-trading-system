"""Add per-position exit_automation_enabled flag.

Revision ID: q5r6s7t8u9v0
Revises: p4q5r6s7t8u9
Create Date: 2026-09-09 20:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "q5r6s7t8u9v0"
down_revision: str | Sequence[str] | None = "p4q5r6s7t8u9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "positions",
        sa.Column(
            "exit_automation_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.execute(
        """
        UPDATE positions p
        SET exit_automation_enabled = a.exit_automation_enabled
        FROM allocations a
        WHERE a.account_id = p.account_id
          AND a.strategy_id = p.strategy_id
          AND p.risk_state = 'OPEN'
        """
    )


def downgrade() -> None:
    op.drop_column("positions", "exit_automation_enabled")
