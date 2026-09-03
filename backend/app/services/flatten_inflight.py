"""Shared in-process flatten exclusion (kill-switch, pair-close, broker flatten).

Flatten paths deliberately skip ``execution_claims``. This registry is the
mutual-exclusion those producers still need. Keys are the position, not a
per-attempt uuid. After a crash the registry is empty; retake is the caller's
responsibility (broker snapshot must show qty still live).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Hashable

logger = logging.getLogger(__name__)

_lock = asyncio.Lock()
_HELD: set[Hashable] = set()


def ledger_key(account_id: int, trade_id: str) -> tuple[str, int, str]:
    return ("ledger", int(account_id), str(trade_id))


def broker_key(ibkr_account: str, con_id: int) -> tuple[str, str, int]:
    return ("broker", ibkr_account.strip().upper(), int(con_id))


async def try_acquire(key: Hashable) -> bool:
    async with _lock:
        if key in _HELD:
            return False
        _HELD.add(key)
        return True


async def try_acquire_many(keys: list[Hashable]) -> bool:
    """Acquire every key or none. Empty list is a no-op success."""
    unique = list(dict.fromkeys(keys))
    if not unique:
        return True
    async with _lock:
        if any(key in _HELD for key in unique):
            return False
        for key in unique:
            _HELD.add(key)
        return True


async def release(key: Hashable) -> None:
    async with _lock:
        _HELD.discard(key)


async def release_many(keys: list[Hashable]) -> None:
    unique = list(dict.fromkeys(keys))
    async with _lock:
        for key in unique:
            _HELD.discard(key)
