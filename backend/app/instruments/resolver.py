"""Map requested signal instrument_type + optional master row to an IBKR contract.

Explicit IBKR secType policy (requested → IBKR):
    STK → STK   (unless temporary paper demo override maps STK → CFD)
    ETF → STK   (cash ETF is STK at IBKR; not a silent product change to CFD)
    CFD → CFD   (non-demo: requires instruments master row + trade_conid)

Temporary STK_TO_CFD_DEMO path: symbol + secType=CFD + SMART/USD defaults.
Does not require an instruments row and does not invent conId.

Never falls back CFD → STK.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import replace
from decimal import ROUND_DOWN, Decimal
from typing import Protocol

from app.instruments.execution_override import (
    STK_TO_CFD_DEMO,
    execution_instrument_type,
    paper_execute_stk_as_cfd_enabled,
)
from app.instruments.models import (
    InstrumentRecord,
    InstrumentResolutionError,
    ResolvedInstrument,
)

logger = logging.getLogger(__name__)

# Requested payload type → IBKR Contract.secType
_EXPLICIT_SEC_TYPE: dict[str, str] = {
    "STK": "STK",
    "ETF": "STK",
    "CFD": "CFD",
}

_TYPES_REQUIRING_MASTER = frozenset({"CFD"})
_EXPIRY_SEC_TYPES = frozenset({"FUT", "FOP", "OPT"})
_DEFAULT_STK_EXCHANGE = "SMART"
_DEFAULT_STK_CURRENCY = "USD"

def apply_size_increment(quantity: Decimal, increment: Decimal) -> Decimal:
    """Round quantity down to a whole number of IBKR size increments.

    ``Decimal('1.00000000')`` from Numeric(18,8) must still yield whole lots, not 8 d.p.
    """
    if increment <= 0:
        raise ValueError("size_increment must be positive")
    lots = (quantity / increment).to_integral_value(rounding=ROUND_DOWN)
    return lots * increment


def ibkr_contract_from_resolved(resolved: ResolvedInstrument):
    """Build an ibapi Contract from a ResolvedInstrument. Does not guess secType."""
    from ibapi.contract import Contract  # type: ignore[import-untyped]

    contract = Contract()
    contract.symbol = resolved.symbol
    contract.secType = resolved.sec_type
    contract.exchange = resolved.exchange
    contract.currency = resolved.currency
    if resolved.con_id:
        contract.conId = resolved.con_id
    if resolved.primary_exchange:
        contract.primaryExchange = resolved.primary_exchange
    if resolved.multiplier is not None and resolved.multiplier != Decimal(1):
        contract.multiplier = str(resolved.multiplier)
    return contract


def ibkr_market_data_contract_from_resolved(resolved: ResolvedInstrument):
    """Build IBKR Contract for market-data subscribe (prefers market_data_con_id)."""
    contract = ibkr_contract_from_resolved(resolved)
    md_con = resolved.market_data_con_id or resolved.con_id
    if md_con:
        contract.conId = md_con
    return contract


class InstrumentCatalog(Protocol):
    """Lookup instruments by symbol and IBKR sec_type. Does not invent rows."""

    def find_all(self, symbol: str, sec_type: str) -> Sequence[InstrumentRecord]: ...


class EmptyInstrumentCatalog:
    def find_all(self, symbol: str, sec_type: str) -> Sequence[InstrumentRecord]:
        return ()


class InMemoryInstrumentCatalog:
    def __init__(self, rows: Sequence[InstrumentRecord] | None = None) -> None:
        self._rows = list(rows or [])

    def find_all(self, symbol: str, sec_type: str) -> Sequence[InstrumentRecord]:
        wanted_sym = symbol.strip().upper()
        wanted_sec = sec_type.strip().upper()
        return [
            row
            for row in self._rows
            if row.symbol.strip().upper() == wanted_sym
            and row.sec_type.strip().upper() == wanted_sec
        ]


def ibkr_sec_type(requested: str | None) -> str:
    raw = (requested or "").strip()
    if not raw:
        raise InstrumentResolutionError(
            "MISSING_INSTRUMENT_TYPE: each leg requires instrument_type from the signal."
        )
    mapped = _EXPLICIT_SEC_TYPE.get(raw.upper())
    if mapped is None:
        raise InstrumentResolutionError(
            f"UNSUPPORTED_INSTRUMENT_TYPE: '{raw}' is not executable. "
            "Supported requested types: STK, ETF, CFD."
        )
    return mapped


def is_expiry_instrument(instrument_type: str | None) -> bool:
    raw = (instrument_type or "").strip().upper()
    if raw in _EXPIRY_SEC_TYPES:
        return True
    return ibkr_sec_type_or_none(raw) in _EXPIRY_SEC_TYPES


def ibkr_sec_type_or_none(requested: str | None) -> str | None:
    raw = (requested or "").strip().upper()
    if not raw:
        return None
    return _EXPLICIT_SEC_TYPE.get(raw, raw)


def resolve_leg(
    *,
    symbol: str,
    instrument_type: str | None,
    market: str | None = None,
    currency: str | None = None,
    con_id: int | None = None,
    catalog: InstrumentCatalog | None = None,
    apply_demo_override: bool | None = None,
) -> ResolvedInstrument:
    """Resolve one leg. ``instrument_type`` is the signal's requested product."""
    symbol_clean = (symbol or "").strip()
    if not symbol_clean:
        raise InstrumentResolutionError("MISSING_SYMBOL: cannot resolve a contract without a symbol.")

    requested = (instrument_type or "").strip()
    demo_on = (
        paper_execute_stk_as_cfd_enabled()
        if apply_demo_override is None
        else apply_demo_override
    )
    execution_type, override = execution_instrument_type(
        requested, enabled=demo_on
    )
    sec_type = ibkr_sec_type(execution_type)

    if demo_on and sec_type == "CFD":
        if requested.upper() == "STK" and override != STK_TO_CFD_DEMO:
            raise InstrumentResolutionError(
                "STK_TO_CFD_DEMO: requested STK did not map to CFD; "
                "refusing to submit an STK contract."
            )
        catalog = catalog or EmptyInstrumentCatalog()
        matches = list(catalog.find_all(symbol_clean, "CFD"))
        if len(matches) > 1:
            raise InstrumentResolutionError(
                f"AMBIGUOUS_INSTRUMENT: {len(matches)} instruments rows for "
                f"{symbol_clean}/CFD."
            )
        if len(matches) == 1:
            resolved = _resolve_cfd(
                symbol=symbol_clean,
                requested=requested,
                sec_type=sec_type,
                market=market,
                currency=currency,
                con_id=con_id,
                record=matches[0],
            )
        else:
            resolved = _resolve_demo_cfd(
                symbol=symbol_clean,
                requested=requested,
                market=market,
                currency=currency,
                con_id=con_id,
            )
        if resolved.sec_type != "CFD":
            raise InstrumentResolutionError(
                "STK_TO_CFD_DEMO: resolved sec_type is not CFD; "
                "refusing to submit an STK contract."
            )
    else:
        catalog = catalog or EmptyInstrumentCatalog()
        matches = list(catalog.find_all(symbol_clean, sec_type))
        if len(matches) > 1:
            raise InstrumentResolutionError(
                f"AMBIGUOUS_INSTRUMENT: {len(matches)} instruments rows for "
                f"{symbol_clean}/{sec_type}."
            )
        record = matches[0] if matches else None

        if sec_type in _TYPES_REQUIRING_MASTER:
            resolved = _resolve_cfd(
                symbol=symbol_clean,
                requested=requested,
                sec_type=sec_type,
                market=market,
                currency=currency,
                con_id=con_id,
                record=record,
            )
        else:
            resolved = _resolve_stk(
                symbol=symbol_clean,
                requested=requested,
                sec_type=sec_type,
                market=market,
                currency=currency,
                con_id=con_id,
                record=record,
            )
    logger.info(
        "Model Blue instrument resolution: symbol=%s requested_type=%s "
        "resolved_type=%s override=%s con_id=%s exchange=%s",
        resolved.symbol,
        resolved.requested_instrument_type,
        resolved.sec_type,
        override or "none",
        resolved.con_id,
        resolved.exchange,
    )
    return resolved


def _resolve_stk(
    *,
    symbol: str,
    requested: str,
    sec_type: str,
    market: str | None,
    currency: str | None,
    con_id: int | None,
    record: InstrumentRecord | None,
) -> ResolvedInstrument:
    # Signal market/currency win over master and over STK defaults.
    exchange = (market or "").strip() or (
        record.exchange.strip() if record is not None and record.exchange else ""
    ) or _DEFAULT_STK_EXCHANGE
    ccy = (currency or "").strip() or (
        record.currency.strip() if record is not None and record.currency else ""
    ) or _DEFAULT_STK_CURRENCY
    resolved_con = _optional_con_id(con_id)
    if resolved_con is None and record is not None:
        resolved_con = _optional_con_id(record.trade_conid)
    md_con = record.market_data_conid if record is not None else None
    multiplier = record.multiplier if record is not None else Decimal(1)
    primary = record.underlying_exchange if record is not None else None
    return ResolvedInstrument(
        symbol=symbol,
        requested_instrument_type=requested.upper(),
        sec_type=sec_type,
        exchange=exchange,
        currency=ccy,
        con_id=resolved_con,
        market_data_con_id=_optional_con_id(md_con),
        multiplier=multiplier if multiplier is not None else Decimal(1),
        primary_exchange=(primary or None),
        size_increment=record.size_increment if record is not None else None,
    )


def _resolve_demo_cfd(
    *,
    symbol: str,
    requested: str,
    market: str | None,
    currency: str | None,
    con_id: int | None,
) -> ResolvedInstrument:
    """Temporary paper path: IBKR CFD from symbol + secType, no catalog/conId."""
    exchange = (market or "").strip() or _DEFAULT_STK_EXCHANGE
    ccy = (currency or "").strip() or _DEFAULT_STK_CURRENCY
    if not exchange or not ccy:
        raise InstrumentResolutionError(
            f"INSTRUMENT_RESOLUTION_FAILED: CFD {symbol} needs exchange and currency; "
            "refusing to submit an STK contract."
        )
    return ResolvedInstrument(
        symbol=symbol,
        requested_instrument_type=requested.upper(),
        sec_type="CFD",
        exchange=exchange,
        currency=ccy,
        con_id=_optional_con_id(con_id),
        market_data_con_id=None,
        multiplier=Decimal(1),
        primary_exchange=None,
        size_increment=None,
    )


def _resolve_cfd(
    *,
    symbol: str,
    requested: str,
    sec_type: str,
    market: str | None,
    currency: str | None,
    con_id: int | None,
    record: InstrumentRecord | None,
) -> ResolvedInstrument:
    if record is None:
        raise InstrumentResolutionError(
            "INSTRUMENT_METADATA_MISSING: CFD requires an instruments row with "
            f"symbol={symbol!r} and sec_type='CFD' including a positive trade_conid. "
            "Refusing to submit an STK contract."
        )
    if record.sec_type.strip().upper() != "CFD":
        raise InstrumentResolutionError(
            f"INSTRUMENT_METADATA_MISSING: instruments row for {symbol} is "
            f"sec_type={record.sec_type!r}, not CFD. Refusing STK fallback."
        )
    resolved_con = _require_con_id(con_id if con_id else record.trade_conid, symbol)
    exchange = (market or "").strip() or (record.exchange or "").strip()
    ccy = (currency or "").strip() or (record.currency or "").strip()
    if not exchange or not ccy:
        raise InstrumentResolutionError(
            f"INSTRUMENT_METADATA_MISSING: CFD {symbol} row needs exchange and currency."
        )
    multiplier = record.multiplier if record.multiplier is not None else Decimal(1)
    return ResolvedInstrument(
        symbol=symbol,
        requested_instrument_type=requested.upper(),
        sec_type="CFD",
        exchange=exchange,
        currency=ccy,
        con_id=resolved_con,
        market_data_con_id=_optional_con_id(record.market_data_conid),
        multiplier=multiplier,
        primary_exchange=record.underlying_exchange or None,
        size_increment=record.size_increment,
    )


def _optional_con_id(raw: int | None) -> int | None:
    if raw is None:
        return None
    value = int(raw)
    if value <= 0:
        return None
    return value


def _require_con_id(raw: int | None, symbol: str) -> int:
    value = _optional_con_id(raw)
    if value is None:
        raise InstrumentResolutionError(
            f"INVALID_CONID: CFD {symbol} requires a positive trade_conid; "
            "conId was missing or non-positive. Refusing to invent one."
        )
    return value


def attach_resolved(
    intent,
    catalog: InstrumentCatalog | None = None,
    *,
    apply_demo_override: bool | None = None,
):
    """Resolve every leg or raise. Does not submit. Used before basket/OMS."""
    from app.rms.models import OrderIntent, OrderLeg

    if not isinstance(intent, OrderIntent):
        raise TypeError("attach_resolved expects an OrderIntent")
    new_legs: list[OrderLeg] = []
    failures: list[str] = []
    for index, leg in enumerate(intent.legs):
        try:
            resolved = leg.resolved or resolve_leg(
                symbol=leg.symbol,
                instrument_type=leg.instrument_type,
                market=intent.market or leg.exchange,
                currency=leg.currency,
                con_id=leg.con_id,
                catalog=catalog,
                apply_demo_override=apply_demo_override,
            )
            qty = Decimal(str(leg.quantity))
            increment = resolved.size_increment
            if increment is not None and increment > 0:
                qty = apply_size_increment(qty, increment)
                if qty <= 0:
                    failures.append(
                        f"L{index} {leg.symbol}: quantity {leg.quantity} quantized "
                        f"to {qty} using size_increment={increment}"
                    )
                    continue
            new_legs.append(
                replace(
                    leg,
                    quantity=float(qty),
                    resolved=resolved,
                    con_id=resolved.con_id if resolved.con_id is not None else leg.con_id,
                    exchange=resolved.exchange,
                    currency=resolved.currency,
                    instrument_type=leg.instrument_type or resolved.requested_instrument_type,
                )
            )
        except InstrumentResolutionError as exc:
            failures.append(f"L{index} {leg.symbol}: {exc}")
    if failures:
        raise InstrumentResolutionError(
            "INSTRUMENT_RESOLUTION_FAILED: " + "; ".join(failures)
        )
    return replace(intent, legs=new_legs)
