"""One interface for every command N-AI runs.

Commands used to be spawned from several places (the target-environment setup,
the verify/integrate handlers, the model-facing ``run_command`` tool and the
evaluation harness), each with its own environment handling, timeout and
cleanup.  This module replaces those call sites with a single seam::

    target = make_target(root, docker_image=...)
    result = target.run(argv, cwd, env_overrides, timeout, cancel_event, output_cap)

Hardening rules every target honours:

* the child environment is scrubbed to an allowlist (``PATH``, ``HOME``,
  ``LANG`` plus whatever the adapter asks for); secrets such as
  ``LLAMA_SWAP_KEY`` are never forwarded;
* each adapter has two phases: ``setup`` (network allowed) and ``run``
  (network off);
* resource limits are applied (memory, CPUs, pids, capabilities);
* output is streamed with a cap and head+tail retention;
* cancellation is real: process groups locally, named containers in Docker.

Nothing here knows about a language.  Adapters supply argv through
``LanguageAdapter``; the target only runs it safely.
"""

from __future__ import annotations

import os
import shlex
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

PHASE_SETUP = "setup"
PHASE_RUN = "run"

#: Environment variables a child may inherit.  Everything else is dropped, so a
#: secret in the parent environment cannot reach generated code or a
#: model-supplied command.
ENV_ALLOWLIST: tuple[str, ...] = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "TMPDIR",
    "TZ",
    "USER",
    "LOGNAME",
    "SHELL",
)

#: Prefixes that are never forwarded, even when a caller asks for them.
SECRET_PREFIXES: tuple[str, ...] = (
    "LLAMA_SWAP_KEY",
    "LLAMA_SWAP_URL",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "HF_TOKEN",
    "HUGGINGFACE",
    "AWS_",
    "AZURE_",
    "GOOGLE_",
    "DOCKER_AUTH",
    "KUBECONFIG",
    "SSH_AUTH_SOCK",
    "PGPASSWORD",
    "DATABASE_URL",
    "PRIVATE_KEY",
    "SECRET",
    "TOKEN",
    "PASSWORD",
    "CREDENTIAL",
    "NAI_",
)

#: Values the pipeline has always exported to generated code.  They are not
#: secrets; they make commands deterministic and non-interactive.
DEFAULT_ENV: dict[str, str] = {
    "CI": "1",
    "PYTHONUNBUFFERED": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
    "DEBIAN_FRONTEND": "noninteractive",
    "GIT_TERMINAL_PROMPT": "0",
    "PIP_NO_INPUT": "1",
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
}

#: Exit code used for a command killed by the timeout.
TIMEOUT_EXIT = 124
#: Exit code used for a command killed by cancellation.
CANCEL_EXIT = 130


class ExecError(RuntimeError):
    """A target could not be prepared or driven (missing docker, bad limits)."""


def is_secret(name: str) -> bool:
    """True when *name* looks like a credential and must never be forwarded."""
    upper = str(name).upper()
    return any(upper.startswith(prefix) for prefix in SECRET_PREFIXES)


def scrub_env(
    env_overrides: Mapping[str, object] | None = None,
    *,
    allowlist: Iterable[str] = ENV_ALLOWLIST,
    extra: Mapping[str, object] | None = None,
) -> dict[str, str]:
    """Return the environment a child is allowed to see.

    *allowlist* names are copied from ``os.environ`` when present.  *extra* and
    *env_overrides* add adapter needs (``PYTHONPATH``, a venv ``PATH``) and are
    filtered through :func:`is_secret` so a caller cannot smuggle a credential
    in.  ``DEFAULT_ENV`` fills in the pipeline defaults without overwriting an
    explicit value.
    """
    env: dict[str, str] = {}
    for name in allowlist:
        if is_secret(name):
            continue
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    for name, value in DEFAULT_ENV.items():
        env.setdefault(name, value)
    for source in (extra or {}, env_overrides or {}):
        for name, value in source.items():
            if value is None or is_secret(name):
                continue
            env[str(name)] = str(value)
    return env


@dataclass(frozen=True)
class Capabilities:
    """What a target can actually enforce; ``describe()`` renders it."""

    mode: str
    network_off: bool = False
    filesystem_isolated: bool = False
    memory_limit: bool = False
    cpu_limit: bool = False
    pids_limit: bool = False
    capability_drop: bool = False
    no_new_privileges: bool = False
    user_namespace: bool = False
    real_cancel: bool = False
    streaming: bool = True

    def to_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "network_off": self.network_off,
            "filesystem_isolated": self.filesystem_isolated,
            "memory_limit": self.memory_limit,
            "cpu_limit": self.cpu_limit,
            "pids_limit": self.pids_limit,
            "capability_drop": self.capability_drop,
            "no_new_privileges": self.no_new_privileges,
            "user_namespace": self.user_namespace,
            "real_cancel": self.real_cancel,
            "streaming": self.streaming,
        }


@dataclass(frozen=True)
class RunResult:
    """The outcome of one command."""

    argv: tuple[str, ...]
    exit_code: int
    output: str
    duration: float = 0.0
    timed_out: bool = False
    cancelled: bool = False
    truncated: bool = False
    mode: str = "local"
    network: str = "off"
    weaker_isolation: bool = False
    error: str = ""
    notes: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.cancelled

    def __str__(self) -> str:
        return f"exit code: {self.exit_code}\n{self.output}"

    def to_dict(self) -> dict[str, object]:
        return {
            "argv": list(self.argv),
            "exit_code": self.exit_code,
            "output": self.output,
            "duration": self.duration,
            "timed_out": self.timed_out,
            "cancelled": self.cancelled,
            "truncated": self.truncated,
            "mode": self.mode,
            "network": self.network,
            "weaker_isolation": self.weaker_isolation,
            "error": self.error,
            "notes": list(self.notes),
        }


class OutputCapture:
    """Streams a child's output while retaining only a head+tail window.

    ``feed`` is called from the reader thread as text arrives; ``text`` is the
    final string.  ``sink`` receives every chunk so callers can show live
    output without buffering the whole thing.
    """

    def __init__(self, cap: int = 1 << 20, sink: Callable[[str], None] | None = None):
        self.cap = max(64, int(cap or 0))
        self.head_budget = self.cap // 2
        self.tail_budget = self.cap - self.head_budget
        self._head: list[str] = []
        self._head_len = 0
        self._tail: deque[str] = deque()
        self._tail_len = 0
        self._dropped = 0
        self._sink = sink

    def feed(self, chunk: str) -> None:
        if not chunk:
            return
        if self._sink is not None:
            try:
                self._sink(chunk)
            except Exception:  # pragma: no cover - a sink must never break a run
                pass
        if self._head_len < self.head_budget:
            room = self.head_budget - self._head_len
            piece, chunk = chunk[:room], chunk[room:]
            self._head.append(piece)
            self._head_len += len(piece)
        if not chunk:
            return
        self._tail.append(chunk)
        self._tail_len += len(chunk)
        while self._tail_len > self.tail_budget and len(self._tail) > 1:
            dropped = self._tail.popleft()
            self._dropped += len(dropped)
            self._tail_len -= len(dropped)
        if self._tail_len > self.tail_budget:
            only = self._tail.pop()
            keep = only[-self.tail_budget:]
            self._dropped += len(only) - len(keep)
            self._tail.append(keep)
            self._tail_len = len(keep)

    @property
    def truncated(self) -> bool:
        return self._dropped > 0

    def text(self) -> str:
        head = "".join(self._head)
        tail = "".join(self._tail)
        if not self._dropped:
            return head + tail
        return f"{head}\n... [{self._dropped} chars truncated] ...\n{tail}"


@runtime_checkable
class ExecutionTarget(Protocol):
    """The only way N-AI runs a command."""

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
        """Run *argv* and return its result.  Never raises for a failing command."""

    def capabilities(self) -> Capabilities:
        """What this target can enforce."""

    def describe(self) -> str:
        """One line for logs and ``nai doctor``."""

    def close(self) -> None:
        """Release containers or other resources (idempotent)."""


def shell_argv(command: str) -> list[str]:
    """argv for a step an adapter explicitly marks as a shell step.

    Only callers that own a user- or adapter-supplied shell string use this;
    pipeline commands are passed as argv.  ``-lc`` is deliberately avoided: a
    login shell would re-read profiles and change the environment we just
    scrubbed.
    """
    return ["bash", "-o", "pipefail", "-c", command]


def as_argv(command: str | Iterable[str]) -> list[str]:
    """Normalise a command to argv without ever invoking a shell."""
    if isinstance(command, str):
        return shlex.split(command)
    return [str(part) for part in command]


#: Characters that only mean something to a shell when they are *unquoted*.
_SHELL_CHARS = frozenset("|&;<>()$`*?[]{}~\n")


def needs_shell(command: str) -> bool:
    """True when a string command uses unquoted shell syntax.

    Quoted arguments are ignored: ``python -c 'print(1)'`` is a plain command
    even though it contains parentheses.  An unquoted operator, glob or
    expansion means the string cannot be split into argv safely.
    """
    quote: str | None = None
    escaped = False
    for ch in command:
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if quote is not None:
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            continue
        if ch in _SHELL_CHARS:
            return True
    return False


@dataclass
class BaseTarget:
    """Shared plumbing: env scrubbing, phase/network resolution, notes."""

    root: object
    default_timeout: float = 600.0
    default_cap: int = 1 << 20
    notes: list[str] = field(default_factory=list)
    _warned_network: bool = field(default=False, init=False, repr=False)

    def _note(self, message: str) -> None:
        if message not in self.notes:
            self.notes.append(message)

    def _network_for(self, phase: str, network: bool | None) -> bool:
        """Resolve whether the command may use the network."""
        if network is not None:
            return bool(network)
        return phase == PHASE_SETUP

    def _clamp_timeout(self, timeout: float | None) -> float:
        value = float(timeout if timeout is not None else self.default_timeout)
        return max(1.0, min(value, 3600.0))

    def _env(self, env_overrides: Mapping[str, object] | None) -> dict[str, str]:
        return scrub_env(env_overrides)
