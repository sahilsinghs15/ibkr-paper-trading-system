"""Add default_symbol_limit column to accounts table.

Revision ID: e9f2a7b4c610
Revises: b6d8f0a2c147
Create Date: 2026-08-25 21:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e9f2a7b4c610"
down_revision: Union[str, Sequence[str], None] = "b6d8f0a2c147"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column("default_symbol_limit", sa.Numeric(18, 4), nullable=True, server_default="10000000.0000"),
    )


def downgrade() -> None:
    op.drop_column("accounts", "default_symbol_limit")
