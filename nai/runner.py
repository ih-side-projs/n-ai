"""command-runner: the workspace's handle on the execution target.

Every command N-AI runs goes through :class:`nai.exec.ExecutionTarget`.  This
module is the thin, backward-compatible adapter the workspace and the build
pipeline use: it keeps the historical ``run_command`` string API for the
model-facing ``run_command`` tool, and adds ``run_argv`` for pipeline steps
that know their argv.

Hardening lives in the target, not here: environment scrubbing, the setup/run
network split, resource limits, streaming output and cancellation.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

from .exec import PHASE_RUN, ExecutionTarget, RunResult, make_target, shell_argv
from .gate import VerificationGate
from .textutil import truncate

#: Appended when a command counted as a passing verification run.
VERIFICATION_NOTE = "verification"


class CommandRunner:
    """Run commands through one :class:`ExecutionTarget`."""

    def __init__(
        self,
        root: str | Path,
        default_timeout: float = 600,
        docker_image: str | None = None,
        gate: VerificationGate | None = None,
        extra_env: dict[str, str] | None = None,
        target: ExecutionTarget | None = None,
        on_note: Any = None,
    ):
        self.root = Path(root).resolve()
        self.default_timeout = default_timeout
        self.gate = gate if gate is not None else VerificationGate(self.root)
        # e.g. a target venv's bin dir on PATH; merged into every command's env
        self.extra_env = dict(extra_env or {})
        self.target: ExecutionTarget = target or make_target(
            self.root,
            docker_image=docker_image,
            default_timeout=default_timeout,
            on_note=on_note,
        )

    # -- introspection -------------------------------------------------------
    @property
    def container(self) -> str | None:
        """The Docker container name when the target runs in Docker mode."""
        return getattr(self.target, "container", None)

    def describe(self) -> str:
        return self.target.describe()

    def capabilities(self):
        return self.target.capabilities()

    # -- cancellation --------------------------------------------------------
    def cancel(self) -> bool:
        """Stop the running command.  Returns True when something was running."""
        cancel = getattr(self.target, "cancel", None)
        return bool(cancel()) if callable(cancel) else False

    def close(self) -> None:
        self.target.close()

    # -- running -------------------------------------------------------------
    def run_argv(
        self,
        argv: Any,
        timeout_seconds: float | None = None,
        scope: str | None = None,
        *,
        phase: str = PHASE_RUN,
        env_overrides: dict[str, str] | None = None,
        network: bool | None = None,
        cwd: str | Path | None = None,
        output_cap: int | None = None,
        cancel_event: Any = None,
        command_text: str | None = None,
    ) -> RunResult:
        """Run *argv* through the target and record a passing test if it ran one."""
        merged = {**self.extra_env, **(env_overrides or {})}
        result = self.target.run(
            argv,
            cwd=str(cwd) if cwd is not None else str(self.root),
            env_overrides=merged,
            timeout=timeout_seconds if timeout_seconds is not None else self.default_timeout,
            cancel_event=cancel_event,
            output_cap=output_cap,
            phase=phase,
            network=network,
        )
        # The gate matches *command strings*; keep the caller's text when it has
        # one (a shell step), otherwise render the argv.
        text = command_text or shlex.join([str(a) for a in argv])
        return self._record_gate(result, text, scope)

    def run_command(self, command: str, timeout_seconds: float | None = None, scope: str | None = None) -> str:
        """Run a shell command and return ``exit code: N\\n<output>``.

        This is the model-facing ``run_command`` tool: the string is a shell
        step by definition, so it runs as ``bash -o pipefail -c``.  Pipeline
        steps should call :meth:`run_argv` instead.
        """
        result = self.run_argv(
            shell_argv(command),
            timeout_seconds=timeout_seconds,
            scope=scope,
            phase=PHASE_RUN,
            command_text=command,
        )
        text = f"exit code: {result.exit_code}\n{truncate(result.output)}"
        if VERIFICATION_NOTE in result.notes:
            text += "\n[verification: counted as a passing test run]"
        if result.error:
            text += f"\n{result.error}"
        return text

    def _record_gate(self, result: RunResult, command: str, scope: str | None) -> RunResult:
        if self.gate is None or not command:
            return result
        try:
            keys = self.gate.counts_as_test(command, result.exit_code, result.output, scope)
        except Exception:  # pragma: no cover - a gate bug must not hide a result
            return result
        if not keys:
            return result
        self.gate.record_pass(keys, scope)
        return RunResult(
            argv=result.argv,
            exit_code=result.exit_code,
            output=result.output,
            duration=result.duration,
            timed_out=result.timed_out,
            cancelled=result.cancelled,
            truncated=result.truncated,
            mode=result.mode,
            network=result.network,
            weaker_isolation=result.weaker_isolation,
            error=result.error,
            notes=(*result.notes, VERIFICATION_NOTE),
        )
