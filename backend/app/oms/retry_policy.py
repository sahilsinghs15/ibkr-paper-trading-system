"""Paper-only basket retry / square-off timing. Does not submit orders."""

from __future__ import annotations

from dataclasses import dataclass

PAPER_IBKR_PORTS = frozenset({7497, 4002})


def paper_retry_ports_allowed(ibkr_port: int) -> bool:
    """Retries are demo/paper Gateway/TWS ports only (not 7496/4001 live)."""
    return int(ibkr_port) in PAPER_IBKR_PORTS


@dataclass(frozen=True)
class ExecutionRetryPolicy:
    """User-facing auto square-off and incomplete-leg retry knobs."""

    enabled: bool = True
    square_off_after_sec: float = 30.0
    max_retries: int = 3
    retry_interval_sec: float = 5.0
    retry_window_sec: float = 30.0

    def validate(self) -> None:
        if self.square_off_after_sec <= 0:
            raise ValueError("square_off_after_sec must be greater than 0.")
        if self.max_retries < 0:
            raise ValueError("max_retries must be >= 0.")
        if self.retry_interval_sec <= 0:
            raise ValueError("retry_interval_sec must be greater than 0.")
        if self.retry_window_sec <= 0:
            raise ValueError("retry_window_sec must be greater than 0.")
        if self.retry_window_sec < self.retry_interval_sec:
            raise ValueError("retry_window_sec must be >= retry_interval_sec.")


def default_paper_retry_policy() -> ExecutionRetryPolicy:
    policy = ExecutionRetryPolicy()
    policy.validate()
    return policy
