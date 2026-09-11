"""Trading Pause Service and In-Memory Hot Cache.

Provides an authoritative mechanism to pause new opening order submissions on an account-by-account basis
without triggering emergency flattens or altering kill-switch states.

Invariants:
- `accounts.trading_paused` in PostgreSQL is authoritative.
- An in-memory hot cache `_PAUSED_ACCOUNTS` provides low-latency O(1) checks during signal evaluation.
- All pause/resume operations write to PostgreSQL FIRST and mutate the in-memory cache SECOND.
- Hydrated on system startup from PostgreSQL before workers process signals.
- Fully isolated from `accounts.enabled` and `kill_switch_operations`.
- Pause NEVER submits orders or triggers flattens.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.account import AccountModel

logger = logging.getLogger(__name__)

# Hot in-memory read cache of paused account IDs.
_PAUSED_ACCOUNTS: set[int] = set()


def is_account_trading_paused(account_id: int) -> bool:
    """Return True if account is currently paused for new OPEN orders."""
    return account_id in _PAUSED_ACCOUNTS


def get_paused_accounts() -> set[int]:
    """Return a copy of all currently paused account IDs from memory cache."""
    return set(_PAUSED_ACCOUNTS)


def _pause_account_cache(account_id: int) -> None:
    """Mark an account as paused in memory cache."""
    _PAUSED_ACCOUNTS.add(account_id)


def _resume_account_cache(account_id: int) -> None:
    """Remove an account from the paused memory cache."""
    _PAUSED_ACCOUNTS.discard(account_id)


async def hydrate_trading_pause_cache(
    session_factory: async_sessionmaker[AsyncSession],
) -> set[int]:
    """Rebuild the paused-account hot cache from PostgreSQL accounts table.

    Must run during runtime startup before workers process signals.
    """
    async with session_factory() as session:
        result = await session.execute(
            select(AccountModel.id).where(AccountModel.trading_paused.is_(True))
        )
        paused = {int(row[0]) for row in result.all()}

    _PAUSED_ACCOUNTS.clear()
    _PAUSED_ACCOUNTS.update(paused)
    if paused:
        logger.warning(
            "TRADING PAUSE CACHE HYDRATED FROM DB: %d account(s) paused: %s",
            len(paused),
            sorted(paused),
        )
    else:
        logger.info("Trading pause cache hydrated: no accounts paused")
    return set(_PAUSED_ACCOUNTS)


class TradingPauseService:
    """Service managing durable account trading pause state."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_pause_status(
        self, account_id: int
    ) -> tuple[AccountModel | None, bool]:
        """Fetch account row and report whether it is trading paused."""
        async with self._session_factory() as session:
            account = await session.get(AccountModel, account_id)
            if account is None:
                return None, False
            return account, account.trading_paused

    async def pause_account(
        self,
        account_id: int,
        *,
        paused_by: str = "operator",
    ) -> tuple[AccountModel | None, bool]:
        """Pause new OPEN orders for an account.

        Idempotent: if already paused, returns the existing state without updating paused_at.
        DB write happens FIRST; hot cache is updated SECOND.
        """
        now = datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            account = await session.get(AccountModel, account_id)
            if account is None:
                return None, False

            if account.trading_paused:
                # Already paused - idempotent no-op
                _pause_account_cache(account_id)
                return account, False

            account.trading_paused = True
            account.paused_at = now
            account.paused_by = paused_by
            session.add(account)

        # Update hot cache after successful DB commit
        _pause_account_cache(account_id)
        logger.warning(
            "TRADING_PAUSED: account_id=%s ibkr=%s paused_by=%s",
            account_id,
            account.ibkr_account,
            paused_by,
        )
        return account, True

    async def resume_account(
        self,
        account_id: int,
    ) -> tuple[AccountModel | None, bool]:
        """Resume trading for an account, re-allowing new OPEN orders.

        Idempotent: if already active (not paused), returns existing state.
        DB write happens FIRST; hot cache is updated SECOND.
        """
        async with self._session_factory() as session, session.begin():
            account = await session.get(AccountModel, account_id)
            if account is None:
                return None, False

            if not account.trading_paused:
                # Already active - idempotent no-op
                _resume_account_cache(account_id)
                return account, False

            account.trading_paused = False
            account.paused_at = None
            account.paused_by = None
            session.add(account)

        # Update hot cache after successful DB commit
        _resume_account_cache(account_id)
        logger.info(
            "TRADING_RESUMED: account_id=%s ibkr=%s resumed",
            account_id,
            account.ibkr_account,
        )
        return account, True
