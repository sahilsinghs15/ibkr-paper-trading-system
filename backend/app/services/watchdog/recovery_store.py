"""Persisted recovery budget — survives watchdog restart, fail-closed on corruption."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from app.services.watchdog.config import WatchdogSettings

logger = logging.getLogger(__name__)


class RecoveryBudgetStore:
    """Atomic JSON store for recovery_attempts per service.

    File format: { "gateway": ["2026-08-31T12:00:00+00:00", ...], ... }
    Corrupted file → treat as exhausted (fail closed) and log.
    Future timestamps (> now+60s) → treated as corrupted entry, ignored for count but flagged.
    """

    def __init__(self, settings: WatchdogSettings):
        self.settings = settings
        self.path = Path(settings.recovery_state_path)
        self._corrupted = False

    def load(self) -> dict[str, list[datetime]]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text())
            result: dict[str, list[datetime]] = {}
            now_ts = datetime.now(UTC).timestamp()
            for svc, lst in data.items():
                parsed: list[datetime] = []
                for s in lst:
                    try:
                        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=UTC)
                        # future timestamp check: > now+60s considered invalid (clock jump)
                        if dt.timestamp() > now_ts + 60:
                            logger.error("Recovery state future timestamp for %s: %s — ignoring", svc, s)
                            self._corrupted = True
                            continue
                        parsed.append(dt)
                    except Exception as exc:  # noqa: BLE001
                        logger.error("Recovery state parse error for %s: %s (%s)", svc, s, exc)
                        self._corrupted = True
                        continue
                result[svc] = parsed
            return result
        except Exception as exc:  # noqa: BLE001
            logger.error("Recovery state file corrupted at %s: %s — failing closed (budget exhausted)", self.path, exc)
            self._corrupted = True
            return {}

    def is_corrupted(self) -> bool:
        return self._corrupted

    def save(self, state: dict[str, list[datetime | str]]) -> None:
        # atomic write: tmp + fsync + replace — accepts datetime or already-serialized str for tests
        self.path.parent.mkdir(parents=True, exist_ok=True)
        serializable: dict[str, list[str]] = {}
        for k, v in state.items():
            lst: list[str] = []
            for dt in v:
                if isinstance(dt, str):
                    lst.append(dt)
                else:
                    lst.append(dt.astimezone(UTC).isoformat())
            serializable[k] = lst
        fd, tmp_path = tempfile.mkstemp(dir=str(self.path.parent), prefix=".watchdog_recovery_tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(serializable, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.path)
            # ensure dir fsync
            try:
                dir_fd = os.open(str(self.path.parent), os.O_DIRECTORY)
                os.fsync(dir_fd)
                os.close(dir_fd)
            except Exception:
                pass
        except Exception as exc:
            logger.error("Failed to persist recovery state: %s", exc)
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            raise

    def add_attempt(self, service: str, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        state = self.load()
        # if already corrupted, keep corrupted flag but still append (fail closed will handle)
        lst = state.get(service, [])
        lst.append(now)
        state[service] = lst
        self.save(state)

    def get_attempts(self, service: str) -> list[datetime]:
        state = self.load()
        return state.get(service, [])

    def is_exhausted(self, service: str, max_attempts: int, window_seconds: int) -> bool:
        # corrupted → fail closed: treat as exhausted
        if self._corrupted:
            return True
        attempts = self.get_attempts(service)
        cutoff = datetime.now(UTC).timestamp() - window_seconds
        # also handle future timestamps already filtered, but double-check
        recent = [dt for dt in attempts if dt.timestamp() > cutoff and dt.timestamp() <= datetime.now(UTC).timestamp() + 60]
        return len(recent) >= max_attempts
