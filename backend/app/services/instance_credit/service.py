"""Instance credit ledger service: fetch, persist, calculate."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.instance_credit import InstanceDailyCreditModel
from app.services.instance_credit.calculation import compute_credit_usage, daily_display
from app.services.instance_credit.metadata import (
    InstanceIdentity,
    discover_instance_identity,
    resolve_region,
)
from app.services.instance_credit.provider import CostExplorerProvider

logger = logging.getLogger(__name__)


class InstanceCreditService:
    """Orchestrates idempotent daily fetch and monthly calculation."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        provider: CostExplorerProvider | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._provider = provider

    async def _get_provider(self, region: str) -> CostExplorerProvider:
        if self._provider is not None:
            return self._provider
        return CostExplorerProvider(region=region)

    async def get_identity(self) -> InstanceIdentity | None:
        return await discover_instance_identity()

    async def fetch_and_persist_for_date(
        self,
        target_date: date,
        *,
        identity: InstanceIdentity | None = None,
        provider: CostExplorerProvider | None = None,
    ) -> InstanceDailyCreditModel | None:
        """Idempotent fetch for previous UTC calendar day.

        - If ledger already contains ACTUAL for target_date, skip AWS call.
        - On success, upsert ACTUAL record.
        - On failure, do NOT write $0; record FAILED with error detail if desired,
          but preserve last valid data. We choose to NOT insert FAILED row for missing
          data; instead return None and let caller surface stale/unavailable.
        - For permission/data-unavailable errors, we optionally persist FAILED to
          aid diagnostics but never as ACTUAL with zero.
        """
        if identity is None:
            identity = await self.get_identity()
        if identity is None:
            logger.warning("InstanceCredit fetch skipped: no identity")
            return None

        region = resolve_region(identity)
        prov = provider or await self._get_provider(region)

        async with self._session_factory() as session:
            existing = await session.get(InstanceDailyCreditModel, target_date)
            if (
                existing is not None
                and existing.status == "ACTUAL"
                and existing.total_cost_usd is not None
                and existing.instance_id == identity.instance_id
            ):
                logger.info("InstanceCredit ledger already has ACTUAL for %s, skipping fetch", target_date)
                return existing
            if existing is not None and existing.instance_id != identity.instance_id:
                logger.warning(
                    "InstanceCredit instance mismatch for %s: ledger %s vs current %s — will refresh with new identity",
                    target_date,
                    existing.instance_id,
                    identity.instance_id,
                )

            # Fetch combined cost — pass production EIP allocation for exact attribution
            result = prov.fetch_combined_daily_cost(
                target_date,
                instance_id=identity.instance_id,
                public_ipv4=identity.public_ipv4,
                eip_allocation_id=identity.eip_allocation_id,
                account_id=identity.account_id,
            )

            if result.total_cost_usd is None:
                # Do NOT write $0; log error, optionally persist FAILED for observability
                logger.warning(
                    "InstanceCredit fetch returned no data for %s: %s",
                    target_date,
                    result.error,
                )
                # Persist FAILED marker to avoid repeated hammering same day? But spec says at most once per day,
                # so we should not persist FAILED as success barrier; instead let next day's scheduler decide.
                # For diagnostics, we could insert FAILED row to record error_detail, but not count as ACTUAL.
                # We'll insert/update FAILED only if no ACTUAL exists.
                if existing is None:
                    model = InstanceDailyCreditModel(
                        usage_date=target_date,
                        instance_id=identity.instance_id,
                        aws_account_id=identity.account_id,
                        region=region,
                        eni_id=identity.eni_id,
                        public_ipv4=identity.public_ipv4,
                        eip_allocation_id=identity.eip_allocation_id,
                        ec2_cost_usd=None,
                        public_ipv4_cost_usd=None,
                        total_cost_usd=None,
                        status="FAILED",
                        source=result.source,
                        fetched_at=datetime.now(UTC),
                        error_detail=result.error,
                    )
                    session.add(model)
                    try:
                        await session.commit()
                    except Exception:
                        await session.rollback()
                        logger.exception("Failed to persist FAILED credit record")
                else:
                    existing.error_detail = result.error
                    existing.fetched_at = datetime.now(UTC)
                    await session.commit()
                return None

            # Success: persist ACTUAL
            ec2 = result.ec2_cost_usd
            ipv4 = result.public_ipv4_cost_usd
            total = result.total_cost_usd

            # Idempotent upsert via pg_insert on conflict — include EIP allocation for exact attribution
            stmt = pg_insert(InstanceDailyCreditModel).values(
                usage_date=target_date,
                instance_id=identity.instance_id,
                aws_account_id=identity.account_id,
                region=region,
                eni_id=identity.eni_id,
                public_ipv4=identity.public_ipv4,
                eip_allocation_id=identity.eip_allocation_id,
                ec2_cost_usd=ec2,
                public_ipv4_cost_usd=ipv4,
                total_cost_usd=total,
                status="ACTUAL",
                source=result.source,
                fetched_at=datetime.now(UTC),
                error_detail=None,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["usage_date"],
                set_={
                    "instance_id": identity.instance_id,
                    "aws_account_id": identity.account_id,
                    "region": region,
                    "eni_id": identity.eni_id,
                    "public_ipv4": identity.public_ipv4,
                    "eip_allocation_id": identity.eip_allocation_id,
                    "ec2_cost_usd": ec2,
                    "public_ipv4_cost_usd": ipv4,
                    "total_cost_usd": total,
                    "status": "ACTUAL",
                    "source": result.source,
                    "fetched_at": datetime.now(UTC),
                    "error_detail": None,
                    "updated_at": datetime.now(UTC),
                },
            )
            await session.execute(stmt)
            await session.commit()
            # Expire identity map so get fetches fresh row (handles instance_id mismatch update)
            session.expire_all()
            persisted = await session.get(InstanceDailyCreditModel, target_date)
            logger.info(
                "InstanceCredit persisted ACTUAL %s: ec2=%s ipv4=%s total=%s",
                target_date,
                ec2,
                ipv4,
                total,
            )
            return persisted

    async def get_monthly_usage(
        self,
        *,
        today: date | None = None,
    ) -> dict[str, Any]:
        """Compute monthly ledger view from persisted ACTUAL rows.

        Returns dict with keys matching compute_credit_usage + resource identity.
        """
        if today is None:
            today = datetime.now(UTC).date()
        month_start = date(today.year, today.month, 1)

        async with self._session_factory() as session:
            # Fetch actuals for current month (status ACTUAL and date in [month_start, today) )
            q = select(InstanceDailyCreditModel).where(
                InstanceDailyCreditModel.usage_date >= month_start,
                InstanceDailyCreditModel.usage_date < today,
                InstanceDailyCreditModel.status == "ACTUAL",
            ).order_by(InstanceDailyCreditModel.usage_date.asc())
            res = await session.execute(q)
            actuals: list[InstanceDailyCreditModel] = list(res.scalars().all())

            # Latest actual overall (for estimate fallback across month boundary)
            latest_q = (
                select(InstanceDailyCreditModel)
                .where(InstanceDailyCreditModel.status == "ACTUAL", InstanceDailyCreditModel.total_cost_usd.is_not(None))
                .order_by(InstanceDailyCreditModel.usage_date.desc())
                .limit(1)
            )
            latest_res = await session.execute(latest_q)
            latest: InstanceDailyCreditModel | None = latest_res.scalar_one_or_none()

            # Also fetch last fetched_at for metadata
            fetched_at = latest.fetched_at if latest else None

            actual_records = [
                {"usage_date": r.usage_date, "total_cost_usd": r.total_cost_usd} for r in actuals
            ]

            latest_total = latest.total_cost_usd if latest else None
            latest_date = latest.usage_date if latest else None

            calc = compute_credit_usage(
                today=today,
                actual_records=actual_records,
                latest_actual_total=latest_total,
                latest_actual_date=latest_date,
                fetched_at=fetched_at,
            )

            # Daily display for today
            today_rec_q = select(InstanceDailyCreditModel).where(InstanceDailyCreditModel.usage_date == today)
            today_res = await session.execute(today_rec_q)
            today_rec = today_res.scalar_one_or_none()
            today_dict = None
            if today_rec is not None:
                today_dict = {
                    "usage_date": today_rec.usage_date,
                    "total_cost_usd": today_rec.total_cost_usd,
                    "status": today_rec.status,
                }

            daily = daily_display(
                today=today,
                latest_record=today_dict,
                latest_actual_total=latest_total,
                latest_actual_date=latest_date,
            )

            # Also fetch count for diagnostics
            return {
                **calc,
                "daily": daily,
                "latest_actual_date": latest_date,
                "latest_actual_total": latest_total,
                "actual_count": len(actuals),
            }

    async def list_ledger(self, limit: int = 60) -> list[InstanceDailyCreditModel]:
        async with self._session_factory() as session:
            q = select(InstanceDailyCreditModel).order_by(InstanceDailyCreditModel.usage_date.desc()).limit(limit)
            res = await session.execute(q)
            return list(res.scalars().all())
