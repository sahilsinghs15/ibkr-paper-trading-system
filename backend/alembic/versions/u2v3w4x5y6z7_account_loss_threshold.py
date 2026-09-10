"""Per-account realized P&L loss threshold + crossing state.

Revision ID: u2v3w4x5y6z7
Revises: t1u2v3w4x5y6
Create Date: 2026-09-11
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "u2v3w4x5y6z7"
down_revision: str | Sequence[str] | None = "t1u2v3w4x5y6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("accounts", sa.Column("loss_threshold", sa.Numeric(precision=18, scale=4), nullable=True))
    op.create_table(
        "account_loss_state",
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("is_below", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("last_threshold", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("account_id"),
    )


def downgrade() -> None:
    op.drop_table("account_loss_state")
    op.drop_column("accounts", "loss_threshold")
