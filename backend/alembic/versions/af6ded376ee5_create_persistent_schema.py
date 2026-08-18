"""create_persistent_schema

Revision ID: af6ded376ee5
Revises: d4bd73bb4fde
Create Date: 2026-08-17 13:13:33.627257

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'af6ded376ee5'
down_revision: Union[str, Sequence[str], None] = 'd4bd73bb4fde'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'signals',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('strategy_id', sa.String(), nullable=False),
        sa.Column('signal_id', sa.String(), nullable=False),
        sa.Column('action', sa.String(), nullable=False),
        sa.Column('pair', sa.String(), nullable=False),
        sa.Column('side', sa.String(), nullable=False),
        sa.Column('ref_price_a', sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column('ref_price_b', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('raw_payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('reject_reason', sa.String(), nullable=True),
        sa.Column('received_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('strategy_id', 'signal_id', name='uq_signals_strategy_signal')
    )

    op.create_table(
        'accounts',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('ibkr_account', sa.String(), nullable=False),
        sa.Column('total_margin', sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.PrimaryKeyConstraint('id')
    )

    op.create_table(
        'strategies',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('strategy_id', sa.String(), nullable=False),
        sa.Column('legs', sa.Integer(), nullable=False),
        sa.Column('expression', sa.String(), nullable=False, server_default='CFD'),
        sa.Column('max_open_positions', sa.Integer(), nullable=False),
        sa.Column('weight_source', sa.String(), nullable=False),
        sa.Column('target_delta', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('strategy_id')
    )

    op.create_table(
        'allocations',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('account_id', sa.BigInteger(), nullable=False),
        sa.Column('strategy_id', sa.String(), nullable=False),
        sa.Column('alloc_pct', sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column('target', sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column('stop', sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column('time_limit', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ),
        sa.ForeignKeyConstraint(['strategy_id'], ['strategies.strategy_id'], ),
        sa.PrimaryKeyConstraint('id')
    )

    op.create_table(
        'per_symbol_limits',
        sa.Column('symbol', sa.String(), nullable=False),
        sa.Column('account_id', sa.BigInteger(), nullable=False),
        sa.Column('money_limit', sa.Numeric(precision=18, scale=4), nullable=False),
        sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ),
        sa.PrimaryKeyConstraint('symbol', 'account_id')
    )

    op.create_table(
        'orders',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('signal_id', sa.BigInteger(), nullable=False),
        sa.Column('account_id', sa.BigInteger(), nullable=False),
        sa.Column('strategy_id', sa.String(), nullable=False),
        sa.Column('leg', sa.String(), nullable=False),
        sa.Column('symbol', sa.String(), nullable=False),
        sa.Column('ibkr_contract', sa.String(), nullable=False),
        sa.Column('buy_sell', sa.String(), nullable=False),
        sa.Column('quantity', sa.Integer(), nullable=False),
        sa.Column('limit_price', sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('broker_order_id', sa.String(), nullable=True),
        sa.Column('fill_price', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('fill_qty', sa.Integer(), nullable=True),
        sa.Column('filled_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('margin_impact', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ),
        sa.ForeignKeyConstraint(['signal_id'], ['signals.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_orders_account_status', 'orders', ['account_id', 'status'], unique=False)
    op.create_index('ix_orders_signal_id', 'orders', ['signal_id'], unique=False)

    op.create_table(
        'event_log',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('ts', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('process', sa.String(), nullable=False),
        sa.Column('signal_id', sa.BigInteger(), nullable=True),
        sa.Column('order_id', sa.BigInteger(), nullable=True),
        sa.Column('kind', sa.String(), nullable=False),
        sa.Column('detail', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(['order_id'], ['orders.id'], ),
        sa.ForeignKeyConstraint(['signal_id'], ['signals.id'], ),
        sa.PrimaryKeyConstraint('id')
    )

    op.create_table(
        'positions',
        sa.Column('trade_id', sa.String(), nullable=False),
        sa.Column('strategy_id', sa.String(), nullable=False),
        sa.Column('account_id', sa.BigInteger(), nullable=False),
        sa.Column('leg_a_symbol', sa.String(), nullable=False),
        sa.Column('leg_a_signed_qty', sa.Integer(), nullable=False),
        sa.Column('leg_a_entry_mark', sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column('leg_b_symbol', sa.String(), nullable=True),
        sa.Column('leg_b_signed_qty', sa.Integer(), nullable=True),
        sa.Column('leg_b_entry_mark', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('realised_pnl', sa.Numeric(precision=18, scale=4), server_default='0', nullable=False),
        sa.Column('commission', sa.Numeric(precision=18, scale=4), server_default='0', nullable=False),
        sa.Column('live_pnl', sa.Numeric(precision=18, scale=4), server_default='0', nullable=False),
        sa.Column('target', sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column('stop', sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column('time_limit', sa.Integer(), nullable=False),
        sa.Column('opened_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('risk_state', sa.String(), nullable=False),
        sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ),
        sa.PrimaryKeyConstraint('trade_id')
    )

    op.create_table(
        'instruments',
        sa.Column('symbol', sa.String(), nullable=False),
        sa.Column('sec_type', sa.String(), nullable=False),
        sa.Column('trade_conid', sa.BigInteger(), nullable=False),
        sa.Column('market_data_conid', sa.BigInteger(), nullable=False),
        sa.Column('underlying_exchange', sa.String(), nullable=False),
        sa.Column('exchange', sa.String(), nullable=False),
        sa.Column('currency', sa.String(), nullable=False),
        sa.Column('multiplier', sa.Numeric(precision=18, scale=4), server_default='1', nullable=False),
        sa.PrimaryKeyConstraint('symbol')
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('instruments')
    op.drop_table('positions')
    op.drop_table('event_log')
    op.drop_index('ix_orders_signal_id', table_name='orders')
    op.drop_index('ix_orders_account_status', table_name='orders')
    op.drop_table('orders')
    op.drop_table('per_symbol_limits')
    op.drop_table('allocations')
    op.drop_table('strategies')
    op.drop_table('accounts')
    op.drop_table('signals')
