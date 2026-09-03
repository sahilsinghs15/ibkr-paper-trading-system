"""CHECK 1 — MARGIN cap against live IBKR headroom.

Uses cached directional rates and the running committed-margin tally.
Broker I/O is not allowed here: the borderline what-if confirmation lives
in OrderManager after instrument resolution (Gate C).
"""


from app.rms.checks.base import BaseRMSCheck
from app.rms.margin_estimate import (
    MarginBand,
    classify_headroom,
    effective_free_margin,
    estimate_required_margin,
)
from app.rms.models import (
    CheckResult,
    ExecutionIntentMode,
    OrderAction,
    OrderIntent,
    RMSContext,
    RMSOutcome,
)


class MarginCheck(BaseRMSCheck):
    """CHECK 1 — Enforces IBKR initial-margin headroom per account."""

    @property
    def check_number(self) -> int:
        return 1

    @property
    def check_name(self) -> str:
        return "MARGIN"

    def evaluate(self, intent: OrderIntent, context: RMSContext) -> CheckResult:
        policy = context.margin_policy
        if not policy.check_enabled:
            return CheckResult(
                check_number=self.check_number,
                check_name=self.check_name,
                outcome=RMSOutcome.PASS,
                reason="MARGIN_CHECK_DISABLED: shadow mode, see margin_settings.check_enabled.",
            )

        if (
            intent.action == OrderAction.CLOSE
            or intent.intent_mode == ExecutionIntentMode.EMERGENCY_FLATTEN
        ):
            return CheckResult(
                check_number=self.check_number,
                check_name=self.check_name,
                outcome=RMSOutcome.PASS,
            )

        account = (intent.ibkr_account or "").strip().upper()
        if not account:
            return CheckResult(
                check_number=self.check_number,
                check_name=self.check_name,
                outcome=RMSOutcome.REJECT,
                reason="MARGIN_SNAPSHOT_UNAVAILABLE: intent has no ibkr_account.",
            )

        snapshot = context.margin_snapshots.get(account)
        if snapshot is None:
            return CheckResult(
                check_number=self.check_number,
                check_name=self.check_name,
                outcome=RMSOutcome.REJECT,
                reason=(
                    f"MARGIN_SNAPSHOT_UNAVAILABLE: no accountSummary snapshot "
                    f"for {account}."
                ),
            )

        if snapshot.is_stale and policy.reject_on_stale_snapshot:
            return CheckResult(
                check_number=self.check_number,
                check_name=self.check_name,
                outcome=RMSOutcome.REJECT,
                reason=(
                    f"MARGIN_SNAPSHOT_STALE: snapshot for {account} older than "
                    f"{snapshot.max_age_sec}s."
                ),
            )

        required, sources = estimate_required_margin(
            intent,
            context.margin_rates,
            context.margin_rate_sources,
            policy,
        )
        if required <= 0:
            return CheckResult(
                check_number=self.check_number,
                check_name=self.check_name,
                outcome=RMSOutcome.PASS,
                reason="MARGIN_ZERO_REQUIRED",
            )

        effective = effective_free_margin(
            snapshot, context.margin_commitments.get(account, []), policy
        )
        if effective is None:
            return CheckResult(
                check_number=self.check_number,
                check_name=self.check_name,
                outcome=RMSOutcome.REJECT,
                reason=(
                    f"MARGIN_SNAPSHOT_UNAVAILABLE: {policy.gate_basis} missing "
                    f"for {account}."
                ),
            )

        band = classify_headroom(
            required, effective_free=effective, policy=policy
        )
        source_detail = ", ".join(
            f"{symbol}={source}" for symbol, source in sources.items()
        )
        if band is MarginBand.INSUFFICIENT:
            return CheckResult(
                check_number=self.check_number,
                check_name=self.check_name,
                outcome=RMSOutcome.REJECT,
                reason=(
                    f"MARGIN_INSUFFICIENT: account={account} required={required} "
                    f"effective_free={effective} basis={policy.gate_basis} "
                    f"rates=[{source_detail}]."
                ),
            )

        return CheckResult(
            check_number=self.check_number,
            check_name=self.check_name,
            outcome=RMSOutcome.PASS,
            reason=(
                f"MARGIN_{band.value}: required={required} effective_free={effective} "
                f"rates=[{source_detail}]."
            ),
        )
