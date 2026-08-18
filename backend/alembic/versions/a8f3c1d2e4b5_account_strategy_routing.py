"""account strategy routing schema

Revision ID: a8f3c1d2e4b5
Revises: c3e9f1a2b4d6
Create Date: 2026-08-18 15:30:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a8f3c1d2e4b5"
down_revision: Union[str, Sequence[str], None] = "c3e9f1a2b4d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Enable Account × Strategy subscriptions and isolate positions by account."""
    op.add_column(
        "allocations",
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
    op.create_unique_constraint(
        "uq_allocations_account_strategy",
        "allocations",
        ["account_id", "strategy_id"],
    )
    op.create_check_constraint(
        "ck_allocations_alloc_pct_range",
        "allocations",
        "alloc_pct >= 0 AND alloc_pct <= 1",
    )
    op.create_check_constraint(
        "ck_accounts_total_margin_positive",
        "accounts",
        "total_margin > 0",
    )

    op.drop_constraint("positions_pkey", "positions", type_="primary")
    op.create_primary_key("pk_positions_account_trade", "positions", ["account_id", "trade_id"])
    op.create_index("ix_positions_trade_id", "positions", ["trade_id"], unique=False)


def downgrade() -> None:
    """Reverse Account × Strategy subscription columns. Unsafe if duplicate trade_ids exist."""
    op.drop_index("ix_positions_trade_id", table_name="positions")
    op.drop_constraint("pk_positions_account_trade", "positions", type_="primary")
    op.create_primary_key("positions_pkey", "positions", ["trade_id"])

    op.drop_constraint("ck_accounts_total_margin_positive", "accounts", type_="check")
    op.drop_constraint("ck_allocations_alloc_pct_range", "allocations", type_="check")
    op.drop_constraint("uq_allocations_account_strategy", "allocations", type_="unique")
    op.drop_column("allocations", "enabled")
