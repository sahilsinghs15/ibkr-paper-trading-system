"""Read-only broker-vs-ledger reconcile data for the dashboard API."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.broker.ibkr.positions import BrokerPositionLine
from app.db.models.account import AccountModel
from app.db.models.instrument import InstrumentModel
from app.db.models.position import PositionModel
from app.db.repositories.broker_position_repository import BrokerPositionRepository
from app.schemas.reconcile_schemas import (
    BrokerPositionSnapshotRow,
    LedgerPositionRow,
    ReconcileDiffRow,
    ReconcilePositionsResponse,
    ReconcileRunSummary,
)
from app.services.position_reconciler import (
    build_ledger_net_lines,
    classify_reconcile_diffs,
    fetch_in_flight_accounts,
)


def _broker_line_from_row(row) -> BrokerPositionLine:
    return BrokerPositionLine(
        ibkr_account=row.ibkr_account,
        symbol=row.symbol,
        sec_type=row.sec_type,
        con_id=int(row.con_id),
        currency=row.currency,
        exchange=row.exchange or "",
        quantity=float(row.signed_qty),
        avg_cost=float(row.avg_cost),
    )


def _run_summary_from_model(run) -> ReconcileRunSummary:
    return ReconcileRunSummary(
        id=run.id,
        finished_at=run.finished_at,
        timed_out=run.timed_out,
        error=run.error,
        broker_line_count=run.broker_line_count,
        match_count=run.match_count,
        ghost_count=run.ghost_count,
        orphan_count=run.orphan_count,
        drift_count=run.drift_count,
        unmapped_account_count=run.unmapped_account_count,
    )


async def collect_reconcile_positions(
    session: AsyncSession,
    *,
    ibkr_account: str | None = None,
) -> ReconcilePositionsResponse:
    """Load latest broker snapshot, OPEN ledger rows, and fresh diff classification."""
    repo = BrokerPositionRepository(session)
    latest_run = await repo.get_latest_run()
    run_summary = _run_summary_from_model(latest_run) if latest_run is not None else None
    timed_out = latest_run.timed_out if latest_run is not None else False

    accounts = list((await session.execute(select(AccountModel))).scalars().all())
    ibkr_to_account = {acc.ibkr_account: acc.id for acc in accounts}
    account_to_ibkr = {acc.id: acc.ibkr_account for acc in accounts}

    target_account_id: int | None = None
    if ibkr_account is not None:
        target_account_id = ibkr_to_account.get(ibkr_account)
        if target_account_id is None:
            return ReconcilePositionsResponse(
                run=run_summary,
                broker_positions=[],
                ledger_positions=[],
                diffs=[],
            )

    broker_rows = await repo.list_snapshot(ibkr_account=ibkr_account)
    broker_lines = [_broker_line_from_row(row) for row in broker_rows]

    open_stmt = select(PositionModel).where(PositionModel.risk_state == "OPEN")
    if target_account_id is not None:
        open_stmt = open_stmt.where(PositionModel.account_id == target_account_id)
    open_rows = list((await session.execute(open_stmt)).scalars().all())

    instruments = list((await session.execute(select(InstrumentModel))).scalars().all())
    in_flight_accounts = await fetch_in_flight_accounts(session)

    ledger_lines = build_ledger_net_lines(open_rows, instruments)
    diffs = classify_reconcile_diffs(
        broker_lines=broker_lines,
        ledger_lines=ledger_lines,
        ibkr_to_account=ibkr_to_account,
        timed_out=timed_out,
        in_flight_accounts=in_flight_accounts,
    )

    broker_positions = [
        BrokerPositionSnapshotRow(
            ibkr_account=row.ibkr_account,
            con_id=int(row.con_id),
            account_id=row.account_id,
            symbol=row.symbol,
            sec_type=row.sec_type,
            currency=row.currency,
            exchange=row.exchange or "",
            signed_qty=float(row.signed_qty),
            avg_cost=float(row.avg_cost),
            as_of=row.as_of,
        )
        for row in broker_rows
    ]

    ledger_positions = [
        LedgerPositionRow(
            account_id=row.account_id,
            ibkr_account=account_to_ibkr.get(row.account_id),
            trade_id=row.trade_id,
            strategy_id=row.strategy_id,
            leg_a_symbol=row.leg_a_symbol,
            leg_a_signed_qty=float(row.leg_a_signed_qty),
            leg_a_instrument_type=row.leg_a_instrument_type,
            leg_b_symbol=row.leg_b_symbol,
            leg_b_signed_qty=float(row.leg_b_signed_qty) if row.leg_b_signed_qty is not None else None,
            leg_b_instrument_type=row.leg_b_instrument_type,
            risk_state=row.risk_state,
        )
        for row in open_rows
    ]

    diff_rows = [
        ReconcileDiffRow(
            kind=diff.kind,
            ibkr_account=diff.ibkr_account,
            account_id=diff.account_id,
            symbol=diff.symbol,
            sec_type=diff.sec_type,
            con_id=diff.con_id,
            broker_qty=diff.broker_qty,
            ledger_qty=diff.ledger_qty,
            in_flight=diff.in_flight,
        )
        for diff in diffs
    ]

    return ReconcilePositionsResponse(
        run=run_summary,
        broker_positions=broker_positions,
        ledger_positions=ledger_positions,
        diffs=diff_rows,
    )
