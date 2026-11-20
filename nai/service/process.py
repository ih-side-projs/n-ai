"""Managed subprocess wrapper used by the supervisor (thin wrapper, no reimplementation)."""
from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class ManagedProcess:
    """One supervised child process with its own log file and lifecycle."""

    name: str
    command: list[str]
    cwd: Path
    env: dict[str, str]
    log_path: Path | None = None
    auto_restart: bool = True
    restart_limit: int = 3
    restart_backoff: float = 2.0
    stop_timeout: float = 15.0
    process: subprocess.Popen | None = field(default=None, repr=False)
    restarts: int = 0
    started_at: str | None = None
    last_exit: int | None = None
    last_error: str | None = None
    _log_handle: Any = field(default=None, repr=False)
    _retired: bool = False
    _stopped_explicitly: bool = False

    @property
    def pid(self) -> int | None:
        return self.process.pid if self.process else None

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self) -> None:
        if self.running:
            return
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_handle = open(self.log_path, "ab", buffering=0)
        self.process = subprocess.Popen(
            self.command,
            cwd=str(self.cwd),
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=self._log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # own process group so children get stopped too
        )
        self.started_at = _now()
        self.last_error = None
        self._stopped_explicitly = False

    def poll(self) -> str:
        """Return 'running', 'stopped', 'crashed', or 'disabled'."""
        if self._retired:
            return "disabled"
        if self.process is None:
            return "stopped"
        rc = self.process.poll()
        if rc is None:
            return "running"
        self.last_exit = rc
        self._close_log()
        return "stopped" if self._stopped_explicitly or rc == 0 else "crashed"

    def stop(self, timeout: float | None = None) -> bool:
        """SIGTERM the process group, then SIGKILL if needed. Returns True if exited."""
        if not self.process:
            return True
        timeout = self.stop_timeout if timeout is None else timeout
        self._stopped_explicitly = True
        proc = self.process
        if proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (OSError, ProcessLookupError):
                try:
                    proc.terminate()
                except OSError:
                    pass
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    proc.kill()
                proc.wait(timeout=5)
        self._close_log()
        self.last_exit = proc.returncode
        return True

    def restart(self) -> None:
        self.stop()
        self.process = None
        self.started_at = None
        self.start()

    def retire(self) -> None:
        """Permanently disable this process (e.g. after exceeding restart limit)."""
        self._retired = True

    def _close_log(self) -> None:
        if self._log_handle:
            try:
                self._log_handle.close()
            finally:
                self._log_handle = None

    def status(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "pid": self.pid,
            "state": self.poll(),
            "command": self.command,
            "started_at": self.started_at,
            "last_exit": self.last_exit,
            "restarts": self.restarts,
            "retired": self._retired,
            "log": str(self.log_path) if self.log_path else None,
            "last_error": self.last_error,
        }

    def tail_log(self, lines: int = 100) -> str:
        if not self.log_path or not self.log_path.exists():
            return ""
        with open(self.log_path, "r", encoding="utf-8", errors="replace") as fh:
            content = fh.readlines()
        return "".join(content[-lines:])
