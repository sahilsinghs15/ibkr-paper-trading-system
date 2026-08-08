"""Tests for the MockBroker implementation."""

from decimal import Decimal

import pytest
import pytest_asyncio

from app.broker.mock_broker import BrokerDisconnectedError, MockBroker
from app.models.broker import BrokerStatus
from app.models.order import OrderSide, OrderStatus


@pytest.fixture
def broker() -> MockBroker:
    """Return a fresh MockBroker instance."""
    return MockBroker()


@pytest_asyncio.fixture
async def connected_broker(broker: MockBroker) -> MockBroker:
    """Return a MockBroker that has already logged in."""
    await broker.login()
    return broker


# ── Connection lifecycle ────────────────────────────────────────────


class TestConnection:
    def test_initial_status_is_disconnected(self, broker: MockBroker) -> None:
        assert broker.status == BrokerStatus.DISCONNECTED

    @pytest.mark.asyncio
    async def test_login_sets_connected(self, broker: MockBroker) -> None:
        await broker.login()
        assert broker.status == BrokerStatus.CONNECTED

    @pytest.mark.asyncio
    async def test_disconnect_sets_disconnected(
        self, connected_broker: MockBroker
    ) -> None:
        await connected_broker.disconnect()
        assert connected_broker.status == BrokerStatus.DISCONNECTED

    @pytest.mark.asyncio
    async def test_login_is_idempotent(self, broker: MockBroker) -> None:
        await broker.login()
        await broker.login()
        assert broker.status == BrokerStatus.CONNECTED


# ── Operations require connection ───────────────────────────────────


class TestDisconnectedGuard:
    @pytest.mark.asyncio
    async def test_place_order_requires_connection(self, broker: MockBroker) -> None:
        with pytest.raises(BrokerDisconnectedError):
            await broker.place_order("RELIANCE", OrderSide.BUY, 1, "MARKET")

    @pytest.mark.asyncio
    async def test_get_positions_requires_connection(self, broker: MockBroker) -> None:
        with pytest.raises(BrokerDisconnectedError):
            await broker.get_positions()

    @pytest.mark.asyncio
    async def test_get_margin_requires_connection(self, broker: MockBroker) -> None:
        with pytest.raises(BrokerDisconnectedError):
            await broker.get_margin()

    @pytest.mark.asyncio
    async def test_get_order_book_requires_connection(self, broker: MockBroker) -> None:
        with pytest.raises(BrokerDisconnectedError):
            await broker.get_order_book()

    @pytest.mark.asyncio
    async def test_modify_order_requires_connection(self, broker: MockBroker) -> None:
        with pytest.raises(BrokerDisconnectedError):
            await broker.modify_order("FAKE-ID")

    @pytest.mark.asyncio
    async def test_cancel_order_requires_connection(self, broker: MockBroker) -> None:
        with pytest.raises(BrokerDisconnectedError):
            await broker.cancel_order("FAKE-ID")

    @pytest.mark.asyncio
    async def test_simulate_fill_requires_connection(self, broker: MockBroker) -> None:
        with pytest.raises(BrokerDisconnectedError):
            await broker.simulate_fill("FAKE-ID")


# ── Order creation ──────────────────────────────────────────────────


class TestPlaceOrder:
    @pytest.mark.asyncio
    async def test_place_order_returns_submitted(
        self, connected_broker: MockBroker
    ) -> None:
        order = await connected_broker.place_order(
            "RELIANCE", OrderSide.BUY, 10, "LIMIT", price=Decimal(2500)
        )
        assert order.status == OrderStatus.SUBMITTED
        assert order.filled_quantity == 0
        assert order.average_fill_price is None
        assert order.order_id.startswith("MOCK-")

    @pytest.mark.asyncio
    async def test_order_ids_are_unique(self, connected_broker: MockBroker) -> None:
        o1 = await connected_broker.place_order("RELIANCE", OrderSide.BUY, 1, "MARKET")
        o2 = await connected_broker.place_order("RELIANCE", OrderSide.BUY, 1, "MARKET")
        assert o1.order_id != o2.order_id

    @pytest.mark.asyncio
    async def test_invalid_quantity_rejected(
        self, connected_broker: MockBroker
    ) -> None:
        with pytest.raises(ValueError, match="positive"):
            await connected_broker.place_order("RELIANCE", OrderSide.BUY, 0, "MARKET")

    @pytest.mark.asyncio
    async def test_negative_quantity_rejected(
        self, connected_broker: MockBroker
    ) -> None:
        with pytest.raises(ValueError, match="positive"):
            await connected_broker.place_order("RELIANCE", OrderSide.BUY, -5, "MARKET")

    @pytest.mark.asyncio
    async def test_empty_symbol_rejected(self, connected_broker: MockBroker) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            await connected_broker.place_order("", OrderSide.BUY, 1, "MARKET")


# ── Order lifecycle ─────────────────────────────────────────────────


class TestOrderLifecycle:
    @pytest.mark.asyncio
    async def test_submitted_to_modified(self, connected_broker: MockBroker) -> None:
        order = await connected_broker.place_order(
            "RELIANCE", OrderSide.BUY, 10, "LIMIT", price=Decimal(2500)
        )
        modified = await connected_broker.modify_order(
            order.order_id, quantity=20, price=Decimal(2600)
        )
        assert modified.quantity == 20
        assert modified.price == Decimal(2600)
        assert modified.order_id == order.order_id
        assert modified.status == OrderStatus.SUBMITTED

    @pytest.mark.asyncio
    async def test_submitted_to_cancelled(self, connected_broker: MockBroker) -> None:
        order = await connected_broker.place_order(
            "RELIANCE", OrderSide.BUY, 10, "LIMIT", price=Decimal(2500)
        )
        cancelled = await connected_broker.cancel_order(order.order_id)
        assert cancelled.status == OrderStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_submitted_to_filled(self, connected_broker: MockBroker) -> None:
        order = await connected_broker.place_order(
            "RELIANCE", OrderSide.BUY, 10, "LIMIT", price=Decimal(2500)
        )
        filled = await connected_broker.simulate_fill(order.order_id)
        assert filled.status == OrderStatus.FILLED
        assert filled.filled_quantity == 10
        assert filled.average_fill_price == Decimal(2500)

    @pytest.mark.asyncio
    async def test_filled_cannot_be_modified(
        self, connected_broker: MockBroker
    ) -> None:
        order = await connected_broker.place_order(
            "RELIANCE", OrderSide.BUY, 10, "LIMIT", price=Decimal(2500)
        )
        await connected_broker.simulate_fill(order.order_id)
        with pytest.raises(ValueError, match="FILLED"):
            await connected_broker.modify_order(order.order_id, quantity=20)

    @pytest.mark.asyncio
    async def test_filled_cannot_be_cancelled(
        self, connected_broker: MockBroker
    ) -> None:
        order = await connected_broker.place_order(
            "RELIANCE", OrderSide.BUY, 10, "LIMIT", price=Decimal(2500)
        )
        await connected_broker.simulate_fill(order.order_id)
        with pytest.raises(ValueError, match="FILLED"):
            await connected_broker.cancel_order(order.order_id)

    @pytest.mark.asyncio
    async def test_filled_cannot_be_filled_again(
        self, connected_broker: MockBroker
    ) -> None:
        order = await connected_broker.place_order(
            "RELIANCE", OrderSide.BUY, 10, "LIMIT", price=Decimal(2500)
        )
        await connected_broker.simulate_fill(order.order_id)
        with pytest.raises(ValueError, match="FILLED"):
            await connected_broker.simulate_fill(order.order_id)

    @pytest.mark.asyncio
    async def test_cancelled_cannot_be_modified(
        self, connected_broker: MockBroker
    ) -> None:
        order = await connected_broker.place_order(
            "RELIANCE", OrderSide.BUY, 10, "LIMIT", price=Decimal(2500)
        )
        await connected_broker.cancel_order(order.order_id)
        with pytest.raises(ValueError, match="CANCELLED"):
            await connected_broker.modify_order(order.order_id, quantity=20)

    @pytest.mark.asyncio
    async def test_cancelled_cannot_be_filled(
        self, connected_broker: MockBroker
    ) -> None:
        order = await connected_broker.place_order(
            "RELIANCE", OrderSide.BUY, 10, "LIMIT", price=Decimal(2500)
        )
        await connected_broker.cancel_order(order.order_id)
        with pytest.raises(ValueError, match="CANCELLED"):
            await connected_broker.simulate_fill(order.order_id)

    @pytest.mark.asyncio
    async def test_market_order_fill_uses_simulated_price(
        self, connected_broker: MockBroker
    ) -> None:
        order = await connected_broker.place_order(
            "RELIANCE", OrderSide.BUY, 5, "MARKET"
        )
        filled = await connected_broker.simulate_fill(order.order_id)
        assert filled.average_fill_price is not None
        assert filled.average_fill_price == Decimal(100)

    @pytest.mark.asyncio
    async def test_modify_invalid_quantity_rejected(
        self, connected_broker: MockBroker
    ) -> None:
        order = await connected_broker.place_order(
            "RELIANCE", OrderSide.BUY, 10, "LIMIT", price=Decimal(2500)
        )
        with pytest.raises(ValueError, match="positive"):
            await connected_broker.modify_order(order.order_id, quantity=0)


# ── Unknown orders ──────────────────────────────────────────────────


class TestUnknownOrders:
    @pytest.mark.asyncio
    async def test_modify_unknown_order(self, connected_broker: MockBroker) -> None:
        with pytest.raises(ValueError, match="Unknown order"):
            await connected_broker.modify_order("DOES-NOT-EXIST")

    @pytest.mark.asyncio
    async def test_cancel_unknown_order(self, connected_broker: MockBroker) -> None:
        with pytest.raises(ValueError, match="Unknown order"):
            await connected_broker.cancel_order("DOES-NOT-EXIST")

    @pytest.mark.asyncio
    async def test_fill_unknown_order(self, connected_broker: MockBroker) -> None:
        with pytest.raises(ValueError, match="Unknown order"):
            await connected_broker.simulate_fill("DOES-NOT-EXIST")


# ── Long positions ──────────────────────────────────────────────────


class TestLongPositions:
    @pytest.mark.asyncio
    async def test_buy_from_flat(self, connected_broker: MockBroker) -> None:
        order = await connected_broker.place_order(
            "RELIANCE", OrderSide.BUY, 10, "LIMIT", price=Decimal(2500)
        )
        await connected_broker.simulate_fill(order.order_id)
        positions = await connected_broker.get_positions()
        assert len(positions) == 1
        assert positions[0].symbol == "RELIANCE"
        assert positions[0].quantity == 10
        assert positions[0].average_price == Decimal(2500)

    @pytest.mark.asyncio
    async def test_additional_buy(self, connected_broker: MockBroker) -> None:
        o1 = await connected_broker.place_order(
            "RELIANCE", OrderSide.BUY, 10, "LIMIT", price=Decimal(2000)
        )
        await connected_broker.simulate_fill(o1.order_id)
        o2 = await connected_broker.place_order(
            "RELIANCE", OrderSide.BUY, 10, "LIMIT", price=Decimal(3000)
        )
        await connected_broker.simulate_fill(o2.order_id)
        positions = await connected_broker.get_positions()
        assert positions[0].quantity == 20
        assert positions[0].average_price == Decimal(2500)

    @pytest.mark.asyncio
    async def test_sell_reducing_long(self, connected_broker: MockBroker) -> None:
        o1 = await connected_broker.place_order(
            "RELIANCE", OrderSide.BUY, 10, "LIMIT", price=Decimal(2500)
        )
        await connected_broker.simulate_fill(o1.order_id)
        o2 = await connected_broker.place_order(
            "RELIANCE", OrderSide.SELL, 4, "LIMIT", price=Decimal(2600)
        )
        await connected_broker.simulate_fill(o2.order_id)
        positions = await connected_broker.get_positions()
        assert len(positions) == 1
        assert positions[0].quantity == 6
        # Average price unchanged on partial reduction
        assert positions[0].average_price == Decimal(2500)

    @pytest.mark.asyncio
    async def test_sell_to_flat(self, connected_broker: MockBroker) -> None:
        o1 = await connected_broker.place_order(
            "RELIANCE", OrderSide.BUY, 5, "LIMIT", price=Decimal(2500)
        )
        await connected_broker.simulate_fill(o1.order_id)
        o2 = await connected_broker.place_order(
            "RELIANCE", OrderSide.SELL, 5, "LIMIT", price=Decimal(2600)
        )
        await connected_broker.simulate_fill(o2.order_id)
        positions = await connected_broker.get_positions()
        assert len(positions) == 0


# ── Short positions ─────────────────────────────────────────────────


class TestShortPositions:
    @pytest.mark.asyncio
    async def test_sell_from_flat(self, connected_broker: MockBroker) -> None:
        order = await connected_broker.place_order(
            "RELIANCE", OrderSide.SELL, 10, "LIMIT", price=Decimal(2500)
        )
        await connected_broker.simulate_fill(order.order_id)
        positions = await connected_broker.get_positions()
        assert len(positions) == 1
        assert positions[0].quantity == -10
        assert positions[0].average_price == Decimal(2500)

    @pytest.mark.asyncio
    async def test_additional_sell(self, connected_broker: MockBroker) -> None:
        o1 = await connected_broker.place_order(
            "RELIANCE", OrderSide.SELL, 10, "LIMIT", price=Decimal(2500)
        )
        await connected_broker.simulate_fill(o1.order_id)
        o2 = await connected_broker.place_order(
            "RELIANCE", OrderSide.SELL, 5, "LIMIT", price=Decimal(2600)
        )
        await connected_broker.simulate_fill(o2.order_id)
        positions = await connected_broker.get_positions()
        assert positions[0].quantity == -15
        # Weighted average: (2500*10 + 2600*5) / 15 = 2533.33...
        expected_avg = (Decimal(2500) * 10 + Decimal(2600) * 5) / 15
        assert positions[0].average_price == expected_avg

    @pytest.mark.asyncio
    async def test_buy_reducing_short(self, connected_broker: MockBroker) -> None:
        o1 = await connected_broker.place_order(
            "RELIANCE", OrderSide.SELL, 10, "LIMIT", price=Decimal(2500)
        )
        await connected_broker.simulate_fill(o1.order_id)
        o2 = await connected_broker.place_order(
            "RELIANCE", OrderSide.BUY, 4, "LIMIT", price=Decimal(2400)
        )
        await connected_broker.simulate_fill(o2.order_id)
        positions = await connected_broker.get_positions()
        assert positions[0].quantity == -6
        # Average price unchanged on partial reduction
        assert positions[0].average_price == Decimal(2500)

    @pytest.mark.asyncio
    async def test_buy_to_flat(self, connected_broker: MockBroker) -> None:
        o1 = await connected_broker.place_order(
            "RELIANCE", OrderSide.SELL, 5, "LIMIT", price=Decimal(2500)
        )
        await connected_broker.simulate_fill(o1.order_id)
        o2 = await connected_broker.place_order(
            "RELIANCE", OrderSide.BUY, 5, "LIMIT", price=Decimal(2400)
        )
        await connected_broker.simulate_fill(o2.order_id)
        positions = await connected_broker.get_positions()
        assert len(positions) == 0


# ── Order book ──────────────────────────────────────────────────────


class TestOrderBook:
    @pytest.mark.asyncio
    async def test_order_book_returns_orders(
        self, connected_broker: MockBroker
    ) -> None:
        await connected_broker.place_order("RELIANCE", OrderSide.BUY, 1, "MARKET")
        await connected_broker.place_order("INFY", OrderSide.SELL, 2, "MARKET")
        book = await connected_broker.get_order_book()
        assert len(book) == 2

    @pytest.mark.asyncio
    async def test_order_book_returns_copies(
        self, connected_broker: MockBroker
    ) -> None:
        await connected_broker.place_order("RELIANCE", OrderSide.BUY, 1, "MARKET")
        book_a = await connected_broker.get_order_book()
        book_b = await connected_broker.get_order_book()
        assert book_a[0] is not book_b[0]


# ── Defensive copies ───────────────────────────────────────────────


class TestDefensiveCopies:
    """Modifying a returned Order must not mutate internal broker state."""

    @pytest.mark.asyncio
    async def test_place_order_returns_copy(self, connected_broker: MockBroker) -> None:
        returned = await connected_broker.place_order(
            "RELIANCE", OrderSide.BUY, 10, "LIMIT", price=Decimal(2500)
        )
        returned.quantity = 999
        book = await connected_broker.get_order_book()
        assert book[0].quantity == 10

    @pytest.mark.asyncio
    async def test_modify_order_returns_copy(
        self, connected_broker: MockBroker
    ) -> None:
        order = await connected_broker.place_order(
            "RELIANCE", OrderSide.BUY, 10, "LIMIT", price=Decimal(2500)
        )
        modified = await connected_broker.modify_order(order.order_id, quantity=20)
        modified.quantity = 999
        book = await connected_broker.get_order_book()
        assert book[0].quantity == 20

    @pytest.mark.asyncio
    async def test_cancel_order_returns_copy(
        self, connected_broker: MockBroker
    ) -> None:
        order = await connected_broker.place_order(
            "RELIANCE", OrderSide.BUY, 10, "LIMIT", price=Decimal(2500)
        )
        cancelled = await connected_broker.cancel_order(order.order_id)
        cancelled.status = OrderStatus.SUBMITTED
        book = await connected_broker.get_order_book()
        assert book[0].status == OrderStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_simulate_fill_returns_copy(
        self, connected_broker: MockBroker
    ) -> None:
        order = await connected_broker.place_order(
            "RELIANCE", OrderSide.BUY, 10, "LIMIT", price=Decimal(2500)
        )
        filled = await connected_broker.simulate_fill(order.order_id)
        filled.status = OrderStatus.SUBMITTED
        book = await connected_broker.get_order_book()
        assert book[0].status == OrderStatus.FILLED

    @pytest.mark.asyncio
    async def test_get_order_book_returns_copies(
        self, connected_broker: MockBroker
    ) -> None:
        await connected_broker.place_order(
            "RELIANCE", OrderSide.BUY, 10, "LIMIT", price=Decimal(2500)
        )
        book = await connected_broker.get_order_book()
        book[0].quantity = 999
        fresh_book = await connected_broker.get_order_book()
        assert fresh_book[0].quantity == 10

    @pytest.mark.asyncio
    async def test_get_positions_returns_copies(
        self, connected_broker: MockBroker
    ) -> None:
        order = await connected_broker.place_order(
            "RELIANCE", OrderSide.BUY, 5, "LIMIT", price=Decimal(2500)
        )
        await connected_broker.simulate_fill(order.order_id)
        positions_a = await connected_broker.get_positions()
        positions_b = await connected_broker.get_positions()
        assert positions_a[0] is not positions_b[0]


# ── Margin ──────────────────────────────────────────────────────────


class TestMargin:
    @pytest.mark.asyncio
    async def test_get_margin_returns_valid_data(
        self, connected_broker: MockBroker
    ) -> None:
        margin = await connected_broker.get_margin()
        assert margin.equity > 0
        assert margin.available_funds > 0
        assert margin.buying_power > 0
