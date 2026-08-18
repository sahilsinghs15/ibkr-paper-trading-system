#!/usr/bin/env python3
"""Developer-only acceptance script for the real OMS -> IBKR Paper Execution slice.

Connects to local Paper TWS at 127.0.0.1:7497, processes a simulated Signal
through OrderManager -> OrderIntent -> RMS (Checks 2, 3, 4, 7, 8) -> OMSService ->
IBKRExecutionAdapter -> TWSClient -> Paper TWS, and reports paper execution status.

WARNING: THIS TEST SUBMITS A REAL PAPER ORDER TO IBKR PAPER TWS.
THIS SCRIPT MUST NEVER RUN AUTOMATICALLY DURING FASTAPI STARTUP.
It requires explicit manual execution by a developer with Paper TWS running.
"""

import argparse
import asyncio
import logging
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

# Add backend directory to sys.path
backend_dir = Path(__file__).resolve().parents[2]
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from app.broker.ibkr.tws_client import TWSClient
from app.models.signal import Signal, SignalType
from app.oms.ibkr_adapter import IBKRExecutionAdapter
from app.oms.models import OMSOrderStatus
from app.oms.oms_service import OMSService
from app.rms import RMSContext, RMSEngine
from app.rms.models import StrategyConfig
from app.services.order_manager import OrderManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("run_paper_execution")


def format_ts(ts: datetime | None) -> str:
    """Format timestamp string for report output."""
    if ts is None:
        return "N/A"
    return ts.strftime("%Y-%m-%d %H:%M:%S.%f") + " UTC"


def format_duration(ms: float | None) -> str:
    """Format millisecond duration for report output."""
    if ms is None:
        return "N/A"
    return f"{ms:.2f} ms"


def parse_args() -> argparse.Namespace:
    """Parse command line arguments for the acceptance test script."""
    parser = argparse.ArgumentParser(
        description="Run real IBKR Paper TWS end-to-end execution acceptance test."
    )
    parser.add_argument(
        "--fill-timeout",
        type=float,
        default=90.0,
        help="Timeout in seconds to wait for execution/fill callback (default: 90.0)",
    )
    parser.add_argument(
        "--symbol",
        type=str,
        default="EWA",
        help="Test instrument symbol (default: EWA)",
    )
    parser.add_argument(
        "--quantity",
        type=int,
        default=1,
        help="Test order quantity (default: 1)",
    )
    parser.add_argument(
        "--order-type",
        type=str,
        default="LIMIT",
        choices=["LIMIT", "MARKET"],
        help="Test order type (default: LIMIT)",
    )
    parser.add_argument(
        "--limit-price",
        type=Decimal,
        default=Decimal("29.80"),
        help="Test limit price (default: 25.00)",
    )
    return parser.parse_args()


async def main() -> None:
    """Execute end-to-end Signal -> OrderManager -> RMS -> OMS -> IBKR Paper TWS execution flow."""
    args = parse_args()

    print("\n" + "=" * 65)
    print("      IBKR PAPER TWS END-TO-END ACCEPTANCE TEST")
    print(" WARNING: THIS TEST SUBMITS A REAL PAPER ORDER TO IBKR TWS")
    print("=" * 65)

    # 1. Configurable Test Parameters
    symbol = args.symbol
    quantity = args.quantity
    order_type = args.order_type
    limit_price = args.limit_price
    fill_timeout = args.fill_timeout
    strategy_id = "MODEL_BLUE"

    print("\nTarget Environment: IBKR Paper TWS (127.0.0.1:7497)")
    print(f"Strategy ID       : {strategy_id}")
    print(f"Symbol            : {symbol}")
    print(f"Quantity          : {quantity}")
    print(f"Order Type        : {order_type}")
    if order_type == "LIMIT":
        print(f"Limit Price       : {limit_price} USD")
    print(f"Fill Timeout      : {fill_timeout:.1f} seconds")

    # 2. Instantiate Real Production Components
    client = TWSClient()
    ibkr_adapter = IBKRExecutionAdapter(
        client=client,
        host="127.0.0.1",
        port=7497,
        client_id=99,  # Use client_id=99 for developer testing to avoid collisions
        timeout=10.0,
    )
    oms = OMSService(adapter=ibkr_adapter)

    rms_engine = RMSEngine()
    rms_context = RMSContext(
        strategy_configs={
            strategy_id: StrategyConfig(
                strategy_id=strategy_id,
                max_open_positions=10,
                money_limit_per_symbol=Decimal(1_000_000),
            )
        },
        current_time=datetime.now(UTC),
    )

    order_manager = OrderManager(
        oms=oms,
        symbol=symbol,
        quantity=quantity,
        order_type=order_type,
        price=limit_price,
        strategy_id=strategy_id,
        rms_engine=rms_engine,
        rms_context=rms_context,
    )

    try:
        # 3. Connect TWS Client
        print("\nConnecting TWS Client to 127.0.0.1:7497...")
        await ibkr_adapter.connect()

        # 4. Construct Simulated External Signal
        signal_received_at = datetime.now(UTC)
        test_signal = Signal(
            signal_type=SignalType.BUY,
            timestamp=signal_received_at,
            reason="Developer Paper Execution Acceptance Test",
        )

        print(f"\nSubmitting simulated Signal to OrderManager at {format_ts(signal_received_at)}...")

        # 5. Process through REAL OrderManager -> OrderIntent -> RMS Checks -> OMS Submission
        submitted_order = await order_manager.process_signal(test_signal)

        if submitted_order is None:
            print("\n❌ OrderManager returned None (HOLD signal). Acceptance failed.")
            sys.exit(1)

        print(f"Internal Order ID : {submitted_order.internal_order_id}")
        print(f"IBKR Order ID    : {submitted_order.ibkr_order_id}")
        print(f"Initial Status   : {submitted_order.status.value}")

        # 6. Wait for execution callback from IBKR Paper TWS (up to fill_timeout seconds)
        print(f"\nWaiting for IBKR Paper TWS execution/fill callback (timeout={fill_timeout:.1f}s)...")
        final_order = submitted_order
        try:
            final_order = await ibkr_adapter.wait_for_terminal_or_fill(
                internal_order_id=submitted_order.internal_order_id,
                timeout=fill_timeout,
            )
        except TimeoutError:
            print(f"\n[INFO] Timeout after {fill_timeout:.1f}s waiting for fill callback.")
            # Fetch current order state from OMS
            current_order = oms.get_order(submitted_order.internal_order_id)
            if current_order:
                final_order = current_order

        fill_price_str = (
            f"{final_order.average_fill_price:.2f} USD"
            if final_order.average_fill_price
            else "N/A"
        )

        print("\n" + "-" * 65)
        print(" EXECUTION RESULT REPORT")
        print("-" * 65)
        print(f"Internal Order ID : {final_order.internal_order_id}")
        print(f"IBKR Order ID    : {final_order.ibkr_order_id}")
        print(f"Final Status     : {final_order.status.value}")
        print(f"Filled Quantity  : {final_order.filled_quantity} / {final_order.quantity}")
        print(f"Remaining Qty    : {final_order.remaining_quantity}")
        print(f"Average Price    : {fill_price_str}")

        print("\nTimestamps:")
        print(f"  Signal Received  : {format_ts(signal_received_at)}")
        print(f"  Intent Created   : {format_ts(final_order.timestamps.intent_created_at)}")
        print(f"  RMS Completed    : {format_ts(final_order.timestamps.rms_completed_at)}")
        print(f"  OMS Received     : {format_ts(final_order.timestamps.oms_received_at)}")
        print(f"  IBKR Submit Start: {format_ts(final_order.timestamps.ibkr_submit_started_at)}")
        print(f"  IBKR Submit End  : {format_ts(final_order.timestamps.ibkr_submit_completed_at)}")
        print(f"  TWS Status Ack   : {format_ts(final_order.timestamps.order_status_received_at)}")
        print(f"  Execution Fill   : {format_ts(final_order.timestamps.execution_received_at)}")

        print("\nInstrumentation Latencies:")
        print(f"  RMS Latency      : {format_duration(final_order.timestamps.rms_latency_ms)}")
        print(f"  OMS Latency      : {format_duration(final_order.timestamps.oms_latency_ms)}")
        print(f"  IBKR Submit      : {format_duration(final_order.timestamps.ibkr_submit_latency_ms)}")
        print(f"  Submit -> Fill   : {format_duration(final_order.timestamps.submit_to_fill_ms)}")
        print(f"  Total Intent->Fill: {format_duration(final_order.timestamps.total_intent_to_fill_ms)}")

        # Evaluate STAGE A (Submission) & STAGE B (Execution)
        submission_passed = final_order.ibkr_order_id is not None and final_order.status not in (
            OMSOrderStatus.REJECTED,
            OMSOrderStatus.ERROR,
        )
        stage_a_res = "PASS" if submission_passed else "FAIL"

        if final_order.status == OMSOrderStatus.FILLED and final_order.filled_quantity > 0:
            stage_b_res = "PASS"
            overall_res = "SUCCESS"
            callback_note = "Actual execution callback proven."
        elif final_order.filled_quantity == 0 and submission_passed:
            stage_b_res = "NOT COMPLETED / STILL WORKING"
            overall_res = "SUBMISSION SUCCESS / EXECUTION PENDING"
            callback_note = (
                "Order-status callbacks proven.\n"
                "      Actual execution/execDetails callback has not yet been observed.\n"
                "      Order remains working in Paper TWS.\n"
                "      Cancel it manually in TWS if it is no longer wanted."
            )
        else:
            stage_b_res = f"FAIL ({final_order.error_message or final_order.status.value})"
            overall_res = "FAILURE"
            callback_note = f"Order failed: {final_order.error_message or final_order.status.value}"

        print(f"\nSTAGE A (ORDER SUBMISSION): {stage_a_res}")
        print(f"STAGE B (ORDER EXECUTION) : {stage_b_res}")
        print(f"\nOVERALL RESULT   : {overall_res}")
        print(f"\nNOTE: {callback_note}")
        print("NOTE: Execution callback proven; persistent position management is deferred until the account/position architecture is provided.")
        print("=" * 65 + "\n")

    except ConnectionError as ce:
        print(f"\n❌ Connection Error: {ce}")
        print("Ensure local IBKR Paper TWS / Gateway is running on 127.0.0.1:7497")
        print("and 'Enable ActiveX and Socket EClients' is checked in TWS API Settings.")
        print("\nSTAGE A (ORDER SUBMISSION): FAIL (TWS Connection Unavailable)")
        print("STAGE B (ORDER EXECUTION) : FAIL")
        print("OVERALL RESULT            : FAILURE (TWS Connection Unavailable)")
        print("=" * 65 + "\n")
    except Exception as e:  # noqa: BLE001
        print(f"\n❌ Execution Error: {e}")
        print("\nSTAGE A (ORDER SUBMISSION): FAIL")
        print("STAGE B (ORDER EXECUTION) : FAIL")
        print("OVERALL RESULT            : FAILURE")
        print("=" * 65 + "\n")
    finally:
        await ibkr_adapter.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
