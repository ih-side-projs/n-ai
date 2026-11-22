"""command-runner: run one shell command in the workspace, locally or in a Docker container (v1 + CORE-6)."""
from __future__ import annotations

import atexit
import itertools
import os
import signal
import subprocess
import threading
from pathlib import Path

from .gate import VerificationGate
from .terminal import log
from .textutil import truncate

EXTRA_ENV = {"CI": "1", "PYTHONUNBUFFERED": "1", "DEBIAN_FRONTEND": "noninteractive",
             "GIT_TERMINAL_PROMPT": "0", "PIP_DISABLE_PIP_VERSION_CHECK": "1",
             "PYTHONDONTWRITEBYTECODE": "1"}  # no stale .pyc after same-size edits
_SEQ = itertools.count(1)

# Secrets must never be handed to generated code or shell commands (G2).
_SECRET_ENV_PREFIXES = ("LLAMA_SWAP_KEY", "NAI_WEB_TOKEN", "OPENAI_API_KEY", "AWS_", "GITHUB_TOKEN")


def _safe_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not any(k.upper().startswith(p) for p in _SECRET_ENV_PREFIXES)}
    env.update(EXTRA_ENV)
    env.update(extra or {})
    return env


class CommandRunner:
    def __init__(self, root: Path, default_timeout: int, docker_image: str | None, gate: VerificationGate,
                 extra_env: dict[str, str] | None = None):
        self.root = Path(root)
        self.default_timeout = default_timeout
        self.gate = gate
        self.extra_env = dict(extra_env or {})   # e.g. venv PATH, PYTHONWARNINGS (TENV-2)
        self.container: str | None = None
        self._proc: subprocess.Popen | None = None
        self._cancelled = False
        self._lock = threading.Lock()
        if docker_image:
            self._start_container(docker_image)

    # -- docker ---------------------------------------------------------------
    def _start_container(self, image: str) -> None:
        # one name per runner (not only per process) so several workspaces can coexist
        self.container = f"nai-{os.getpid()}-{next(_SEQ)}"
        subprocess.run(
            ["docker", "run", "-d", "--rm", "--init", "--name", self.container,
             "--network", "none", "--memory", "2g", "--cpus", "2", "--user", "1000:1000",
             "-v", f"{self.root}:/work", "-w", "/work", image, "sleep", "infinity"],
            check=True, stdout=subprocess.DEVNULL)
        atexit.register(self.close)
        log("sys", f"commands run inside docker container {self.container} ({image})", "cyan")

    def close(self) -> None:
        """Remove the container (idempotent)."""
        if self.container:
            name, self.container = self.container, None
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)

    # -- cancel (for SCH-6) -----------------------------------------------------
    def cancel(self) -> bool:
        """Kill the running command (process group locally, docker exec container-side)."""
        with self._lock:
            p = self._proc
            if p is None:
                return False
            self._cancelled = True
            if self.container:
                try:
                    subprocess.run(["docker", "exec", self.container, "pkill", "-9", "-f", "bash -o pipefail"],
                                   capture_output=True, timeout=5)
                    p.kill()
                    return True
                except Exception:
                    return False
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            except Exception:
                return False
            return True

    # -- run ------------------------------------------------------------------
    def run_command(self, command: str, timeout_seconds: int | None = None, scope: str | None = None) -> str:
        timeout = min(int(timeout_seconds or self.default_timeout), 3600)
        env_add = {**EXTRA_ENV, **self.extra_env}
        self._cancelled = False
        if self.container:
            argv = ["docker", "exec", "-w", "/work"]
            for k, v in env_add.items():
                argv += ["-e", f"{k}={v}"]
            argv += [self.container, "timeout", "-k", "5", str(timeout), "bash", "-o", "pipefail", "-lc", command]
            p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, errors="replace")
            wait = timeout + 20
        else:
            p = subprocess.Popen(["bash", "-o", "pipefail", "-lc", command], cwd=self.root, env=_safe_env(env_add),
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, errors="replace", start_new_session=True)
            wait = timeout
        with self._lock:
            self._proc = p
        # communicate() collects output; bound the retained result to 1 MiB.
        # The command itself is subject to , so unbounded growth is limited.
        try:
            try:
                out, _ = p.communicate(timeout=wait)
                rc = p.returncode
            except subprocess.TimeoutExpired:
                try:
                    if self.container:
                        p.kill()
                    else:
                        os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                except Exception:
                    pass
                out, _ = p.communicate()
                out = (out or "") + f"\n[timed out after {timeout}s and was killed]"
                rc = 124
        finally:
            with self._lock:
                self._proc = None
        out = (out or "")[:1024 * 1024]
        if self._cancelled:
            out = (out or "") + "\n[cancelled]"
            rc = 130
        note = ""
        keys = self.gate.counts_as_test(command, rc, out or "", scope)
        if keys:
            self.gate.record_pass(keys, scope)
            note = "\n[verification: counted as a passing test run]"
        return f"exit code: {rc}\n{truncate(out or '')}{note}"
