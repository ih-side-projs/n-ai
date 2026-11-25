"""Command execution targets.

Every command N-AI runs goes through one interface, so the environment is
scrubbed, resource limits are applied, output is streamed with a cap, and
cancellation is real.  See :mod:`nai.exec.target` for the contract.

``make_target`` picks the implementation:

* ``docker_image`` set -> :class:`nai.exec.docker.DockerTarget`
* otherwise -> :class:`nai.exec.local.LocalTarget`
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from .docker import DockerTarget, require_docker
from .local import LocalTarget
from .target import (
    CANCEL_EXIT,
    DEFAULT_ENV,
    ENV_ALLOWLIST,
    PHASE_RUN,
    PHASE_SETUP,
    SECRET_PREFIXES,
    TIMEOUT_EXIT,
    BaseTarget,
    Capabilities,
    ExecError,
    ExecutionTarget,
    OutputCapture,
    RunResult,
    as_argv,
    is_secret,
    needs_shell,
    scrub_env,
    shell_argv,
)

__all__ = [
    "CANCEL_EXIT",
    "DEFAULT_ENV",
    "ENV_ALLOWLIST",
    "PHASE_RUN",
    "PHASE_SETUP",
    "SECRET_PREFIXES",
    "TIMEOUT_EXIT",
    "BaseTarget",
    "Capabilities",
    "DockerTarget",
    "ExecError",
    "ExecutionTarget",
    "LocalTarget",
    "OutputCapture",
    "RunResult",
    "as_argv",
    "is_secret",
    "make_target",
    "needs_shell",
    "require_docker",
    "scrub_env",
    "shell_argv",
]


def make_target(
    root: str | Path,
    *,
    docker_image: str | None = None,
    default_timeout: float = 600.0,
    output_cap: int | None = None,
    on_note=None,
    sink=None,
    **options: object,
) -> ExecutionTarget:
    """Build the execution target for *root*.

    *docker_image* selects Docker mode; everything else configures the local
    target (memory, CPUs, network isolation).  ``on_note`` receives
    ``(event_type, message)`` pairs so callers can record weaker isolation in
    the event log; ``sink`` receives streamed output chunks.
    """
    cap = int(output_cap or (1 << 20))
    if docker_image:
        return DockerTarget(
            root,
            docker_image,
            default_timeout=default_timeout,
            default_cap=cap,
            on_note=on_note,
            sink=sink,
            **options,
        )
    return LocalTarget(
        root,
        default_timeout=default_timeout,
        default_cap=cap,
        on_note=on_note,
        sink=sink,
        **options,
    )


def capabilities_for(target: ExecutionTarget) -> Mapping[str, object]:
    """Convenience: ``target.capabilities().to_dict()``."""
    return target.capabilities().to_dict()
