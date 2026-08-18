"""SQLAlchemy model for append-only event audit trail."""

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.db.models.order import OrderModel
    from app.db.models.signal import SignalModel


class EventLogModel(Base):
    """Append-only system event audit trail table."""

    __tablename__ = "event_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    process: Mapped[str] = mapped_column(String, nullable=False)
    signal_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("signals.id"), nullable=True
    )
    order_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("orders.id"), nullable=True
    )
    basket_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("baskets.id"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String, nullable=True, unique=True)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    signal: Mapped["SignalModel | None"] = relationship("SignalModel")
    order: Mapped["OrderModel | None"] = relationship("OrderModel")
