"""Repair mismatched manual executions where contract/symbol/perm mismatch caused filled > qty.

READ-ONLY by default. Use --execute to actually delete and recalc.

For production: manual order MAN_6D SMH BUY10 filled 30 via KIE 20@63, etc.
"""
import argparse
import asyncio
from decimal import Decimal
from sqlalchemy import select, text, func
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from app.core.config import get_settings

async def find_mismatched(session: AsyncSession):
    rows = (await session.execute(text("""
        SELECT me.id as me_id, me.exec_id, me.quantity as me_qty, me.price as me_price, me.broker_order_id as me_broker,
               mo.id as mo_id, mo.internal_order_id, mo.trade_id, mo.symbol as mo_symbol, mo.con_id as mo_con, mo.perm_id as mo_perm, mo.side as mo_side, mo.quantity as mo_qty,
               te.symbol as te_symbol, te.con_id as te_con, te.side as te_side, te.quantity as te_qty, te.price as te_price, te.broker_order_id as te_broker, te.perm_id as te_perm
        FROM manual_executions me
        JOIN manual_orders mo ON me.manual_order_id = mo.id
        LEFT JOIN trade_executions te ON me.exec_id = te.exec_id
        ORDER BY me.id DESC
    """))).mappings().all()
    mismatched = []
    for r in rows:
        d=dict(r)
        # Only check where trade execution exists and symbols differ, or con_id differs, or perm differs and con mismatch
        if d['te_symbol'] is None:
            continue
        # Check symbol mismatch
        if d['mo_symbol'] and d['te_symbol'] and d['mo_symbol'].strip().upper() != d['te_symbol'].strip().upper():
            mismatched.append(d)
        elif d['mo_con'] and d['te_con'] and int(d['mo_con']) != int(d['te_con']):
            mismatched.append(d)
    return mismatched

async def find_filled_over_qty(session: AsyncSession):
    rows = (await session.execute(text("""
        SELECT mo.id, mo.internal_order_id, mo.symbol, mo.side, mo.quantity as qty, mo.trade_id, SUM(me.quantity) as filled
        FROM manual_orders mo
        LEFT JOIN manual_executions me ON me.manual_order_id = mo.id
        GROUP BY mo.id
        HAVING SUM(me.quantity) > mo.quantity
    """))).mappings().all()
    return rows

async def recalc_position_for_trade(session: AsyncSession, account_id: int, trade_id: str):
    from app.db.models.manual_order import ManualPositionModel, ManualExecutionModel, ManualOrderModel
    # Get all remaining manual executions for this trade_id
    # Find orders with this trade_id
    orders = (await session.execute(select(ManualOrderModel).where(ManualOrderModel.account_id==account_id, ManualOrderModel.trade_id==trade_id))).scalars().all()
    if not orders:
        return
    # Get all executions for those orders
    order_ids = [o.id for o in orders]
    execs = (await session.execute(select(ManualExecutionModel).where(ManualExecutionModel.manual_order_id.in_(order_ids)).order_by(ManualExecutionModel.id))).scalars().all()
    # Recompute position from scratch using same logic as apply_execution
    # Fetch or create position
    from app.db.repositories.manual_repository import ManualPositionRepository
    # Delete existing position and recreate via apply logic is complex; simpler: reset position and re-apply
    # We'll delete the position row and then re-apply each exec sequentially via repo
    # But to avoid FK issues, we will directly update the position row
    pos = (await session.execute(select(ManualPositionModel).where(ManualPositionModel.account_id==account_id, ManualPositionModel.trade_id==trade_id))).scalar_one_or_none()
    if not pos:
        return
    # Reset
    pos.signed_qty = Decimal(0)
    pos.avg_cost = Decimal(0)
    pos.realized_pnl = Decimal(0)
    pos.status = "OPEN"
    pos.closed_at = None
    await session.flush()
    # Re-apply each exec
    repo = ManualPositionRepository(session)
    # We need to temporarily delete and recreate? Instead we will manually compute as repo would
    # Use the same logic: for each exec, find order to get side, but we have exec's side via order? Actually exec's side is determined via order side and execution side
    # For manual, exec side is same as order side (BUY/SELL). We can use order.side
    # But we need to handle the case where exec quantity is for that order's side
    # Simplify: For each exec, get its order's side and apply
    for exec_row in execs:
        order = next((o for o in orders if o.id == exec_row.manual_order_id), None)
        if not order:
            continue
        # Determine side: use order.side (BUY/SELL) and exec quantity
        # The manual execution's side is order.side, but we need to pass side to apply_execution as order.side
        # However apply_execution expects side param to determine direction, and it will use order's side
        # We can call apply_execution for each exec with that order's side
        # But apply_execution expects to find position by trade_id and apply; we have already reset pos, so we can re-apply
        # Instead of calling apply_execution (which would try to create new position if not exists), we can manually compute
        pass
    # For now, just recompute via simple net
    # This function will be expanded for production repair

async def main(execute: bool):
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    sf = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)
    async with sf() as sess:
        mismatched = await find_mismatched(sess)
        print(f"Found {len(mismatched)} mismatched manual executions (symbol/con_id mismatch):")
        for m in mismatched:
            print(f"  me_id={m['me_id']} exec={m['exec_id']} mo {m['mo_symbol']}({m['mo_con']}) vs te {m['te_symbol']}({m['te_con']}) qty mo {m['mo_qty']} me {m['me_qty']} te {m['te_qty']} price {m['me_price']} perm mo {m['mo_perm']} te {m['te_perm']}")

        over = await find_filled_over_qty(sess)
        print(f"\nOrders with filled > qty: {len(over)}")
        for o in over:
            print(f"  id={o['id']} {o['internal_order_id']} {o['symbol']} {o['side']} qty={o['qty']} filled={o['filled']} trade_id={o['trade_id']}")

        if not execute:
            print("\nDry run - no changes. Use --execute to repair.")
            return

        # Repair: delete mismatched manual_executions where symbol mismatch
        # For SMH case, delete me_id 9 (KIE 20@63)
        # For other AAPL cases, delete similarly
        # We will delete only those where mo_symbol != te_symbol
        for m in mismatched:
            if m['mo_symbol'].strip().upper() != m['te_symbol'].strip().upper():
                me_id = m['me_id']
                print(f"Deleting mismatched manual_execution id={me_id} exec={m['exec_id']}")
                await sess.execute(text("DELETE FROM manual_executions WHERE id=:id"), {"id": me_id})
                # Also need to recalc position for that trade_id
                # Find trade_id and account
                trade_id = m['trade_id']
                # Find account_id for that order
                from sqlalchemy import select as sel
                from app.db.models.manual_order import ManualOrderModel
                order = (await sess.execute(sel(ManualOrderModel).where(ManualOrderModel.id==m['mo_id']))).scalar_one_or_none()
                if order:
                    # Recalc position by resetting and re-applying remaining execs
                    from app.db.models.manual_order import ManualPositionModel
                    pos = (await sess.execute(select(ManualPositionModel).where(ManualPositionModel.account_id==order.account_id, ManualPositionModel.trade_id==trade_id))).scalar_one_or_none()
                    if pos:
                        # Get remaining execs for this trade_id with commission
                        remaining = (await sess.execute(text("SELECT me.quantity, me.price, me.commission, mo.side FROM manual_executions me JOIN manual_orders mo ON me.manual_order_id=mo.id WHERE mo.trade_id=:tid AND mo.account_id=:acc ORDER BY me.id"), {"tid": trade_id, "acc": order.account_id})).mappings().all()
                        print(f"  Remaining execs for {trade_id}: {len(remaining)}")
                        from decimal import Decimal as D
                        signed_qty = D(0)
                        avg_cost = D(0)
                        realized = D(0)
                        for r in remaining:
                            qty = D(str(r['quantity']))
                            price = D(str(r['price']))
                            comm = D(str(r['commission'] or 0))
                            side = r['side']
                            d = qty if side.upper() in ('BUY','BOT') else -qty
                            if signed_qty == 0:
                                signed_qty = d
                                avg_cost = price
                                realized -= comm
                            else:
                                same_dir = (signed_qty >0 and d>0) or (signed_qty <0 and d<0)
                                if same_dir:
                                    new_qty = signed_qty + d
                                    abs_curr = abs(signed_qty)
                                    abs_exec = qty
                                    abs_new = abs(new_qty)
                                    new_avg = ((abs_curr * avg_cost) + (abs_exec * price)) / abs_new
                                    signed_qty = new_qty
                                    avg_cost = new_avg
                                    realized -= comm
                                else:
                                    abs_curr = abs(signed_qty)
                                    close_qty = min(abs_curr, qty)
                                    if signed_qty >0:
                                        gross = close_qty * (price - avg_cost)
                                    else:
                                        gross = close_qty * (avg_cost - price)
                                    realized += gross - comm
                                    if abs_curr > qty:
                                        signed_qty = signed_qty + d
                                    elif abs_curr == qty:
                                        signed_qty = D(0)
                                    else:
                                        flip_qty = qty - abs_curr
                                        new_sign = D(1) if d>0 else D(-1)
                                        signed_qty = new_sign * flip_qty
                                        avg_cost = price
                        pos.signed_qty = signed_qty
                        if signed_qty == 0:
                            pos.status = "CLOSED"
                            from datetime import datetime, UTC
                            pos.closed_at = datetime.now(UTC)
                        else:
                            pos.status = "OPEN"
                            pos.closed_at = None
                            pos.avg_cost = avg_cost
                        pos.realized_pnl = realized
                        if signed_qty !=0:
                            pos.avg_cost = avg_cost
                        print(f"  Recalculated pos {trade_id} qty={pos.signed_qty} avg={pos.avg_cost} pnl={pos.realized_pnl} status={pos.status}")
                        await sess.flush()
        await sess.commit()
        print("Repair committed")

    await engine.dispose()

if __name__ == "__main__":
    import sys
    execute = "--execute" in sys.argv
    import asyncio
    asyncio.run(main(execute))
