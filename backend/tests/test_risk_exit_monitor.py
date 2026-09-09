"""RiskExitMonitor gates, precedence, shadow mode, retries, and session PnL."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.db.repositories.position_repository import PositionRepository
from app.services.pnl import PairPnlSnapshot
from app.services.risk_exit_monitor import RiskExitMonitor
from app.services.risk_exit_rules import REASON_ACCOUNT_STOP, REASON_PAIR_STOP
from app.services.session_clock import ET

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=ET)
RTH_OPEN = datetime(2026, 9, 9, 9, 30, tzinfo=ET)
RTH_CLOSE = datetime(2026, 9, 9, 16, 0, tzinfo=ET)
MONO_NOW = 1_000_000.0


class FakeClock:
    def now(self) -> datetime:
        return NOW

    def rth_open(self, _day):
        return RTH_OPEN

    def rth_close(self, _day):
        return RTH_CLOSE


class _Begin:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, accounts, allocations):
        self.accounts = accounts
        self.allocations = allocations

    def begin(self):
        return _Begin()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt):
        compiled = str(stmt).lower()
        rows: list = []
        if "from accounts" in compiled:
            rows = self.accounts
        elif "from allocations" in compiled:
            rows = self.allocations
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: list(rows)))


class FakeFactory:
    def __init__(self, accounts, allocations):
        self.accounts = accounts
        self.allocations = allocations

    def __call__(self):
        return FakeSession(self.accounts, self.allocations)


class FakeLive:
    def __init__(self, snaps: dict[tuple[int, str], PairPnlSnapshot]):
        self.snaps = snaps

    def get_pair_pnl(self, account_id: int, trade_id: str) -> PairPnlSnapshot | None:
        return self.snaps.get((account_id, trade_id))


class FakeCloser:
    def __init__(self, *, success: bool = True, status: str = "CLOSED"):
        self.calls: list[tuple] = []
        self.success = success
        self.status = status

    async def close_pair(self, account_id, trade_id, *, exit_reason=None):
        self.calls.append((account_id, trade_id, exit_reason))
        return SimpleNamespace(success=self.success, status=self.status)


class FakeKillSwitch:
    def __init__(self):
        self.initiated: list[tuple] = []
        self.flattened: list = []

    async def initiate_square_off(self, account_id, requested_by="operator"):
        self.initiated.append((account_id, requested_by))
        return SimpleNamespace(operation_id="op-1"), True

    async def execute_flatten_operation_background(self, operation_id):
        self.flattened.append(operation_id)


def _account(**overrides):
    data = {
        "id": 1,
        "name": "A",
        "ibkr_account": "DU1",
        "total_margin": Decimal(100000),
        "enabled": True,
        "daily_target": Decimal(2000),
        "daily_stop": Decimal(1000),
        "daily_target_unit": "ABSOLUTE",
        "daily_stop_unit": "ABSOLUTE",
        "account_risk_enabled": False,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def _allocation(**overrides):
    data = {
        "account_id": 1,
        "strategy_id": "model_blue",
        "exit_automation_enabled": True,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def _position(**overrides):
    data = {
        "account_id": 1,
        "trade_id": "T1",
        "strategy_id": "model_blue",
        "target": Decimal(500),
        "stop": Decimal(250),
        "time_limit": 3600,
        "target_unit": "ABSOLUTE",
        "stop_unit": "ABSOLUTE",
        "opened_at": NOW - timedelta(seconds=120),
        "leg_a_signed_qty": Decimal(10),
        "leg_a_entry_mark": Decimal(100),
        "leg_b_signed_qty": Decimal(-10),
        "leg_b_entry_mark": Decimal(100),
        "exit_automation_enabled": True,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def _fresh_snap(pnl: Decimal) -> PairPnlSnapshot:
    return PairPnlSnapshot(pnl=pnl, updated_at_mono=MONO_NOW, all_legs_marked=True)


def _stale_snap(pnl: Decimal) -> PairPnlSnapshot:
    return PairPnlSnapshot(
        pnl=pnl, updated_at_mono=MONO_NOW - 60.0, all_legs_marked=True
    )


@pytest.fixture
def repo_patches(monkeypatch):
    state = {
        "open_rows": [],
        "realised": {},
        "since": [],
        "exit_reasons": [],
        "events": [],
    }

    async def list_open(self):
        return list(state["open_rows"])

    async def sum_realised(self, *, account_id, since):
        state["since"].append(since)
        return Decimal(str(state["realised"].get(account_id, 0)))

    async def set_reason(self, *, account_id, reason, trade_id=None):
        state["exit_reasons"].append((account_id, reason, trade_id))
        return 1

    async def append(self, **kwargs):
        state["events"].append(kwargs)

    monkeypatch.setattr(PositionRepository, "list_open", list_open)
    monkeypatch.setattr(PositionRepository, "sum_realised_closed_since", sum_realised)
    monkeypatch.setattr(PositionRepository, "set_exit_reason", set_reason)
    monkeypatch.setattr(
        "app.services.risk_exit_monitor.EventRepository.append", append
    )
    monkeypatch.setattr(
        "app.services.risk_exit_monitor.is_account_kill_switch_active",
        lambda _aid: False,
    )
    return state


def _monitor(*, accounts, allocations, live, closer, ks, shadow=False):
    return RiskExitMonitor(
        FakeFactory(accounts, allocations),
        client=SimpleNamespace(is_connected=lambda: True),
        live_pnl=live,
        interval_sec=2.0,
        max_pnl_staleness_sec=15.0,
        max_retries=3,
        enabled=True,
        shadow_mode=shadow,
        session_clock=FakeClock(),
        pair_closer=closer,
        kill_switch=ks,
        now_fn=lambda: NOW,
        monotonic_fn=lambda: MONO_NOW,
    )


@pytest.mark.asyncio
async def test_pair_stop_closes(repo_patches) -> None:
    pos = _position()
    repo_patches["open_rows"] = [pos]
    closer = FakeCloser()
    ks = FakeKillSwitch()
    mon = _monitor(
        accounts=[_account()],
        allocations=[_allocation()],
        live=FakeLive({(1, "T1"): _fresh_snap(Decimal(-250))}),
        closer=closer,
        ks=ks,
    )
    await mon.run_once()
    assert closer.calls == [(1, "T1", REASON_PAIR_STOP)]
    assert ks.initiated == []
    kinds = [e["kind"] for e in repo_patches["events"]]
    assert "PAIR_EXIT_TRIGGERED" in kinds


@pytest.mark.asyncio
async def test_shadow_mode_emits_without_actuators(repo_patches) -> None:
    repo_patches["open_rows"] = [_position()]
    closer = FakeCloser()
    ks = FakeKillSwitch()
    mon = _monitor(
        accounts=[_account()],
        allocations=[_allocation()],
        live=FakeLive({(1, "T1"): _fresh_snap(Decimal(-250))}),
        closer=closer,
        ks=ks,
        shadow=True,
    )
    await mon.run_once()
    assert closer.calls == []
    assert ks.initiated == []
    assert any(e["kind"] == "PAIR_EXIT_TRIGGERED" for e in repo_patches["events"])
    assert repo_patches["events"][0]["detail"]["shadow"] is True


@pytest.mark.asyncio
async def test_stale_pnl_skips_stop_but_time_limit_still_fires(repo_patches) -> None:
    pos = _position(time_limit=10, opened_at=NOW - timedelta(seconds=30))
    repo_patches["open_rows"] = [pos]
    closer = FakeCloser()
    mon = _monitor(
        accounts=[_account()],
        allocations=[_allocation()],
        live=FakeLive({(1, "T1"): _stale_snap(Decimal(-999))}),
        closer=closer,
        ks=FakeKillSwitch(),
    )
    await mon.run_once()
    assert len(closer.calls) == 1
    assert closer.calls[0][2] == "PAIR_TIME_LIMIT"


@pytest.mark.asyncio
async def test_stale_pnl_skips_pair_stop(repo_patches) -> None:
    repo_patches["open_rows"] = [_position()]
    closer = FakeCloser()
    mon = _monitor(
        accounts=[_account()],
        allocations=[_allocation()],
        live=FakeLive({(1, "T1"): _stale_snap(Decimal(-999))}),
        closer=closer,
        ks=FakeKillSwitch(),
    )
    await mon.run_once()
    assert closer.calls == []


@pytest.mark.asyncio
async def test_account_precedence_skips_pair_close(repo_patches) -> None:
    repo_patches["open_rows"] = [_position()]
    repo_patches["realised"] = {1: Decimal(0)}
    closer = FakeCloser()
    ks = FakeKillSwitch()
    mon = _monitor(
        accounts=[_account(account_risk_enabled=True, daily_stop=Decimal(100))],
        allocations=[_allocation()],
        live=FakeLive({(1, "T1"): _fresh_snap(Decimal(-250))}),
        closer=closer,
        ks=ks,
    )
    await mon.run_once()
    assert ks.initiated == [(1, "auto_risk")]
    assert ks.flattened == ["op-1"]
    assert closer.calls == []
    assert any(e["kind"] == "ACCOUNT_RISK_BREACH" for e in repo_patches["events"])
    assert repo_patches["exit_reasons"][0][1] == REASON_ACCOUNT_STOP


@pytest.mark.asyncio
async def test_armed_account_skipped(repo_patches, monkeypatch) -> None:
    repo_patches["open_rows"] = [_position()]
    monkeypatch.setattr(
        "app.services.risk_exit_monitor.is_account_kill_switch_active",
        lambda _aid: True,
    )
    closer = FakeCloser()
    ks = FakeKillSwitch()
    mon = _monitor(
        accounts=[_account(account_risk_enabled=True)],
        allocations=[_allocation()],
        live=FakeLive({(1, "T1"): _fresh_snap(Decimal(-250))}),
        closer=closer,
        ks=ks,
    )
    await mon.run_once()
    assert closer.calls == []
    assert ks.initiated == []


@pytest.mark.asyncio
async def test_retry_backoff_then_exhaust(repo_patches) -> None:
    repo_patches["open_rows"] = [_position()]
    closer = FakeCloser(success=False, status="FAILED")
    mon = _monitor(
        accounts=[_account()],
        allocations=[_allocation()],
        live=FakeLive({(1, "T1"): _fresh_snap(Decimal(-250))}),
        closer=closer,
        ks=FakeKillSwitch(),
    )
    await mon.run_once()
    await mon.run_once()
    assert len(closer.calls) == 1
    mon._pair_failures[(1, "T1")] = (1, MONO_NOW - 1)
    await mon.run_once()
    assert len(closer.calls) == 2
    mon._pair_failures[(1, "T1")] = (2, MONO_NOW - 1)
    await mon.run_once()
    assert len(closer.calls) == 3
    assert (1, "T1") in mon._pair_exhausted
    assert any(e["kind"] == "PAIR_EXIT_FAILED" for e in repo_patches["events"])
    await mon.run_once()
    assert len(closer.calls) == 3


@pytest.mark.asyncio
async def test_session_realised_uses_rth_open(repo_patches) -> None:
    repo_patches["open_rows"] = []
    repo_patches["realised"] = {1: Decimal(-1500)}
    ks = FakeKillSwitch()
    mon = _monitor(
        accounts=[_account(account_risk_enabled=True, daily_stop=Decimal(1000))],
        allocations=[],
        live=FakeLive({}),
        closer=FakeCloser(),
        ks=ks,
    )
    await mon.run_once()
    assert ks.initiated == [(1, "auto_risk")]
    assert repo_patches["since"]
    since = repo_patches["since"][0]
    assert since == RTH_OPEN.astimezone(UTC)


@pytest.mark.asyncio
async def test_disabled_master_switch_is_noop(repo_patches) -> None:
    repo_patches["open_rows"] = [_position()]
    closer = FakeCloser()
    mon = _monitor(
        accounts=[_account()],
        allocations=[_allocation()],
        live=FakeLive({(1, "T1"): _fresh_snap(Decimal(-250))}),
        closer=closer,
        ks=FakeKillSwitch(),
    )
    mon._enabled = False
    await mon.run_once()
    assert closer.calls == []


@pytest.mark.asyncio
async def test_disconnected_is_noop(repo_patches) -> None:
    repo_patches["open_rows"] = [_position()]
    closer = FakeCloser()
    mon = _monitor(
        accounts=[_account()],
        allocations=[_allocation()],
        live=FakeLive({(1, "T1"): _fresh_snap(Decimal(-250))}),
        closer=closer,
        ks=FakeKillSwitch(),
    )
    mon._client = SimpleNamespace(is_connected=lambda: False)
    await mon.run_once()
    assert closer.calls == []


@pytest.mark.asyncio
async def test_row_flag_off_skips_pair(repo_patches) -> None:
    repo_patches["open_rows"] = [_position(exit_automation_enabled=False)]
    closer = FakeCloser()
    mon = _monitor(
        accounts=[_account()],
        allocations=[_allocation(exit_automation_enabled=True)],
        live=FakeLive({(1, "T1"): _fresh_snap(Decimal(-250))}),
        closer=closer,
        ks=FakeKillSwitch(),
    )
    await mon.run_once()
    assert closer.calls == []


@pytest.mark.asyncio
async def test_row_flag_on_closes_without_allocation(repo_patches) -> None:
    repo_patches["open_rows"] = [_position(exit_automation_enabled=True)]
    closer = FakeCloser()
    mon = _monitor(
        accounts=[_account()],
        allocations=[],
        live=FakeLive({(1, "T1"): _fresh_snap(Decimal(-250))}),
        closer=closer,
        ks=FakeKillSwitch(),
    )
    await mon.run_once()
    assert closer.calls == [(1, "T1", REASON_PAIR_STOP)]
