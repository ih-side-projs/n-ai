"""Local execution target: one process group on the host.

Hardening without a container:

* the environment is scrubbed (:func:`nai.exec.target.scrub_env`);
* ``run`` commands are wrapped in ``unshare -rn`` when the kernel allows it, so
  the child gets a private network namespace with no interfaces;
* ``prlimit`` caps address space, CPU time and (inside a user namespace)
  process count;
* the child runs in its own session, so cancellation kills the whole group;
* output is read incrementally and kept as head+tail.
"""

from __future__ import annotations

import os
import shutil
import signal
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
    OutputCapture,
    RunResult,
)

#: Address-space cap for local runs.  Generous on purpose: a too-small
#: ``RLIMIT_AS`` breaks toolchains that reserve large mappings (JVM, Node).
DEFAULT_MEMORY_BYTES = 4 * 1024 * 1024 * 1024
#: Process count inside the user namespace.
DEFAULT_PIDS_LIMIT = 512
#: How long to wait for the reader thread after the child exits.
_READER_GRACE = 2.0

# Probing `unshare -rn` costs a subprocess; cache the answer per interpreter.
_UNSHARE_CACHE: dict[str, str | None] = {}


def _unshare_path() -> str | None:
    """Return the ``unshare`` path when ``unshare -rn`` works, else None."""
    if "path" not in _UNSHARE_CACHE:
        binary = shutil.which("unshare")
        if not binary:
            _UNSHARE_CACHE["path"] = None
        else:
            try:
                probe = subprocess.run(
                    [binary, "-rn", "true"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                )
                _UNSHARE_CACHE["path"] = binary if probe.returncode == 0 else None
            except (OSError, subprocess.SubprocessError):
                _UNSHARE_CACHE["path"] = None
    return _UNSHARE_CACHE["path"]


class LocalTarget(BaseTarget):
    """Run commands as a process group on the host."""

    def __init__(
        self,
        root: str | Path,
        *,
        default_timeout: float = 600.0,
        default_cap: int = 1 << 20,
        memory_bytes: int = DEFAULT_MEMORY_BYTES,
        pids_limit: int = DEFAULT_PIDS_LIMIT,
        isolate_network: bool = True,
        on_note=None,
        sink=None,
    ):
        super().__init__(root=Path(root), default_timeout=default_timeout, default_cap=default_cap)
        self.memory_bytes = int(memory_bytes)
        self.pids_limit = int(pids_limit)
        self.isolate_network = bool(isolate_network)
        self.on_note = on_note
        self.sink = sink
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._cancelled = False
        self._unshare = self._probe_unshare() if self.isolate_network else None
        self._prlimit = shutil.which("prlimit")
        # ``ulimit`` is the fallback when util-linux's prlimit is unavailable.
        # It needs a shell, but only as an execution wrapper: the command itself
        # is still passed as argv via ``exec "$@"``.
        self._bash = shutil.which("bash") if not self._prlimit else None

    # -- capability probes ---------------------------------------------------
    def _probe_unshare(self) -> str | None:
        """Return the ``unshare`` path when ``unshare -rn`` actually works."""
        return _unshare_path()

    def _has_limits(self) -> bool:
        return bool(self._prlimit or self._bash)

    def capabilities(self) -> Capabilities:
        return Capabilities(
            mode="local",
            network_off=bool(self._unshare),
            filesystem_isolated=False,
            memory_limit=self._has_limits(),
            cpu_limit=self._has_limits(),
            pids_limit=bool(self._has_limits() and self._unshare),
            capability_drop=False,
            no_new_privileges=False,
            user_namespace=bool(self._unshare),
            real_cancel=True,
            streaming=True,
        )

    def describe(self) -> str:
        net = "off via unshare -rn" if self._unshare else "off requested, unavailable (weaker mode)"
        if self._prlimit:
            limits = f"prlimit --as={self.memory_bytes} --cpu, pids={self.pids_limit}"
        elif self._bash:
            limits = f"ulimit -v={self.memory_bytes // 1024}K --cpu, pids={self.pids_limit}"
        else:
            limits = "no local limits"
        return f"local process group in {self.root} (network: {net}; {limits})"

    # -- cancellation --------------------------------------------------------
    def cancel(self) -> bool:
        """Kill the running process group.  Returns True when something ran."""
        with self._lock:
            proc = self._proc
            if proc is None:
                return False
            self._cancelled = True
        self._kill_group(proc)
        return True

    @staticmethod
    def _kill_group(proc: subprocess.Popen) -> None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                proc.kill()
            except OSError:
                pass
        except OSError:
            pass

    def close(self) -> None:
        """Idempotent: nothing to release for local runs."""
        return None

    # -- prefixing -----------------------------------------------------------
    def _limit_prefix(self, argv: list[str], timeout: float, use_userns: bool) -> list[str]:
        cpu_seconds = int(timeout) + 30
        if self._prlimit:
            args = [self._prlimit, f"--as={self.memory_bytes}", f"--cpu={cpu_seconds}"]
            if use_userns:
                args.append(f"--nproc={self.pids_limit}")
            args.append("--")
            return [*args, *argv]
        if self._bash:
            # ulimit fallback: the shell only applies limits and execs the argv.
            # ``2>/dev/null`` keeps a refused limit from aborting the command.
            script = f"ulimit -v {self.memory_bytes // 1024} 2>/dev/null; ulimit -t {cpu_seconds} 2>/dev/null"
            if use_userns:
                script += f"; ulimit -u {self.pids_limit} 2>/dev/null"
            script += '; exec "$@"'
            return [self._bash, "-c", script, "nai-exec", *argv]
        return argv

    def _network_prefix(self, argv: list[str], allow_network: bool) -> tuple[list[str], bool]:
        """Return (argv, weaker) for the resolved network mode."""
        if allow_network:
            return argv, False
        if self._unshare:
            return [self._unshare, "-rn", "--", *argv], False
        message = (
            "network isolation requested but `unshare -rn` is unavailable; "
            "the command runs without network isolation (weaker mode)"
        )
        self._note(message)
        if not self._warned_network:
            self._warned_network = True
            if self.on_note is not None:
                try:
                    self.on_note("exec.weaker_isolation", message)
                except Exception:  # pragma: no cover - a note must never break a run
                    pass
        return argv, True

    # -- run -----------------------------------------------------------------
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
            return RunResult(argv=(), exit_code=2, output="", error="empty argv", mode="local")
        limit = self._clamp_timeout(timeout)
        allow_network = self._network_for(phase, network)
        # Limits go *inside* the user namespace so RLIMIT_NPROC counts that
        # namespace's processes rather than every process the real uid owns.
        command = self._limit_prefix(parts, limit, use_userns=bool(self._unshare and not allow_network))
        command, weaker = self._network_prefix(command, allow_network)
        env = self._env(env_overrides)
        workdir = str(cwd or self.root)
        capture = OutputCapture(cap=int(output_cap or self.default_cap), sink=self.sink)
        started = time.monotonic()
        timed_out = False
        cancelled = False
        error = ""

        try:
            proc = subprocess.Popen(
                command,
                cwd=workdir,
                env=env,
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
                mode="local",
                network="on" if allow_network else "off",
                weaker_isolation=weaker,
                error=f"{type(exc).__name__}: {exc}",
            )

        with self._lock:
            self._proc = proc
            self._cancelled = False

        reader = threading.Thread(target=self._pump, args=(proc, capture), daemon=True)
        reader.start()

        deadline = started + limit
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
            self._kill_group(proc)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            self._kill_group(proc)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                error = "process did not exit after SIGKILL"

        reader.join(timeout=_READER_GRACE)
        with self._lock:
            self._proc = None
            was_cancelled = self._cancelled
            self._cancelled = False

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
            mode="local",
            network="on" if allow_network else "off",
            weaker_isolation=weaker,
            error=error,
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
