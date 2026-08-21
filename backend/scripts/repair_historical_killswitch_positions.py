"""One-time administrative repair script for historical Kill Switch stale positions."""

import argparse
import asyncio
import logging
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.order import OrderModel
from app.db.models.position import PositionModel
from app.db.repositories.event_repository import EventRepository
from app.db.repositories.order_repository import OrderRepository
from app.db.repositories.position_repository import PositionRepository
from app.db.session import AsyncSessionLocal, engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("repair_script")


def _norm_sym(sym: str | None) -> str:
    if not sym:
        return ""
    return sym.split(":")[-1].strip().upper()


def _calculate_cumulative_fills(
    pos: PositionModel, orders: list[OrderModel]
) -> tuple[Decimal, Decimal, list[OrderModel]]:
    """Calculate cumulative fills per leg for a position from matching filled close orders."""
    leg_a_fill = Decimal(0)
    leg_b_fill = Decimal(0)
    used_orders: list[OrderModel] = []

    pos_leg_a = _norm_sym(pos.leg_a_symbol)
    pos_leg_b = _norm_sym(pos.leg_b_symbol)

    for o in orders:
        internal_id = (o.internal_order_id or "").upper()
        # Match Kill Switch or CLOSE orders
        if not ("KILLSWITCH-" in internal_id or ":CLOSE" in internal_id or "KILLSWITCH" in internal_id):
            continue

        if (o.status or "").upper() != "FILLED":
            continue

        fill_qty = Decimal(str(o.fill_qty or o.quantity or 0))
        if fill_qty <= 0:
            continue

        order_sym = _norm_sym(o.symbol)

        if order_sym == pos_leg_a:
            leg_a_fill += fill_qty
            used_orders.append(o)
        elif pos_leg_b and order_sym == pos_leg_b:
            leg_b_fill += fill_qty
            used_orders.append(o)

    return leg_a_fill, leg_b_fill, used_orders


async def audit_and_repair_positions(
    session_factory: async_sessionmaker[AsyncSession],
    account_id: int = 7,
    apply_changes: bool = False,
) -> dict[str, Any]:
    """Audit open positions for account_id and repair fully-flat positions if apply_changes=True."""
    report_rows: list[dict[str, Any]] = []
    eligible_count = 0
    already_closed_count = 0
    rejected_count = 0

    async with session_factory() as session:
        pos_repo = PositionRepository(session)
        open_positions = await pos_repo.list_open()
        account_positions = [p for p in open_positions if p.account_id == account_id]

        res_orders = await session.execute(
            select(OrderModel).where(OrderModel.account_id == account_id)
        )
        all_account_orders = list(res_orders.scalars().all())

        for pos in account_positions:
            orders = [
                o for o in all_account_orders
                if (o.trade_id and pos.trade_id in o.trade_id) or (o.internal_order_id and pos.trade_id in o.internal_order_id)
            ]
            leg_a_req = abs(pos.leg_a_signed_qty)
            leg_b_req = abs(pos.leg_b_signed_qty) if pos.leg_b_signed_qty is not None else Decimal(0)

            leg_a_fill, leg_b_fill, used_orders = _calculate_cumulative_fills(pos, orders)

            rem_a = max(Decimal(0), leg_a_req - leg_a_fill)
            rem_b = max(Decimal(0), leg_b_req - leg_b_fill)
            net_rem = rem_a + rem_b

            is_eligible = (
                leg_a_fill >= leg_a_req
                and (pos.leg_b_symbol is None or leg_b_fill >= leg_b_req)
                and net_rem == Decimal(0)
                and len(used_orders) > 0
            )

            reason = "OK_FULLY_FLAT" if is_eligible else "INSUFFICIENT_CLOSE_FILLS"
            if is_eligible:
                eligible_count += 1
            else:
                rejected_count += 1

            report_rows.append({
                "trade_id": pos.trade_id,
                "leg_a": f"{pos.leg_a_symbol} ({leg_a_req} req / {leg_a_fill} fill)",
                "leg_b": f"{pos.leg_b_symbol} ({leg_b_req} req / {leg_b_fill} fill)" if pos.leg_b_symbol else "N/A",
                "remaining_exposure": str(net_rem),
                "close_orders": [o.internal_order_id for o in used_orders],
                "eligible": is_eligible,
                "reason": reason,
                "pos_obj": pos,
                "used_orders": used_orders,
            })

    db_writes = 0
    if apply_changes and eligible_count > 0:
        async with session_factory() as session, session.begin():
            pos_repo = PositionRepository(session)
            event_repo = EventRepository(session)

            for row in report_rows:
                if not row["eligible"]:
                    continue

                pos = row["pos_obj"]
                used_orders = row["used_orders"]

                # Exit marks from actual fill prices
                exit_marks = {}
                for o in used_orders:
                    if o.fill_price is not None:
                        exit_marks[o.symbol] = Decimal(str(o.fill_price))

                # Repair position via close_trade
                await pos_repo.close_trade(
                    pos.trade_id,
                    account_id=account_id,
                    exit_marks=exit_marks,
                )

                # Record POSITION_CLOSE idempotently
                idempotency_key = f"position_close:repair:{account_id}:{pos.trade_id}"
                await event_repo.append(
                    process="position",
                    kind="POSITION_CLOSE",
                    detail={
                        "account_id": account_id,
                        "trade_id": pos.trade_id,
                        "source": "HISTORICAL_REPAIR_SCRIPT",
                    },
                    idempotency_key=idempotency_key,
                )
                db_writes += 1

    return {
        "account_id": account_id,
        "apply_mode": apply_changes,
        "eligible_count": eligible_count,
        "already_closed_count": already_closed_count,
        "rejected_count": rejected_count,
        "db_writes": db_writes,
        "rows": report_rows,
    }


def print_report(res: dict[str, Any]) -> None:
    """Print clean ASCII report of audit results."""
    mode_str = "APPLY MODE (WRITING TO DB)" if res["apply_mode"] else "DRY RUN — NO DATABASE CHANGES MADE"
    print("\n" + "=" * 70)
    print(f"HISTORICAL KILL SWITCH REPAIR — {mode_str}")
    print(f"Account: {res['account_id']}")
    print("=" * 70)
    print(f"{'Trade ID':<45} | {'Eligible':<9} | {'Remaining Exp':<13} | {'Reason'}")
    print("-" * 70)
    for r in res["rows"]:
        status_str = "YES" if r["eligible"] else "NO"
        print(f"{r['trade_id']:<45} | {status_str:<9} | {r['remaining_exposure']:<13} | {r['reason']}")
    print("=" * 70)
    print(f"Eligible for repair : {res['eligible_count']}")
    print(f"Rejected            : {res['rejected_count']}")
    print(f"Already closed      : {res['already_closed_count']}")
    print(f"Database writes     : {res['db_writes']}")
    print("=" * 70)
    if not res["apply_mode"]:
        print(">>> DRY RUN COMPLETE — ZERO DATABASE MODIFICATIONS PERFORMED <<<\n")
    else:
        print(">>> APPLY COMPLETE — DATABASE REPAIR SUCCESSFULLY COMMITTED <<<\n")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Repair historical Kill Switch stale positions.")
    parser.add_argument("--account-id", type=int, default=7, help="Account ID to audit/repair (default: 7)")
    parser.add_argument("--apply", action="store_true", help="Execute DB modifications (default: False / DRY-RUN)")
    args = parser.parse_args()

    res = await audit_and_repair_positions(AsyncSessionLocal, account_id=args.account_id, apply_changes=args.apply)
    print_report(res)
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
