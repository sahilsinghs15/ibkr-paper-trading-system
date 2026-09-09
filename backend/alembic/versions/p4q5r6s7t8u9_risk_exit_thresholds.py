"""Add pair and account risk-exit threshold columns.

Revision ID: p4q5r6s7t8u9
Revises: o3p4q5r6s7t8
Create Date: 2026-09-09 19:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "p4q5r6s7t8u9"
down_revision: str | Sequence[str] | None = "o3p4q5r6s7t8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "allocations",
        sa.Column(
            "target_unit",
            sa.String(),
            nullable=False,
            server_default="ABSOLUTE",
        ),
    )
    op.add_column(
        "allocations",
        sa.Column(
            "stop_unit",
            sa.String(),
            nullable=False,
            server_default="ABSOLUTE",
        ),
    )
    op.add_column(
        "allocations",
        sa.Column(
            "exit_automation_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.create_check_constraint(
        "ck_allocations_target_unit",
        "allocations",
        "target_unit IN ('ABSOLUTE', 'PERCENT')",
    )
    op.create_check_constraint(
        "ck_allocations_stop_unit",
        "allocations",
        "stop_unit IN ('ABSOLUTE', 'PERCENT')",
    )
    op.create_check_constraint(
        "ck_allocations_target_nonneg",
        "allocations",
        "target >= 0",
    )
    op.create_check_constraint(
        "ck_allocations_stop_nonneg",
        "allocations",
        "stop >= 0",
    )
    op.create_check_constraint(
        "ck_allocations_time_limit_nonneg",
        "allocations",
        "time_limit >= 0",
    )

    op.add_column(
        "positions",
        sa.Column(
            "target_unit",
            sa.String(),
            nullable=False,
            server_default="ABSOLUTE",
        ),
    )
    op.add_column(
        "positions",
        sa.Column(
            "stop_unit",
            sa.String(),
            nullable=False,
            server_default="ABSOLUTE",
        ),
    )
    op.add_column(
        "positions",
        sa.Column("exit_reason", sa.String(), nullable=True),
    )

    op.add_column(
        "accounts",
        sa.Column("daily_target", sa.Numeric(18, 4), nullable=True),
    )
    op.add_column(
        "accounts",
        sa.Column("daily_stop", sa.Numeric(18, 4), nullable=True),
    )
    op.add_column(
        "accounts",
        sa.Column(
            "daily_target_unit",
            sa.String(),
            nullable=False,
            server_default="ABSOLUTE",
        ),
    )
    op.add_column(
        "accounts",
        sa.Column(
            "daily_stop_unit",
            sa.String(),
            nullable=False,
            server_default="ABSOLUTE",
        ),
    )
    op.add_column(
        "accounts",
        sa.Column(
            "account_risk_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.create_check_constraint(
        "ck_accounts_daily_target_unit",
        "accounts",
        "daily_target_unit IN ('ABSOLUTE', 'PERCENT')",
    )
    op.create_check_constraint(
        "ck_accounts_daily_stop_unit",
        "accounts",
        "daily_stop_unit IN ('ABSOLUTE', 'PERCENT')",
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


def downgrade() -> None:
    op.drop_constraint("ck_accounts_daily_stop_nonneg", "accounts", type_="check")
    op.drop_constraint("ck_accounts_daily_target_nonneg", "accounts", type_="check")
    op.drop_constraint("ck_accounts_daily_stop_unit", "accounts", type_="check")
    op.drop_constraint("ck_accounts_daily_target_unit", "accounts", type_="check")
    op.drop_column("accounts", "account_risk_enabled")
    op.drop_column("accounts", "daily_stop_unit")
    op.drop_column("accounts", "daily_target_unit")
    op.drop_column("accounts", "daily_stop")
    op.drop_column("accounts", "daily_target")

    op.drop_column("positions", "exit_reason")
    op.drop_column("positions", "stop_unit")
    op.drop_column("positions", "target_unit")

    op.drop_constraint("ck_allocations_time_limit_nonneg", "allocations", type_="check")
    op.drop_constraint("ck_allocations_stop_nonneg", "allocations", type_="check")
    op.drop_constraint("ck_allocations_target_nonneg", "allocations", type_="check")
    op.drop_constraint("ck_allocations_stop_unit", "allocations", type_="check")
    op.drop_constraint("ck_allocations_target_unit", "allocations", type_="check")
    op.drop_column("allocations", "exit_automation_enabled")
    op.drop_column("allocations", "stop_unit")
    op.drop_column("allocations", "target_unit")
