"""Restart reporting, and the event_log double-write that inflated the feed.

Two defects, both operator-visible:

(a) A systemd restart stops a unit and immediately re-activates it. `cli.py`
    correctly suppresses the "Stopped" alert, but used to discard the restart
    fact entirely, so the startup that followed was indistinguishable from a cold
    boot and reported "System Universe Started Successfully". The operator got a
    message with no way to tell why it arrived or what had changed.

(b) `PositionReconciler` writes its own `event_log` row for ROGUE_* (with an
    idempotency key) *and* passed the event to the orchestrator, whose mirror
    wrote a second row with the same kind. Both are in ALLOWED_KINDS, so the
    Notification Center listed and counted every rogue event twice.
"""

from __future__ import annotations

import json
import time

import pytest

from app.services.notification.restart_tracker import (
    CASCADE_MAP,
    SERVICE_COMPONENTS,
    RestartRecord,
    cascaded_from,
    consume_restarts,
    expand_restart_chain,
    record_restart,
)
from app.services.notification.startup import build_restart_summary
from app.services.notification.system_state import CANONICAL_STARTUP_COMPONENTS

ALL_KEYS = (
    "ec2_instance",
    "ib_gateway",
    "ib_login",
    "broker_connection",
    "signal_receiver",
    "oems_engine",
    "dashboard_engine",
)


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAPP_STATE_DIR", str(tmp_path))
    return tmp_path


# ── restart markers ──────────────────────────────────────────────────────────


def test_restart_marker_survives_the_process_that_wrote_it(state_dir):
    """The stop hook and the next startup are different processes."""
    record_restart("trading-backend", "oems_engine")

    records = consume_restarts()
    assert [r.service for r in records] == ["trading-backend"]
    assert records[0].component == "oems_engine"


def test_markers_are_consumed_exactly_once(state_dir):
    """A restart is reported once; a later startup must not claim to be one."""
    record_restart("trading-backend", "oems_engine")

    assert len(consume_restarts()) == 1
    assert consume_restarts() == []


def test_stale_markers_are_discarded(state_dir):
    """A restart that never produced a startup must not label an unrelated one."""
    record_restart("trading-backend", "oems_engine")
    marker = next(state_dir.glob("*.restart.json"))
    aged = json.loads(marker.read_text())
    aged["recorded_at"] = time.time() - 10_000
    marker.write_text(json.dumps(aged))

    assert consume_restarts(ttl_sec=300.0) == []
    # Still consumed, so it cannot resurface on a later startup either.
    assert list(state_dir.glob("*.restart.json")) == []


def test_unreadable_marker_is_dropped_not_raised(state_dir):
    (state_dir / "broken.restart.json").write_text("{not json")
    assert consume_restarts() == []


def test_missing_state_dir_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAPP_STATE_DIR", str(tmp_path / "nope"))
    assert consume_restarts() == []


def test_record_restart_never_raises(tmp_path, monkeypatch):
    """The stop hook must not fail a unit stop."""
    monkeypatch.setenv("TRADINGAPP_STATE_DIR", "/proc/cannot/create/here")
    record_restart("trading-backend", "oems_engine")  # must not raise


# ── cascade ──────────────────────────────────────────────────────────────────


def test_cascade_edges_match_the_unit_files():
    """Each edge is a documented one-way trigger between systemd units.

    ibgateway -> trading-backend : scripts/ibgateway-wrapper.sh touches
        restart_backend.trigger; trading-backend-restart.path watches it.
    trading-backend -> demo-streaming : trading-backend.service ExecStartPost
        runs backend-ready-trigger.sh, which touches restart_demo.trigger;
        demo-streaming-restart.path watches it.
    """
    assert CASCADE_MAP == {
        "ibgateway": ("trading-backend",),
        "trading-backend": ("demo-streaming",),
    }


def test_server_machine_is_not_a_cascade_root():
    """A host reboot is a cold boot, not one service dragging others with it."""
    assert "server-machine" not in CASCADE_MAP
    assert "ec2-instance" not in CASCADE_MAP


def test_every_service_maps_to_a_canonical_component():
    """A restart of any service must be able to name itself in the message."""
    labels = dict(CANONICAL_STARTUP_COMPONENTS)
    for service, component in SERVICE_COMPONENTS.items():
        assert component in labels, f"{service} maps to unknown component {component}"


def test_cascade_membership_is_directional():
    assert cascaded_from("demo-streaming", {"trading-backend", "demo-streaming"}) == "trading-backend"
    assert cascaded_from("trading-backend", {"ibgateway"}) == "ibgateway"
    # A follower restarted on its own is a primary action, not a consequence.
    assert cascaded_from("demo-streaming", {"demo-streaming"}) is None
    assert cascaded_from("ibgateway", {"ibgateway", "trading-backend"}) is None


# ── chain expansion ──────────────────────────────────────────────────────────


def _rec(service, when=None):
    return RestartRecord(service, SERVICE_COMPONENTS[service], when or time.time())


def test_chain_expands_transitively_from_the_root():
    """ibgateway drags trading-backend, which drags demo-streaming."""
    nodes = expand_restart_chain([_rec("ibgateway")])

    assert [n.service for n in nodes] == ["ibgateway", "trading-backend", "demo-streaming"]
    assert [n.depth for n in nodes] == [0, 1, 2]
    assert [n.caused_by for n in nodes] == [None, "ibgateway", "trading-backend"]


def test_cascaded_services_are_reported_without_their_own_marker():
    """demo-streaming.service has no ExecStopPost hook, so it can never record
    a marker -- but restarting trading-backend always restarts it."""
    nodes = expand_restart_chain([_rec("trading-backend")])

    assert [n.service for n in nodes] == ["trading-backend", "demo-streaming"]
    assert nodes[0].observed is True
    assert nodes[1].observed is False
    assert nodes[1].component == "dashboard_engine"


def test_root_is_ordered_before_its_followers_regardless_of_marker_order():
    nodes = expand_restart_chain([_rec("demo-streaming"), _rec("trading-backend")])

    assert [n.service for n in nodes] == ["trading-backend", "demo-streaming"]


def test_independent_restarts_are_each_their_own_root():
    nodes = expand_restart_chain([_rec("webhook-ingest"), _rec("trading-backend")])

    assert [n.depth for n in nodes if n.service == "webhook-ingest"] == [0]
    assert {n.service for n in nodes} == {"webhook-ingest", "trading-backend", "demo-streaming"}


def test_no_service_appears_twice_in_a_chain():
    nodes = expand_restart_chain([_rec("ibgateway"), _rec("trading-backend"), _rec("demo-streaming")])

    services = [n.service for n in nodes]
    assert len(services) == len(set(services))
    assert services[0] == "ibgateway"


# ── message ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("service", "expected_heading"),
    [
        ("trading-backend", "OEMS Engine"),
        ("ibgateway", "IB Gateway"),
        ("webhook-ingest", "Signal Receiver"),
        ("demo-streaming", "Dashboard Engine"),
    ],
)
def test_heading_names_the_service_the_operator_acted_on(service, expected_heading):
    """Every service gets its own heading -- never "System Universe"."""
    label, message, _ = build_restart_summary([_rec(service)], {k: True for k in ALL_KEYS})

    assert label == expected_heading
    assert "System Universe" not in message


@pytest.mark.parametrize("service", sorted(SERVICE_COMPONENTS))
def test_every_service_renders_the_full_state_table(service):
    """The body always reports all seven components, whichever service restarted."""
    _, message, _ = build_restart_summary([_rec(service)], {k: True for k in ALL_KEYS})

    assert "Current State" in message
    for _key, label in CANONICAL_STARTUP_COMPONENTS:
        assert label in message


def test_two_level_chain_attributes_each_hop_to_its_cause():
    label, message, components = build_restart_summary(
        [_rec("ibgateway")], {k: True for k in ALL_KEYS}
    )

    assert label == "IB Gateway"
    assert components == ["ib_gateway", "oems_engine", "dashboard_engine"]
    assert "cascaded from IB Gateway" in message
    assert "cascaded from OEMS Engine" in message


def test_single_service_restart_has_no_cascade_line():
    label, message, components = build_restart_summary(
        [_rec("webhook-ingest")], {k: True for k in ALL_KEYS}
    )

    assert label == "Signal Receiver"
    assert components == ["signal_receiver"]
    assert "cascaded from" not in message
    # Nothing else was touched, so the "no other service" clause would be noise.
    assert "no other service was interrupted" not in message


def test_message_states_what_is_still_down():
    """Reporting a restart must not imply everything is fine."""
    states = {k: True for k in ALL_KEYS}
    states["dashboard_engine"] = False

    _, message, _ = build_restart_summary([_rec("trading-backend")], states)

    assert "Still down: Dashboard Engine" in message
    assert "All systems operational" not in message


def test_cascade_column_is_aligned_at_every_depth():
    _, message, _ = build_restart_summary([_rec("ibgateway")], {k: True for k in ALL_KEYS})

    cascade_lines = [ln for ln in message.splitlines() if "cascaded from" in ln]
    assert len(cascade_lines) == 2
    columns = {ln.index("cascaded from") for ln in cascade_lines}
    assert len(columns) == 1, f"cascade column ragged at {columns}"


# ── event_log double-write ───────────────────────────────────────────────────


def test_rogue_events_opt_out_of_the_orchestrator_mirror():
    """The reconciler writes its own event_log row; a mirror would double it."""
    import re
    from pathlib import Path

    source = Path("app/services/position_reconciler.py").read_text()
    # Both the detection and the resolution event must opt out.
    assert source.count("mirror_to_event_log=False") == 2

    # And each must sit inside a NormalizedEvent that the reconciler also
    # appended to event_log itself.
    assert re.search(r"ROGUE_TRADE_DETECTED[\s\S]{0,2000}?mirror_to_event_log=False", source)
    assert re.search(r"ROGUE_TRADE_RESOLVED[\s\S]{0,2000}?mirror_to_event_log=False", source)


def test_normalized_event_mirrors_by_default():
    """Opting out must be explicit; existing producers keep the mirror."""
    from app.services.notification.types import NormalizedEvent

    event = NormalizedEvent(event_type="X", title="t", message="m")
    assert event.mirror_to_event_log is True
