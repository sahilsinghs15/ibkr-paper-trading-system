"""EC2 instance identity discovery (IMDSv2 + env override).

Prefers EC2 Instance Metadata Service (IMDSv2) with IAM instance profile
when available; falls back to explicit env vars / Settings for local dev
and test isolation. Never hardcodes resource IDs.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)

IMDS_TOKEN_URL = "http://169.254.169.254/latest/api/token"
IMDS_BASE = "http://169.254.169.254/latest"


@dataclass(frozen=True)
class InstanceIdentity:
    """Authoritative infrastructure identity for cost attribution.

    Production EIP is a confirmed Elastic IP (54.205.127.181 / eipalloc-09224cba17bc5dfea)
    associated with i-090c7f3bcb0f37b7a. Another EIP (52.4.139.204 / eipalloc-02d258b5102adba4)
    exists in the account, so account-wide PublicIPv4 sums must not be attributed to this instance.
    """

    instance_id: str
    region: str
    account_id: str | None
    availability_zone: str | None
    instance_type: str | None
    eni_id: str | None
    public_ipv4: str | None
    private_ipv4: str | None
    eip_allocation_id: str | None = None


async def _fetch_imds(path: str, token: str | None = None) -> str | None:
    headers = {}
    if token:
        headers["X-aws-ec2-metadata-token"] = token
    try:
        async with httpx.AsyncClient(timeout=1.0) as client:
            res = await client.get(f"{IMDS_BASE}/{path}", headers=headers)
            if res.status_code == 200 and res.text.strip():
                return res.text.strip()
    except Exception:
        logger.debug("IMDS fetch failed for %s", path, exc_info=True)
    return None


async def _get_imds_token() -> str | None:
    try:
        async with httpx.AsyncClient(timeout=1.0) as client:
            res = await client.put(
                IMDS_TOKEN_URL,
                headers={"X-aws-ec2-metadata-token-ttl-seconds": "5"},
            )
            if res.status_code == 200:
                return res.text.strip()
    except Exception:
        pass
    return None


async def discover_instance_identity() -> InstanceIdentity | None:
    """Resolve instance identity; prefer IMDSv2, then env/Settings."""
    settings = get_settings()

    # Explicit env/Settings override takes precedence for tests / non-EC2
    override_id = os.environ.get("AWS_EC2_INSTANCE_ID") or settings.aws_ec2_instance_id
    override_region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or settings.aws_region
    override_account = os.environ.get("AWS_ACCOUNT_ID") or settings.aws_account_id
    override_eip_alloc = os.environ.get("AWS_EIP_ALLOCATION_ID") or getattr(settings, "aws_eip_allocation_id", None)
    override_public_ipv4 = os.environ.get("AWS_PUBLIC_IPV4") or getattr(settings, "aws_public_ipv4", None)
    override_eni = os.environ.get("AWS_ENI_ID") or getattr(settings, "aws_eni_id", None)

    # Try IMDSv2 discovery when on EC2
    token = await _get_imds_token()
    if token:
        instance_id = await _fetch_imds("meta-data/instance-id", token)
        region = await _fetch_imds("dynamic/instance-identity/document", token)
        # Parse region from document JSON if needed; fallback to AZ
        az = await _fetch_imds("meta-data/placement/availability-zone", token)
        doc_region = None
        instance_type = await _fetch_imds("meta-data/instance-type", token)
        mac = await _fetch_imds("meta-data/mac", token)
        eni_id = None
        public_ipv4 = await _fetch_imds("meta-data/public-ipv4", token)
        private_ipv4 = await _fetch_imds("meta-data/local-ipv4", token)

        # Try to parse account/region from identity document JSON
        account_id = override_account
        doc_region_parsed = override_region
        if region:
            try:
                import json

                doc = json.loads(region)
                doc_region_parsed = doc.get("region", doc_region_parsed)
                account_id = doc.get("accountId", account_id)
                instance_id = doc.get("instanceId", instance_id)
                az = doc.get("availabilityZone", az)
                instance_type = doc.get("instanceType", instance_type)
                private_ipv4 = doc.get("privateIp", private_ipv4)
            except Exception:
                # region variable actually was document string, fallback
                pass

        # ENI via mac
        if mac:
            eni_id = await _fetch_imds(f"meta-data/network/interfaces/macs/{mac}/interface-id", token)
            if not public_ipv4:
                public_ipv4 = await _fetch_imds(
                    f"meta-data/network/interfaces/macs/{mac}/public-ipv4s", token
                )
            if not private_ipv4:
                private_ipv4 = await _fetch_imds("meta-data/local-ipv4", token)

        if instance_id:
            return InstanceIdentity(
                instance_id=override_id or instance_id,
                region=doc_region_parsed or (az[:-1] if az else "us-east-1"),
                account_id=account_id,
                availability_zone=az,
                instance_type=instance_type,
                eni_id=override_eni or eni_id,
                public_ipv4=override_public_ipv4 or public_ipv4,
                private_ipv4=private_ipv4,
                eip_allocation_id=override_eip_alloc,
            )

    # Fallback to env/Settings only (no IMDS)
    if override_id and override_region:
        return InstanceIdentity(
            instance_id=override_id,
            region=override_region,
            account_id=override_account,
            availability_zone=None,
            instance_type=None,
            eni_id=override_eni,
            public_ipv4=override_public_ipv4,
            private_ipv4=None,
            eip_allocation_id=override_eip_alloc,
        )

    logger.warning("Unable to discover EC2 instance identity (no IMDS, no env override)")
    return None


def resolve_region(identity: InstanceIdentity | None) -> str:
    """Return effective AWS region for CE calls (CE is global but favors us-east-1)."""
    settings = get_settings()
    if identity and identity.region:
        return identity.region
    if settings.aws_region:
        return settings.aws_region
    return "us-east-1"
