"""SQLAlchemy model for daily instance credit usage ledger.

Persists authoritative daily EC2 + Public IPv4 costs fetched once per day
from AWS Cost Explorer. Current-day estimate is derived on the fly from
the latest ACTUAL; it is not persisted as a separate ledger entry to avoid
double-counting.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, Date, DateTime, Numeric, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

CREDIT_STATUS_ACTUAL = "ACTUAL"
CREDIT_STATUS_FAILED = "FAILED"


class InstanceDailyCreditModel(Base):
    """Durable daily ledger for instance infrastructure cost."""

    __tablename__ = "instance_daily_credit_usage"

    usage_date: Mapped[date] = mapped_column(Date, primary_key=True)
    instance_id: Mapped[str] = mapped_column(String(32), nullable=False)
    aws_account_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    region: Mapped[str | None] = mapped_column(String(32), nullable=True)
    eni_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    public_ipv4: Mapped[str | None] = mapped_column(String(64), nullable=True)
    eip_allocation_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    ec2_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    public_ipv4_cost_usd: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 6), nullable=True
    )
    total_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)

    status: Mapped[str] = mapped_column(String(16), nullable=False, default=CREDIT_STATUS_ACTUAL)
    source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=lambda: datetime.now(UTC),
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('ACTUAL','FAILED')",
            name="ck_instance_daily_credit_status",
        ),
    )
