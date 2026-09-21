"""ServiceLifecycleWatcher must not report a stop it cannot evidence.

Incident, 2026-09-21 12:19-12:21 EDT: Telegram delivered "Signal Receiver
Stopped", "Signal Receiver Started", "Dashboard Engine Stopped", "Dashboard
Engine Started" and finally "Subsystem SYSTEM is FLAPPING" -- while `systemctl`
showed no transition for either unit. webhook-ingest had been running since
08:35 and demo-streaming since 11:54. Nothing restarted.

The watcher probed each service with a 1s timeout every 2s and treated a single
failed probe as a stop. Each stop/start pair was two seconds apart, which is
shorter than either service takes to boot -- the signature of a probe blip, not
a restart. Three pairs tripped the flapping detector.

A probe that does not answer proves only that no answer arrived. Going down now
needs consecutive failures; coming up still needs one success, because a 200 is
positive evidence.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.notification.service_watcher import (
    DOWN_CONFIRM_PROBES,
    ServiceLifecycleWatcher,
)

SVC = "dashboard_engine"


def _watcher(**kw) -> ServiceLifecycleWatcher:
    orch = MagicMock()
    orch.ingest_event = AsyncMock(return_value=None)
    w = ServiceLifecycleWatcher(orch, **kw)
    w._states[SVC] = True  # already observed up
    return w


def test_single_failed_probe_is_not_a_stop():
    """The exact incident: one blip must not declare the service down."""
    w = _watcher()
    assert w._observe(SVC, False) is None, "one failure must stay undecided"


def test_blip_that_recovers_reports_nothing_at_all():
    """Fail then succeed, repeatedly -- no transition should ever be confirmed."""
    w = _watcher()
    for _ in range(5):
        assert w._observe(SVC, False) is None
        assert w._observe(SVC, True) is True
    # Never dropped below the threshold, so the state was never contradicted.
    assert w._states[SVC] is True


def test_consecutive_failures_do_confirm_a_stop():
    """A genuinely dead service is still reported."""
    w = _watcher()
    results = [w._observe(SVC, False) for _ in range(DOWN_CONFIRM_PROBES)]
    assert results[:-1] == [None] * (DOWN_CONFIRM_PROBES - 1)
    assert results[-1] is False


def test_failure_run_resets_on_success():
    """Two failures then a success must not carry the count forward."""
    w = _watcher()
    assert w._observe(SVC, False) is None
    assert w._observe(SVC, False) is None
    assert w._observe(SVC, True) is True
    # Counter reset: a single later failure is undecided again, not a stop.
    assert w._observe(SVC, False) is None


def test_recovery_is_confirmed_by_one_success():
    """Coming back up is positive evidence and needs no confirmation run."""
    w = _watcher()
    for _ in range(DOWN_CONFIRM_PROBES):
        w._observe(SVC, False)
    assert w._observe(SVC, True) is True


@pytest.mark.parametrize("threshold", [1, 2, 5])
def test_threshold_is_configurable(threshold):
    w = _watcher(failure_threshold=threshold)
    observed = [w._observe(SVC, False) for _ in range(threshold)]
    assert observed[-1] is False
    assert all(o is None for o in observed[:-1])


def test_threshold_floor_is_one():
    """A zero or negative threshold would make every probe a stop."""
    w = _watcher(failure_threshold=0)
    assert w._failure_threshold == 1


def test_probe_timeout_is_tolerant_of_load():
    """1s timed out under ordinary SSE traffic on the dashboard."""
    w = _watcher()
    assert w._probe_timeout >= 3.0
