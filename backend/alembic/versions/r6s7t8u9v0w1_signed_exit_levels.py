"""Store stop/target as signed PnL levels (negatives allowed).

Revision ID: r6s7t8u9v0w1
Revises: q5r6s7t8u9v0
Create Date: 2026-09-09 21:50:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "r6s7t8u9v0w1"
down_revision: str | Sequence[str] | None = "q5r6s7t8u9v0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_allocations_target_nonneg", "allocations", type_="check")
    op.drop_constraint("ck_allocations_stop_nonneg", "allocations", type_="check")
    op.drop_constraint("ck_accounts_daily_target_nonneg", "accounts", type_="check")
    op.drop_constraint("ck_accounts_daily_stop_nonneg", "accounts", type_="check")
    # Previous convention stored stop as a positive magnitude (fire at -stop).
    op.execute("UPDATE allocations SET stop = -stop WHERE stop > 0")
    op.execute("UPDATE positions SET stop = -stop WHERE stop > 0")
    op.execute("UPDATE accounts SET daily_stop = -daily_stop WHERE daily_stop > 0")


def downgrade() -> None:
    op.execute("UPDATE allocations SET stop = -stop WHERE stop < 0")
    op.execute("UPDATE positions SET stop = -stop WHERE stop < 0")
    op.execute("UPDATE accounts SET daily_stop = -daily_stop WHERE daily_stop < 0")
    op.create_check_constraint(
        "ck_allocations_target_nonneg", "allocations", "target >= 0"
    )
    op.create_check_constraint(
        "ck_allocations_stop_nonneg", "allocations", "stop >= 0"
    )
    op.create_check_constraint(
        "ck_accounts_daily_target_nonneg",
        "accounts",
        "daily_target IS NULL OR daily_target >= 0",
    )
    op.create_check_constraint(
        "ck_accounts_daily_stop_nonneg",
        "accounts",
        "daily_stop IS NULL OR daily_stop >= 0",
    )
