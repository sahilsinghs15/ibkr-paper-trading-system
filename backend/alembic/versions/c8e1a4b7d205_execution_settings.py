"""Broker executions table, fill-price precision, event idempotency.

Revision ID: c8e1a4b7d205
Revises: b2d8f4a1c903
Create Date: 2026-08-19 18:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c8e1a4b7d205"
down_revision: Union[str, Sequence[str], None] = "b2d8f4a1c903"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "execution_settings",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("square_off_after_sec", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("max_retries", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("retry_interval_sec", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("retry_window_sec", sa.Integer(), nullable=False, server_default="30"),
        sa.CheckConstraint("id = 1", name="ck_execution_settings_singleton"),
        sa.CheckConstraint("square_off_after_sec > 0", name="ck_execution_settings_timeout_pos"),
        sa.CheckConstraint("max_retries >= 0", name="ck_execution_settings_retries_nonneg"),
        sa.CheckConstraint("retry_interval_sec > 0", name="ck_execution_settings_interval_pos"),
        sa.CheckConstraint("retry_window_sec > 0", name="ck_execution_settings_window_pos"),
        sa.CheckConstraint(
            "retry_window_sec >= retry_interval_sec",
            name="ck_execution_settings_window_ge_interval",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(
        sa.text(
            "INSERT INTO execution_settings "
            "(id, enabled, square_off_after_sec, max_retries, retry_interval_sec, retry_window_sec) "
            "VALUES (1, true, 30, 3, 5, 30)"
        )
    )


def downgrade() -> None:
    op.drop_table("execution_settings")
