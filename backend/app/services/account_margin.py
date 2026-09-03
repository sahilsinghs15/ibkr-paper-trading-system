"""Live IBKR account-margin snapshots via reqAccountSummary.

Registers on the existing TWSClient. Issues reqAccountSummary once after
connect; IBKR re-pushes changed tags roughly every three minutes. Inf and
Double.MAX_VALUE parse to None. A snapshot from a dead socket is never used.
"""

from __future__ import annotations

import logging
import math
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from app.broker.ibkr.gateway_rate_limiter import (
    PRIORITY_DIAGNOSTIC,
    GatewayRateLimiter,
)
from app.broker.ibkr.tws_client import TWSClient
from app.core.config import get_settings

logger = logging.getLogger(__name__)

MARGIN_TAGS = ",".join(
    [
        "NetLiquidation",
        "AvailableFunds",
        "ExcessLiquidity",
        "FullInitMarginReq",
        "FullMaintMarginReq",
        "BuyingPower",
        "GrossPositionValue",
        "TotalCashValue",
        "Cushion",
        "LookAheadInitMarginReq",
        "LookAheadMaintMarginReq",
        "LookAheadAvailableFunds",
        "LookAheadExcessLiquidity",
        "LookAheadNextChange",
    ]
)

_DOUBLE_MAX = Decimal("1.7976931348623157E+308")
_ACCOUNT_SUMMARY_REQ_START = 70000
_GROUP_ALL = "All"

_NUMERIC_TAGS = frozenset(
    {
        "NetLiquidation",
        "AvailableFunds",
        "ExcessLiquidity",
        "FullInitMarginReq",
        "FullMaintMarginReq",
        "BuyingPower",
        "GrossPositionValue",
        "TotalCashValue",
        "Cushion",
        "LookAheadInitMarginReq",
        "LookAheadMaintMarginReq",
        "LookAheadAvailableFunds",
        "LookAheadExcessLiquidity",
    }
)

_FIELD_BY_TAG = {
    "NetLiquidation": "net_liquidation",
    "AvailableFunds": "available_funds",
    "ExcessLiquidity": "excess_liquidity",
    "FullInitMarginReq": "full_init_margin_req",
    "FullMaintMarginReq": "full_maint_margin_req",
    "BuyingPower": "buying_power",
    "GrossPositionValue": "gross_position_value",
    "TotalCashValue": "total_cash_value",
    "Cushion": "cushion",
    "LookAheadInitMarginReq": "look_ahead_init_margin_req",
    "LookAheadMaintMarginReq": "look_ahead_maint_margin_req",
    "LookAheadAvailableFunds": "look_ahead_available_funds",
    "LookAheadExcessLiquidity": "look_ahead_excess_liquidity",
}


def parse_ibkr_number(value: str | None) -> Decimal | None:
    """Parse an IBKR account-summary / orderState numeric string.

    Inf, -inf, NaN, and Double.MAX_VALUE become None. Never guess.
    """
    if value is None:
        return None
    raw = str(value).strip().replace(",", "")
    if not raw or raw.upper() in {"N/A", "NA", "NONE", "NULL"}:
        return None
    try:
        as_float = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(as_float):
        return None
    try:
        parsed = Decimal(raw)
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite() or abs(parsed) >= _DOUBLE_MAX:
        return None
    return parsed


def parse_look_ahead_next_change(value: str | None) -> datetime | None:
    """Parse LookAheadNextChange (unix timestamp seconds) into UTC datetime."""
    number = parse_ibkr_number(value)
    if number is None or number <= 0:
        return None
    try:
        return datetime.fromtimestamp(int(number), tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


@dataclass(frozen=True)
class AccountMarginSnapshot:
    """Frozen IBKR account-summary row for one managed account."""

    ibkr_account: str
    currency: str | None = None
    as_of: datetime | None = None
    net_liquidation: Decimal | None = None
    available_funds: Decimal | None = None
    excess_liquidity: Decimal | None = None
    full_init_margin_req: Decimal | None = None
    full_maint_margin_req: Decimal | None = None
    buying_power: Decimal | None = None
    gross_position_value: Decimal | None = None
    total_cash_value: Decimal | None = None
    cushion: Decimal | None = None
    look_ahead_init_margin_req: Decimal | None = None
    look_ahead_maint_margin_req: Decimal | None = None
    look_ahead_available_funds: Decimal | None = None
    look_ahead_excess_liquidity: Decimal | None = None
    look_ahead_next_change: datetime | None = None
    max_age_sec: int = 300

    @property
    def is_stale(self) -> bool:
        if self.as_of is None:
            return True
        age = datetime.now(UTC) - self.as_of
        return age > timedelta(seconds=self.max_age_sec)

    def free_margin(self, basis: str = "available_funds") -> Decimal | None:
        """Equity minus initial (available_funds) or maintenance (excess_liquidity)."""
        if basis == "excess_liquidity":
            return self.excess_liquidity
        return self.available_funds

    def look_ahead_free_margin(self, basis: str = "available_funds") -> Decimal | None:
        if basis == "excess_liquidity":
            return self.look_ahead_excess_liquidity
        return self.look_ahead_available_funds


class AccountMarginService:
    """Subscribes to reqAccountSummary on the shared TWSClient."""

    def __init__(
        self,
        client: TWSClient,
        *,
        rate_limiter: GatewayRateLimiter | None = None,
        max_age_sec: int | None = None,
    ) -> None:
        settings = get_settings()
        self._client = client
        self._rate_limiter = rate_limiter
        self._max_age_sec = (
            int(max_age_sec)
            if max_age_sec is not None
            else int(settings.margin_snapshot_max_age_sec)
        )
        self._lock = threading.Lock()
        self._next_req_id = _ACCOUNT_SUMMARY_REQ_START
        self._active_req_id: int | None = None
        self._pending: dict[str, dict[str, Any]] = {}
        self._snapshots: dict[str, AccountMarginSnapshot] = {}
        self._on_snapshot: list[Callable[[AccountMarginSnapshot], None]] = []
        self._started = False
        client.register_listener(self)

    def add_snapshot_listener(
        self, callback: Callable[[AccountMarginSnapshot], None]
    ) -> None:
        self._on_snapshot.append(callback)

    def start(self) -> None:
        """Issue reqAccountSummary once. IBKR re-pushes thereafter."""
        if self._started:
            return
        if not self._client.is_connected():
            logger.warning("AccountMarginService.start skipped: TWS not connected")
            return
        self._started = True
        self._request_summary(_GROUP_ALL)
        logger.info("AccountMarginService subscribed: group=%s tags=%s", _GROUP_ALL, MARGIN_TAGS)

    def stop(self) -> None:
        req_id = None
        with self._lock:
            req_id = self._active_req_id
            self._active_req_id = None
            self._started = False
        if req_id is not None:
            cancel = getattr(self._client, "cancelAccountSummary", None)
            if callable(cancel):
                try:
                    cancel(req_id)
                except Exception:
                    logger.exception("cancelAccountSummary failed req_id=%s", req_id)
            unregister = getattr(self._client, "unregister_request_id", None)
            if callable(unregister):
                unregister(req_id)

    def snapshot_for(self, ibkr_account: str | None) -> AccountMarginSnapshot | None:
        if not ibkr_account or not str(ibkr_account).strip():
            return None
        key = str(ibkr_account).strip().upper()
        with self._lock:
            return self._snapshots.get(key)

    def all_snapshots(self) -> dict[str, AccountMarginSnapshot]:
        with self._lock:
            return dict(self._snapshots)

    def on_account_summary(
        self, reqId: int, account: str, tag: str, value: str, currency: str
    ) -> None:
        key = str(account or "").strip().upper()
        if not key:
            return
        with self._lock:
            if self._active_req_id is not None and reqId != self._active_req_id:
                return
            row = self._pending.setdefault(
                key, {"currency": currency or None, "tags": {}}
            )
            if currency:
                row["currency"] = currency
            row["tags"][str(tag)] = value

    def on_account_summary_end(self, reqId: int) -> None:
        now = datetime.now(UTC)
        published: list[AccountMarginSnapshot] = []
        with self._lock:
            if self._active_req_id is not None and reqId != self._active_req_id:
                return
            pending = dict(self._pending)
            self._pending.clear()
            for account, row in pending.items():
                snap = self._row_to_snapshot(account, row, now)
                self._snapshots[account] = snap
                published.append(snap)
        for snap in published:
            logger.info(
                "Account margin snapshot: account=%s net_liq=%s available=%s excess=%s stale=%s",
                snap.ibkr_account,
                snap.net_liquidation,
                snap.available_funds,
                snap.excess_liquidity,
                snap.is_stale,
            )
            for callback in list(self._on_snapshot):
                try:
                    callback(snap)
                except Exception:
                    logger.exception(
                        "Account margin snapshot listener failed account=%s",
                        snap.ibkr_account,
                    )

    def on_connection_closed(self) -> None:
        with self._lock:
            self._snapshots.clear()
            self._pending.clear()
            self._active_req_id = None
            self._started = False
        logger.warning("AccountMarginService cache cleared: TWS connection closed")

    def on_error(self, reqId: int, errorCode: int, errorString: str) -> None:
        with self._lock:
            active = self._active_req_id
        if active is not None and reqId == active:
            logger.warning(
                "AccountSummary error req_id=%s code=%s msg=%s",
                reqId,
                errorCode,
                errorString,
            )

    def _request_summary(self, group: str) -> None:
        if self._rate_limiter is not None:
            self._rate_limiter.blocking_acquire(
                PRIORITY_DIAGNOSTIC, "reqAccountSummary"
            )
        with self._lock:
            req_id = self._next_req_id
            self._next_req_id += 1
            self._active_req_id = req_id
            self._pending.clear()
        register = getattr(self._client, "register_request_id", None)
        if callable(register):
            register(req_id, "account_summary")
        self._client.reqAccountSummary(req_id, group, MARGIN_TAGS)

    def _row_to_snapshot(
        self, account: str, row: dict[str, Any], as_of: datetime
    ) -> AccountMarginSnapshot:
        tags: dict[str, str] = row.get("tags") or {}
        fields: dict[str, Any] = {
            "ibkr_account": account,
            "currency": row.get("currency"),
            "as_of": as_of,
            "max_age_sec": self._max_age_sec,
        }
        for tag, field_name in _FIELD_BY_TAG.items():
            fields[field_name] = parse_ibkr_number(tags.get(tag))
        fields["look_ahead_next_change"] = parse_look_ahead_next_change(
            tags.get("LookAheadNextChange")
        )
        return AccountMarginSnapshot(**fields)
