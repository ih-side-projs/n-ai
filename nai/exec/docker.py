"""Docker execution target: named, hardened containers.

Two phases, two container shapes:

* ``setup`` — a one-shot container with the network enabled, so an adapter can
  install its toolchain;
* ``run`` — a long-lived container created with ``--network none`` and the full
  hardening set, driven with ``docker exec``.

Cancellation is real: the container is named, so ``docker kill`` stops the
command immediately and the next run recreates the container lazily.  The
workspace is a bind mount, so files (including the virtualenv) survive.
"""

from __future__ import annotations

import atexit
import itertools
import os
import subprocess
import threading
import time
from collections.abc import Iterable, Mapping
from pathlib import Path

from .target import (
    CANCEL_EXIT,
    PHASE_RUN,
    TIMEOUT_EXIT,
    BaseTarget,
    Capabilities,
    ExecError,
    OutputCapture,
    RunResult,
)

_SEQ = itertools.count(1)
_READER_GRACE = 2.0


class DockerTarget(BaseTarget):
    """Run commands in a named container with no network and hard limits."""

    def __init__(
        self,
        root: str | Path,
        image: str,
        *,
        default_timeout: float = 600.0,
        default_cap: int = 1 << 20,
        memory: str = "2g",
        cpus: str = "2",
        pids_limit: int = 512,
        user: str | None = None,
        on_note=None,
        sink=None,
    ):
        super().__init__(root=Path(root), default_timeout=default_timeout, default_cap=default_cap)
        self.image = image
        self.memory = memory
        self.cpus = cpus
        self.pids_limit = int(pids_limit)
        self.user = user or f"{os.getuid()}:{os.getgid()}"
        self.on_note = on_note
        self.sink = sink
        self.container: str | None = None
        self._setup_container: str | None = None
        #: The container the *current* run is using.  ``cancel()`` must kill this
        #: one: a setup run uses a one-shot container, not the long-lived run
        #: container, so guessing from ``self.container`` would kill the wrong one.
        self._active_container: str | None = None
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._cancelled = False
        atexit.register(self.close)
        # Create the long-lived run container up front: callers read
        # ``target.container`` to know commands execute in Docker mode.
        self._start_container()

    # -- flags ---------------------------------------------------------------
    def _hardening_flags(self) -> list[str]:
        return [
            "--memory", self.memory,
            "--cpus", self.cpus,
            "--pids-limit", str(self.pids_limit),
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--user", self.user,
        ]

    def _mount_flags(self, workdir: str = "/work") -> list[str]:
        return ["-v", f"{self.root}:/work", "-w", workdir]

    def _container_cwd(self, cwd: str | Path | None) -> str:
        """Map a host working directory under the workspace to its container path.

        The workspace is mounted at ``/work``; a subdirectory maps to
        ``/work/<rel>``.  Anything outside the workspace falls back to ``/work``
        so a caller can never escape the bind mount.
        """
        if cwd is None:
            return "/work"
        try:
            rel = Path(cwd).resolve().relative_to(self.root.resolve())
        except (ValueError, OSError):
            return "/work"
        text = rel.as_posix()
        return "/work" if text in ("", ".") else f"/work/{text}"

    def _env_flags(self, env: Mapping[str, str]) -> list[str]:
        flags: list[str] = []
        for key, value in env.items():
            flags.extend(["-e", f"{key}={value}"])
        return flags

    def capabilities(self) -> Capabilities:
        return Capabilities(
            mode="docker",
            network_off=True,
            filesystem_isolated=True,
            memory_limit=True,
            cpu_limit=True,
            pids_limit=True,
            capability_drop=True,
            no_new_privileges=True,
            user_namespace=True,
            real_cancel=True,
            streaming=True,
        )

    def describe(self) -> str:
        return (
            f"docker image {self.image} in {self.root} "
            f"(memory={self.memory} cpus={self.cpus} pids={self.pids_limit} "
            f"user={self.user} cap-drop=ALL no-new-privileges; run network=off)"
        )

    # -- container lifecycle -------------------------------------------------
    def _start_container(self) -> str:
        """Create the long-lived ``run`` container (network off)."""
        name = f"nai-{os.getpid()}-{next(_SEQ)}"
        argv = [
            "docker", "run", "-d", "--rm", "--init", "--name", name,
            "--network", "none",
            *self._hardening_flags(),
            *self._mount_flags(),
            self.image, "sleep", "infinity",
        ]
        subprocess.run(argv, check=True, stdout=subprocess.DEVNULL)
        self.container = name
        return name

    def _ensure_container(self) -> str:
        if self.container is None:
            self._start_container()
        assert self.container is not None
        return self.container

    def close(self) -> None:
        """Remove the run and setup containers (idempotent)."""
        with self._lock:
            name, self.container = self.container, None
            setup, self._setup_container = self._setup_container, None
            self._active_container = None
        for target in (name, setup):
            if target:
                subprocess.run(["docker", "rm", "-f", target], capture_output=True)

    def _kill(self, name: str) -> None:
        try:
            subprocess.run(["docker", "kill", name], capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError):  # pragma: no cover - defensive
            pass

    def cancel(self) -> bool:
        """Kill the container running the command (named container, ``docker kill``)."""
        with self._lock:
            proc = self._proc
            if proc is None:
                return False
            self._cancelled = True
            name = self._active_container
            # A killed container cannot be reused: forget it so the next run
            # recreates it instead of failing ``docker exec`` on a dead name.
            if name is not None and name == self.container:
                self.container = None
            elif name is not None and name == self._setup_container:
                self._setup_container = None
        if name:
            self._kill(name)
        try:
            proc.kill()
        except OSError:  # pragma: no cover - defensive
            pass
        return True

    # -- run -----------------------------------------------------------------
    def _run_argv(
        self,
        argv: list[str],
        env: Mapping[str, str],
        allow_network: bool,
        cwd: str | Path | None = None,
    ) -> list[str]:
        workdir = self._container_cwd(cwd)
        if allow_network:
            # setup: one-shot container with the network enabled (bridge)
            name = f"nai-setup-{os.getpid()}-{next(_SEQ)}"
            self._setup_container = name
            self._active_container = name
            return [
                "docker", "run", "--rm", "--init", "--name", name,
                "--network", "bridge",
                *self._hardening_flags(),
                *self._mount_flags(workdir),
                *self._env_flags(env),
                self.image, *argv,
            ]
        name = self._ensure_container()
        self._active_container = name
        return ["docker", "exec", "-w", workdir, *self._env_flags(env), name, *argv]

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
        parts = [str(part) for part in argv]
        if not parts:
            return RunResult(argv=(), exit_code=2, output="", error="empty argv", mode="docker")
        limit = self._clamp_timeout(timeout)
        allow_network = self._network_for(phase, network)
        env = self._env(env_overrides)
        capture = OutputCapture(cap=int(output_cap or self.default_cap), sink=self.sink)
        started = time.monotonic()

        try:
            command = self._run_argv(parts, env, allow_network, cwd)
        except (OSError, subprocess.CalledProcessError) as exc:
            return RunResult(
                argv=tuple(parts),
                exit_code=127,
                output="",
                duration=time.monotonic() - started,
                mode="docker",
                network="on" if allow_network else "off",
                error=f"{type(exc).__name__}: {exc}",
            )

        try:
            proc = subprocess.Popen(
                command,
                cwd=str(cwd or self.root),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                bufsize=1,
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            return RunResult(
                argv=tuple(parts),
                exit_code=127,
                output="",
                duration=time.monotonic() - started,
                mode="docker",
                network="on" if allow_network else "off",
                error=f"{type(exc).__name__}: {exc}",
            )

        with self._lock:
            self._proc = proc
            self._cancelled = False

        reader = threading.Thread(target=self._pump, args=(proc, capture), daemon=True)
        reader.start()

        deadline = started + limit
        timed_out = False
        cancelled = False
        while True:
            # Cancellation is checked before the exit code so a kill requested
            # while the command is dying is still reported as a cancellation.
            if cancel_event is not None and getattr(cancel_event, "is_set", lambda: False)():
                cancelled = True
                break
            with self._lock:
                if self._cancelled:
                    cancelled = True
                    break
            if proc.poll() is not None:
                break
            if time.monotonic() >= deadline:
                timed_out = True
                break
            time.sleep(0.05)

        if timed_out or cancelled:
            name = self._active_container
            if name:
                self._kill(name)
                if not allow_network:
                    with self._lock:
                        self.container = None
            try:
                proc.kill()
            except OSError:  # pragma: no cover - defensive
                pass

        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

        reader.join(timeout=_READER_GRACE)
        with self._lock:
            self._proc = None
            was_cancelled = self._cancelled
            self._cancelled = False
            self._active_container = None
            if allow_network:
                self._setup_container = None

        output = capture.text()
        if timed_out:
            exit_code = TIMEOUT_EXIT
            output = f"{output}\n[timed out after {int(limit)}s and was killed]"
        elif cancelled or was_cancelled:
            exit_code = CANCEL_EXIT
            output = f"{output}\n[cancelled]"
        else:
            exit_code = int(proc.returncode if proc.returncode is not None else 1)

        return RunResult(
            argv=tuple(parts),
            exit_code=exit_code,
            output=output,
            duration=time.monotonic() - started,
            timed_out=timed_out,
            cancelled=cancelled or was_cancelled,
            truncated=capture.truncated,
            mode="docker",
            network="on" if allow_network else "off",
            error="",
            notes=tuple(self.notes),
        )

    @staticmethod
    def _pump(proc: subprocess.Popen, capture: OutputCapture) -> None:
        stream = proc.stdout
        if stream is None:  # pragma: no cover - defensive
            return
        try:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    break
                capture.feed(chunk)
        except (OSError, ValueError):  # pragma: no cover - closed pipe on kill
            pass
        finally:
            try:
                stream.close()
            except OSError:  # pragma: no cover - defensive
                pass


def require_docker() -> str:
    """Return the ``docker`` path or raise :class:`ExecError`."""
    import shutil

    binary = shutil.which("docker")
    if not binary:
        raise ExecError("docker is not installed or not on PATH")
    return binary
