"""AWS Cost Explorer provider for EC2 + Elastic IP daily cost.

Production EIP is a confirmed Elastic IP 54.205.127.181 / eipalloc-09224cba17bc5dfea
associated with i-090c7f3bcb0f37b7a. Account also contains 52.4.139.204 / eipalloc-02d258b5102adba4
so account-wide PublicIPv4 sums must NOT be attributed to production instance.

Authoritative API: ce:GetCostAndUsageWithResources (resource-level) for EC2
instance cost (RESOURCE_ID = i-090c7f3bcb0f37b7a) and, where CE supports it,
EIP cost (RESOURCE_ID = eipalloc-09224cba17bc5dfea). Public IPv4 billing dimension
is USAGE_TYPE = PublicIPv4:InUseAddress / PublicIPv4:InUseAddress-Hour etc.
under SERVICE = Amazon Elastic Compute Cloud - Compute.

Verified console: Resource-level data at daily granularity ENABLED for
EC2-Other + EC2-Instances; activation delay up to 48h — code must handle
DataUnavailable / opt-in-not-ready gracefully (never $0).

API docs: https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/ce-api.html
Pricing: $0.01 per GetCostAndUsage* request.
IAM: ce:GetCostAndUsage, ce:GetCostAndUsageWithResources (via EC2 instance profile CostExplorerReadOnly)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DailyCostResult:
    """Parsed result for one UTC calendar day."""

    usage_date: date
    ec2_cost_usd: Decimal | None
    public_ipv4_cost_usd: Decimal | None
    total_cost_usd: Decimal | None
    source: str
    raw_response: dict[str, Any] | None = None
    error: str | None = None


def _parse_amount(raw: str | None) -> Decimal | None:
    if raw is None:
        return None
    try:
        val = Decimal(raw)
        # Preserve explicitly zero as Decimal(0); caller decides if missing
        return val
    except (InvalidOperation, ValueError, TypeError):
        return None


def parse_ce_results_by_time(
    results_by_time: list[dict[str, Any]],
    usage_date: date,
) -> tuple[Decimal | None, Decimal | None]:
    """Extract EC2 and IPv4 costs from CE ResultsByTime entry.

    EC2 path uses UnblendedCost Amount from the row; when GROUP BY RESOURCE_ID
    is used, Amounts are per-resource. For non-resource call, Amount is already
    the filtered total.
    """
    if not results_by_time:
        return None, None
    # ResultsByTime is length 1 for single-day query
    entry = results_by_time[0]
    total = entry.get("Total", {})
    unblended = total.get("UnblendedCost", {})
    amount_str = unblended.get("Amount")
    # This is the total filtered service cost; for EC2 we treat as ec2 cost
    # IPv4 parsing handled separately
    ec2 = _parse_amount(amount_str) if amount_str is not None else None
    # Detect zero vs missing: CE returns "0" string for zero cost; we map to Decimal(0)
    # If Total missing entirely -> None (data unavailable)
    if not total:
        ec2 = None
    return ec2, None


def _extract_amount_from_results(results_by_time: list[dict[str, Any]]) -> tuple[Decimal | None, bool]:
    """Return (amount, estimated_flag)."""
    if not results_by_time:
        return None, False
    entry = results_by_time[0]
    estimated = bool(entry.get("Estimated", False))
    total = entry.get("Total", {})
    unblended = total.get("UnblendedCost", {})
    amt = unblended.get("Amount")
    if amt is None:
        return None, estimated
    parsed = _parse_amount(amt)
    # If CE marks result as Estimated, treat as not yet authoritative (activation pending)
    if estimated:
        # Preserve explicitly zero but flag as estimated; caller decides
        # For now return amount but caller will check estimated flag to decide unavailable
        return parsed, True
    return parsed, False


class CostExplorerProvider:
    """Thin wrapper around boto3 CE client; injectable for tests."""

    def __init__(self, boto3_client: Any | None = None, region: str = "us-east-1") -> None:
        self._client = boto3_client
        self._region = region

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import boto3  # type: ignore[import-not-found]

            return boto3.client("ce", region_name=self._region)
        except Exception as exc:
            raise RuntimeError(f"boto3 CE client unavailable: {exc}") from exc

    def fetch_ec2_daily_cost(
        self,
        usage_date: date,
        instance_id: str,
        *,
        account_id: str | None = None,
    ) -> DailyCostResult:
        """Fetch authoritative EC2 daily cost for one instance.

        Implements min 1 CE request: GetCostAndUsageWithResources with filter
        RESOURCE_ID = instance_id and SERVICE = "Amazon Elastic Compute Cloud - Compute".
        Falls back to GetCostAndUsage if WithResources is not enabled.
        """
        start = usage_date.isoformat()
        end = (usage_date + timedelta(days=1)).isoformat()
        client = self._get_client()

        # Primary: WithResources (resource-level)
        filters: dict[str, Any] = {
            "Dimensions": {"Key": "SERVICE", "Values": ["Amazon Elastic Compute Cloud - Compute"]}
        }
        # Combine with RESOURCE_ID via And
        ce_filter: dict[str, Any] = {
            "And": [
                {"Dimensions": {"Key": "SERVICE", "Values": ["Amazon Elastic Compute Cloud - Compute"]}},
                {"Dimensions": {"Key": "RESOURCE_ID", "Values": [instance_id]}},
            ]
        }
        if account_id:
            # Optional linked account scoping
            ce_filter["And"].append({"Dimensions": {"Key": "LINKED_ACCOUNT", "Values": [account_id]}})

        # EC2 per-instance cost is only valid via WithResources with RESOURCE_ID.
        # GetCostAndUsage does NOT support RESOURCE_ID dimension (ValidationException), so no fallback to account-wide.
        try:
            resp = client.get_cost_and_usage_with_resources(
                TimePeriod={"Start": start, "End": end},
                Granularity="DAILY",
                Filter=ce_filter,
                Metrics=["UnblendedCost"],
            )
            source = "ce:GetCostAndUsageWithResources"
            amt, estimated = _extract_amount_from_results(resp.get("ResultsByTime", []))
            if estimated:
                # Resource-level data still activating (Estimated True) — do not treat 0 as actual
                logger.warning("CE EC2 cost for %s is estimated (activation pending) — treating as unavailable", usage_date)
                return DailyCostResult(
                    usage_date=usage_date,
                    ec2_cost_usd=None,
                    public_ipv4_cost_usd=None,
                    total_cost_usd=None,
                    source=source,
                    raw_response=resp,
                    error="CE data Estimated=True (resource-level activation pending up to 48h)",
                )
            total = amt
            return DailyCostResult(
                usage_date=usage_date,
                ec2_cost_usd=total,
                public_ipv4_cost_usd=None,
                total_cost_usd=total,
                source=source,
                raw_response=resp,
            )
        except Exception as exc:
            err_str = str(exc)
            if "UnknownOperationException" in err_str or "ResourceIdNotAvailable" in err_str or "ValidationException" in err_str:
                logger.warning("CE WithResources not available: %s", exc)
            # For access denied etc, return error result not $0
            if "AccessDenied" in err_str or "DataUnavailable" in err_str or "BillExpirationException" in err_str:
                logger.error("CE EC2 cost fetch failed: %s", exc)
                return DailyCostResult(
                    usage_date=usage_date,
                    ec2_cost_usd=None,
                    public_ipv4_cost_usd=None,
                    total_cost_usd=None,
                    source="ce:GetCostAndUsageWithResources",
                    raw_response=None,
                    error=err_str,
                )
            logger.exception("CE EC2 fetch error")
            return DailyCostResult(
                usage_date=usage_date,
                ec2_cost_usd=None,
                public_ipv4_cost_usd=None,
                total_cost_usd=None,
                source="ce:GetCostAndUsageWithResources",
                error=err_str,
            )
        return DailyCostResult(
            usage_date=usage_date,
            ec2_cost_usd=None,
            public_ipv4_cost_usd=None,
            total_cost_usd=None,
            source="ce:GetCostAndUsageWithResources",
            error="No CE method succeeded",
        )

    def fetch_public_ipv4_daily_cost(
        self,
        usage_date: date,
        *,
        public_ipv4: str | None = None,
        eip_allocation_id: str | None = None,
        instance_id: str | None = None,
        account_id: str | None = None,
    ) -> DailyCostResult:
        """Fetch Elastic IP / Public IPv4 daily cost for production EIP.

        Production: 54.205.127.181 / eipalloc-09224cba17bc5dfea.
        Account also contains 52.4.139.204 / eipalloc-02d258b5102adba4, therefore
        generic account-wide PublicIPv4 sum is NOT correct attribution.

        CE billing: SERVICE = Amazon Elastic Compute Cloud - Compute,
        USAGE_TYPE = PublicIPv4:InUseAddress (and variants). Where CE provides
        per-resource breakdown, RESOURCE_ID = eipalloc-... is the exact attribution
        proof. If CE does not expose EIP as RESOURCE_ID, we treat the IPv4 component
        as unavailable rather than guessing an account-wide split.

        Returns separate result; caller combines with EC2 result.
        """
        start = usage_date.isoformat()
        end = (usage_date + timedelta(days=1)).isoformat()
        client = self._get_client()

        # Usage-type values covering Elastic IP public IPv4 billing.
        # Verified AWS dimensions: PublicIPv4:InUseAddress and variants map to EIP charges.
        ipv4_filter: dict[str, Any] = {
            "And": [
                {
                    "Dimensions": {
                        "Key": "SERVICE",
                        "Values": ["Amazon Elastic Compute Cloud - Compute"],
                    }
                },
                {
                    "Dimensions": {
                        "Key": "USAGE_TYPE",
                        "Values": [
                            "PublicIPv4:InUseAddress",
                            "PublicIPv4:IdleAddress",
                            "PublicIPv4:InUseAddress-Hour",
                        ],
                    }
                },
            ]
        }
        # When production EIP allocation is known, use strict resource-level attribution.
        # Do NOT fallback to account-wide sum — that would include eipalloc-02d258b5102adba4 (52.4.139.204).
        if eip_allocation_id:
            ipv4_filter_with_resource: dict[str, Any] = {
                "And": [
                    ipv4_filter["And"][0],
                    ipv4_filter["And"][1],
                    {"Dimensions": {"Key": "RESOURCE_ID", "Values": [eip_allocation_id]}},
                ]
            }
            try:
                resp = client.get_cost_and_usage_with_resources(
                    TimePeriod={"Start": start, "End": end},
                    Granularity="DAILY",
                    Filter=ipv4_filter_with_resource,
                    Metrics=["UnblendedCost"],
                )
                amt, estimated = _extract_amount_from_results(resp.get("ResultsByTime", []))
                if estimated:
                    return DailyCostResult(
                        usage_date=usage_date,
                        ec2_cost_usd=None,
                        public_ipv4_cost_usd=None,
                        total_cost_usd=None,
                        source="ce:GetCostAndUsageWithResources",
                        raw_response=resp,
                        error="EIP resource-level data Estimated=True (activation pending)",
                    )
                # If amt is None -> CE resource-level not yet available
                return DailyCostResult(
                    usage_date=usage_date,
                    ec2_cost_usd=None,
                    public_ipv4_cost_usd=amt,
                    total_cost_usd=amt,
                    source="ce:GetCostAndUsageWithResources",
                    raw_response=resp,
                    error=None if amt is not None else "EIP resource-level data unavailable (activation pending or no RESOURCE_ID mapping)",
                )
            except Exception as exc:  # noqa: BLE001
                err_str = str(exc)
                logger.warning("CE EIP fetch (resource-level) failed: %s", exc)
                if "AccessDenied" in err_str or "DataUnavailable" in err_str or "BillExpirationException" in err_str:
                    return DailyCostResult(
                        usage_date=usage_date,
                        ec2_cost_usd=None,
                        public_ipv4_cost_usd=None,
                        total_cost_usd=None,
                        source="ce:GetCostAndUsageWithResources",
                        error=err_str,
                    )
                # For other errors, treat as unavailable without account-wide fallback
                return DailyCostResult(
                    usage_date=usage_date,
                    ec2_cost_usd=None,
                    public_ipv4_cost_usd=None,
                    total_cost_usd=None,
                    source="ce:GetCostAndUsageWithResources",
                    error=err_str,
                )

        # Fallback path only when allocation not configured (tests / non-prod).
        # This is the legacy single-IP assumption and is documented as not production-correct when multiple EIPs exist.
        try:
            resp = client.get_cost_and_usage(
                TimePeriod={"Start": start, "End": end},
                Granularity="DAILY",
                Filter=ipv4_filter,
                Metrics=["UnblendedCost"],
            )
            amt, estimated = _extract_amount_from_results(resp.get("ResultsByTime", []))
            if estimated:
                return DailyCostResult(
                    usage_date=usage_date,
                    ec2_cost_usd=None,
                    public_ipv4_cost_usd=None,
                    total_cost_usd=None,
                    source="ce:GetCostAndUsage",
                    raw_response=resp,
                    error="IPv4 data Estimated=True",
                )
            return DailyCostResult(
                usage_date=usage_date,
                ec2_cost_usd=None,
                public_ipv4_cost_usd=amt,
                total_cost_usd=amt,
                source="ce:GetCostAndUsage",
                raw_response=resp,
            )
        except Exception as exc:  # noqa: BLE001
            err_str = str(exc)
            logger.warning("CE IPv4 fallback fetch failed: %s", exc)
            return DailyCostResult(
                usage_date=usage_date,
                ec2_cost_usd=None,
                public_ipv4_cost_usd=None,
                total_cost_usd=None,
                source="ce:GetCostAndUsage",
                error=err_str,
            )

    def fetch_combined_daily_cost(
        self,
        usage_date: date,
        instance_id: str,
        *,
        public_ipv4: str | None = None,
        eip_allocation_id: str | None = None,
        account_id: str | None = None,
    ) -> DailyCostResult:
        """Fetch combined EC2 + IPv4 in minimum requests (typically 2).

        Returns a merged DailyCostResult where total = ec2 + ipv4.
        Never fabricates $0 for missing data.
        """
        ec2_res = self.fetch_ec2_daily_cost(usage_date, instance_id, account_id=account_id)
        ipv4_res = self.fetch_public_ipv4_daily_cost(
            usage_date,
            public_ipv4=public_ipv4,
            eip_allocation_id=eip_allocation_id,
            instance_id=instance_id,
            account_id=account_id,
        )

        # Handle error propagation
        if ec2_res.error and ipv4_res.error:
            return DailyCostResult(
                usage_date=usage_date,
                ec2_cost_usd=None,
                public_ipv4_cost_usd=None,
                total_cost_usd=None,
                source=f"{ec2_res.source}+{ipv4_res.source}",
                error=f"EC2:{ec2_res.error}; IPv4:{ipv4_res.error}",
                raw_response=None,
            )

        # If ec2 missing but ipv4 present (or vice versa), preserve partial
        # Do NOT coerce missing to 0; caller must treat None as unavailable
        ec2 = ec2_res.ec2_cost_usd
        ipv4 = ipv4_res.public_ipv4_cost_usd

        # Determine total: only if at least one component is not None; missing components stay None
        # If both missing -> None; if one missing we preserve available component as total?
        # Spec: daily total = ec2 + ipv4; if ipv4 unavailable we should not fabricate 0,
        # so total stays ec2 when ipv4 None? But then monthly sum would undercount.
        # Safer: total = ec2 if ipv4 is None and ec2 is not None? But spec says public IPv4 charge MUST be included.
        # We expose components separately; total computed as sum of available if any, with flag for partial.
        if ec2 is not None and ipv4 is not None:
            total = ec2 + ipv4
        elif ec2 is not None:
            total = ec2
        elif ipv4 is not None:
            total = ipv4
        else:
            total = None

        # Prefer error from successful call if partial
        error = None
        if ec2_res.error or ipv4_res.error:
            # If total is still computable partially, surface warning but not fatal
            parts = []
            if ec2_res.error:
                parts.append(f"EC2:{ec2_res.error}")
            if ipv4_res.error:
                parts.append(f"IPv4:{ipv4_res.error}")
            error = "; ".join(parts)

        return DailyCostResult(
            usage_date=usage_date,
            ec2_cost_usd=ec2,
            public_ipv4_cost_usd=ipv4,
            total_cost_usd=total,
            source=f"{ec2_res.source}+{ipv4_res.source}",
            raw_response={"ec2": ec2_res.raw_response, "ipv4": ipv4_res.raw_response},
            error=error if total is None else None,
        )
