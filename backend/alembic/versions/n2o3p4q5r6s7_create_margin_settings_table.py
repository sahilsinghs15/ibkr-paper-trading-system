"""Singleton operator-tunable margin-gate policy.

Revision ID: n2o3p4q5r6s7
Revises: m1n2o3p4q5r6
Create Date: 2026-09-02 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "n2o3p4q5r6s7"
down_revision: Union[str, Sequence[str], None] = "m1n2o3p4q5r6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "margin_settings",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("check_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "gate_basis",
            sa.String(),
            nullable=False,
            server_default="available_funds",
        ),
        sa.Column(
            "min_free_buffer",
            sa.Numeric(18, 4),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "min_free_pct_of_netliq",
            sa.Numeric(9, 6),
            nullable=False,
            server_default="0.050000",
        ),
        sa.Column(
            "comfort_ratio",
            sa.Numeric(9, 6),
            nullable=False,
            server_default="0.800000",
        ),
        sa.Column(
            "confirm_borderline",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "enforce_look_ahead",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "reject_on_stale_snapshot",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "default_rate",
            sa.Numeric(9, 6),
            nullable=False,
            server_default="0.300000",
        ),
        sa.Column(
            "rate_safety_multiplier",
            sa.Numeric(9, 6),
            nullable=False,
            server_default="1.100000",
        ),
        sa.CheckConstraint("id = 1", name="ck_margin_settings_singleton"),
        sa.CheckConstraint(
            "gate_basis IN ('available_funds', 'excess_liquidity')",
            name="ck_margin_settings_gate_basis",
        ),
        sa.CheckConstraint(
            "min_free_buffer >= 0", name="ck_margin_settings_min_free_buffer"
        ),
        sa.CheckConstraint(
            "min_free_pct_of_netliq >= 0 AND min_free_pct_of_netliq <= 1",
            name="ck_margin_settings_min_free_pct",
        ),
        sa.CheckConstraint(
            "comfort_ratio > 0 AND comfort_ratio <= 1",
            name="ck_margin_settings_comfort_ratio",
        ),
        sa.CheckConstraint(
            "default_rate > 0 AND default_rate <= 1",
            name="ck_margin_settings_default_rate",
        ),
        sa.CheckConstraint(
            "rate_safety_multiplier >= 1 AND rate_safety_multiplier <= 2",
            name="ck_margin_settings_rate_safety",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(
        sa.text(
            "INSERT INTO margin_settings "
            "(id, check_enabled, gate_basis, min_free_buffer, min_free_pct_of_netliq, "
            "comfort_ratio, confirm_borderline, enforce_look_ahead, "
            "reject_on_stale_snapshot, default_rate, rate_safety_multiplier) "
            "VALUES (1, false, 'available_funds', 0, 0.050000, 0.800000, true, true, "
            "true, 0.300000, 1.100000)"
        )
    )


def downgrade() -> None:
    op.drop_table("margin_settings")
