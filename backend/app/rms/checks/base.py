"""Base interface for Risk Management System (RMS) checks."""

from abc import ABC, abstractmethod

from app.rms.models import CheckResult, OrderIntent, RMSContext


class BaseRMSCheck(ABC):
    """Abstract base class for all RMS checks."""

    @property
    @abstractmethod
    def check_number(self) -> int:
        """The numerical designation of the check according to the RMS spec."""

    @property
    @abstractmethod
    def check_name(self) -> str:
        """Human-readable identifier of the check."""

    @abstractmethod
    def evaluate(self, intent: OrderIntent, context: RMSContext) -> CheckResult:
        """Evaluate the OrderIntent against the current RMSContext state.

        Args:
            intent: The OrderIntent to evaluate.
            context: The simulated RMSContext containing state and rules.

        Returns:
            A CheckResult indicating PASS, REJECT, ADJUST, or HALT.
        """
