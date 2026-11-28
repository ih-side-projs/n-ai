"""``RemoteTarget``: an ``ExecutionTarget`` that runs a named recipe on a worker.

The orchestrator snapshots the project, queues a task, waits (checking for
cancellation and an overall timeout) and returns the worker's exit code and raw
output.  Parsing still happens on Linux via the language adapter.

Waiting for a capable worker raises :class:`WorkerUnavailable`; a lost lease or
a broken transport raises :class:`InfrastructureError`.  Neither counts as a
build attempt (see the runner's infrastructure branch).
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from ..exec.target import (
    CANCEL_EXIT,
    PHASE_RUN,
    TIMEOUT_EXIT,
    BaseTarget,
    Capabilities,
    OutputCapture,
    RunResult,
)
from .errors import InfrastructureError, WorkerUnavailable
from .recipes import spec_for, validate_params
from .snapshots import snapshot_tree


class RemoteTarget(BaseTarget):
    """Queue one recipe and wait for a worker to finish it."""

    def __init__(
        self,
        root: str | Path,
        store: Any,
        *,
        project_id: str,
        recipe: str,
        params: Mapping[str, Any] | None = None,
        required_caps: Iterable[str] = (),
        job_id: str | None = None,
        snapshot_ignore: Iterable[str] = (),
        snapshot_dir: str | Path | None = None,
        cache_dirs: Iterable[str] = (),
        worker_wait_seconds: float = 60.0,
        poll_seconds: float = 0.25,
        expire_seconds: float = 15.0,
        keep_snapshots: int = 5,
        default_timeout: float = 600.0,
        default_cap: int = 2 * 1024 * 1024,
        on_note: Any = None,
    ):
        super().__init__(root=Path(root), default_timeout=default_timeout, default_cap=default_cap)
        self.store = store
        self.project_id = str(project_id)
        self.recipe = str(recipe)
        self.params = dict(params or {})
        self.required_caps = tuple(sorted({str(c).strip().lower() for c in required_caps if str(c).strip()}))
        self.job_id = job_id
        self.snapshot_ignore = tuple(str(p) for p in snapshot_ignore)
        self.snapshot_dir = Path(snapshot_dir) if snapshot_dir else (Path(root) / ".nai" / "snapshots")
        self.cache_dirs = tuple(str(p) for p in cache_dirs)
        self.worker_wait_seconds = float(worker_wait_seconds)
        self.poll_seconds = max(0.01, float(poll_seconds))
        self.expire_seconds = max(1.0, float(expire_seconds))
        self.keep_snapshots = int(keep_snapshots)
        self.on_note = on_note
        self._cancelled = False
        self._task_id: str | None = None
        self._last_expire = 0.0
        # Validate before a worker ever sees the task.
        self.spec = spec_for(self.recipe)
        self.clean_params = validate_params(self.spec, self.params)

    # -- introspection -------------------------------------------------------
    def capabilities(self) -> Capabilities:
        return Capabilities(mode="remote", real_cancel=True, streaming=False)

    def describe(self) -> str:
        caps = ", ".join(self.required_caps) or "none"
        return (
            f"remote recipe {self.recipe} in {self.root} (caps={caps}, "
            f"wait={self.worker_wait_seconds:g}s, workers via nai remote serve)"
        )

    def cancel(self) -> bool:
        """Ask the task to stop; the worker's next heartbeat carries the flag."""
        self._cancelled = True
        if self._task_id:
            try:
                self.store.cancel_remote_task(self._task_id, reason="cancelled by owner")
            except Exception:  # noqa: BLE001 - cancellation must never raise
                return False
            return True
        return False

    def close(self) -> None:
        """Nothing to release: tasks and snapshots outlive one target."""
        return None

    def _note(self, message: str) -> None:
        super()._note(message)
        if self.on_note is not None:
            try:
                self.on_note("remote.note", message)
            except Exception:  # noqa: BLE001 - a note must never break a run
                pass

    # -- running -------------------------------------------------------------
    def run(
        self,
        argv: Iterable[str],
        cwd: str | None = None,
        env_overrides: Mapping[str, object] | None = None,
        timeout: float | None = None,
        cancel_event: object | None = None,
        output_cap: int | None = None,
        *,
        phase: str = PHASE_RUN,
        network: bool | None = None,
    ) -> RunResult:
        """Snapshot, enqueue and wait.  *argv* is recorded but not sent.

        A worker only executes the named recipe; the orchestrator's argv is
        kept for the result and the verification gate.
        """
        recorded = tuple(str(part) for part in argv)
        budget = self._clamp_timeout(timeout)
        snapshot = snapshot_tree(
            Path(str(self.root)),
            self.snapshot_ignore,
            dest_dir=self.snapshot_dir,
            keep=self.keep_snapshots,
        )
        blob = {"params": self.clean_params, "cache_dirs": list(self.cache_dirs)}
        task = self.store.create_remote_task(
            self.project_id,
            self.recipe,
            snapshot.sha,
            job_id=self.job_id,
            params=blob,
            required_caps=self.required_caps,
        )
        self._task_id = str(task["id"])
        self._note(
            f"queued {self.recipe} as {self._task_id} (snapshot {snapshot.sha[:12]}, "
            f"caps={', '.join(self.required_caps) or 'none'})"
        )
        try:
            return self._wait(recorded, budget, cancel_event, output_cap)
        finally:
            self._task_id = None

    def _cancel_requested(self, cancel_event: object | None) -> bool:
        if self._cancelled:
            return True
        return bool(cancel_event is not None and getattr(cancel_event, "is_set", lambda: False)())


    def _wait(
        self,
        argv: tuple[str, ...],
        budget: float,
        cancel_event: object | None,
        output_cap: int | None,
    ) -> RunResult:
        started = time.monotonic()
        wait_deadline = started + self.worker_wait_seconds
        exec_deadline: float | None = None
        while True:
            if self._cancel_requested(cancel_event):
                if self._task_id:
                    self.store.cancel_remote_task(self._task_id, reason="cancelled by owner")
                return RunResult(argv=argv, exit_code=CANCEL_EXIT, output="", cancelled=True,
                                 mode="remote", network="worker", error="cancelled")
            now = time.monotonic()
            if now - self._last_expire >= self.expire_seconds:
                self._last_expire = now
                try:
                    self.store.expire_remote_tasks()
                except Exception:  # noqa: BLE001 - a sweeper failure must not fail the run
                    pass
            task = self.store.get_remote_task(self._task_id) if self._task_id else None
            if task is None:
                raise InfrastructureError(f"remote task {self._task_id} disappeared")
            status = str(task.get("status") or "")
            if status == "done":
                return self._result(argv, task, output_cap)
            if status == "failed":
                raise InfrastructureError(str(task.get("error") or "remote task failed"))
            if status == "cancelled":
                return RunResult(argv=argv, exit_code=CANCEL_EXIT, output="", cancelled=True,
                                 mode="remote", network="worker", error=str(task.get("error") or "cancelled"))
            if status == "claimed" and exec_deadline is None:
                exec_deadline = time.monotonic() + budget
            if exec_deadline is not None and time.monotonic() >= exec_deadline:
                self.store.cancel_remote_task(self._task_id, reason="timed out")
                return RunResult(argv=argv, exit_code=TIMEOUT_EXIT, output="", timed_out=True,
                                 duration=time.monotonic() - started, mode="remote", network="worker",
                                 error="remote command timed out")
            if status == "queued":
                if time.monotonic() >= wait_deadline:
                    online = self.store.workers_with_caps(self.required_caps)
                    if not online:
                        raise WorkerUnavailable(
                            f"waiting for worker ({', '.join(self.required_caps) or 'any'})"
                        )
                    # A capable worker exists but has not claimed the task: the
                    # command budget bounds how long this job waits for a slot.
                    if time.monotonic() >= started + budget:
                        raise WorkerUnavailable(
                            f"waiting for worker ({', '.join(self.required_caps) or 'any'}) "
                            f"to claim {self.recipe}"
                        )
            time.sleep(self.poll_seconds)

    def _result(self, argv: tuple[str, ...], task: dict[str, Any], output_cap: int | None) -> RunResult:
        payload = task.get("result") or {}
        capture = OutputCapture(cap=int(output_cap or self.default_cap))
        capture.feed(str(payload.get("output") or ""))
        notes = []
        for name, version in sorted((payload.get("toolchains") or {}).items()):
            notes.append(f"toolchain {name} {version}")
        if payload.get("truncated"):
            notes.append("output truncated")
        return RunResult(
            argv=argv,
            exit_code=int(payload.get("exit_code") or 0),
            output=capture.text(),
            duration=float(payload.get("duration") or 0.0),
            timed_out=bool(payload.get("timed_out")),
            truncated=capture.truncated or bool(payload.get("truncated")),
            mode="remote",
            network="worker",
            error=str(payload.get("error") or ""),
            notes=tuple(notes),
        )

