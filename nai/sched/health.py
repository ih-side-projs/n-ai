"""SCH-9: runner health file and single-runner lock. The web process reads `runner.json`; it never talks to the runner."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .util import parse_ts

HEALTH_FILE = "runner.json"
LOCK_FILE = "runner.lock"


class RunnerAlreadyRunning(RuntimeError):
    pass


def write_health(state_dir: Path, data: dict) -> None:
    """Atomic replace, so a reader never sees half a file."""
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    tmp = state_dir / (HEALTH_FILE + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, state_dir / HEALTH_FILE)


def read_health(state_dir: Path, stale_after: float = 15.0) -> dict | None:
    """The last health snapshot plus `alive` (heartbeat newer than stale_after seconds and state not stopped)."""
    try:
        data = json.loads((Path(state_dir) / HEALTH_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    hb = parse_ts(data.get("heartbeat"))
    age = (datetime.now(timezone.utc) - hb).total_seconds() if hb else None
    data["heartbeat_age_seconds"] = age
    data["alive"] = bool(age is not None and age <= stale_after and data.get("state") != "stopped")
    return data


class RunnerLock:
    """Only one runner may write to a store (STO-3). flock is released by the OS if the process dies."""

    def __init__(self, state_dir: Path):
        self.path = Path(state_dir) / LOCK_FILE
        self._fh = None

    def acquire(self) -> None:
        try:
            import fcntl
        except ImportError:        # platform without fcntl: no lock
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.seek(0)
            holder = fh.read().strip() or "unknown pid"
            fh.close()
            raise RunnerAlreadyRunning(f"another runner holds {self.path} (pid {holder})")
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        self._fh = fh

    def release(self) -> None:
        if self._fh:
            try:
                self._fh.close()
            finally:
                self._fh = None
