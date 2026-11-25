"""Toolchain probes for language adapters.

An adapter declares the binaries it needs (``LanguageAdapter.toolchain``).  The
probe resolves them on a machine or worker root and reports versions, so
``nai languages`` and the support test can tell "the language is registered"
apart from "this machine can actually build it".

Nothing here mutates the system: it only runs ``<tool> --version`` for tools
that already exist on disk.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import registry
from .base import ToolCheck

_VERSION_LIMIT = 120


@dataclass(frozen=True)
class ToolStatus:
    """Probe result for one declared tool."""

    name: str
    required: bool
    found: bool
    path: str = ""
    version: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "required": self.required,
            "found": self.found,
            "path": self.path,
            "version": self.version,
        }


@dataclass(frozen=True)
class ProbeResult:
    """Probe result for one language on one worker root."""

    language: str
    root: str
    tools: tuple[ToolStatus, ...]

    @property
    def ok(self) -> bool:
        """True when every *required* tool is present."""
        return all(tool.found for tool in self.tools if tool.required)

    @property
    def missing(self) -> tuple[str, ...]:
        """Names of required tools that were not found."""
        return tuple(tool.name for tool in self.tools if tool.required and not tool.found)

    def to_dict(self) -> dict[str, object]:
        return {
            "language": self.language,
            "root": self.root,
            "ok": self.ok,
            "missing": list(self.missing),
            "tools": [tool.to_dict() for tool in self.tools],
        }


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        cleaned = line.strip()
        if cleaned:
            return cleaned[:_VERSION_LIMIT]
    return ""


def _default_run(argv: list[str], cwd: Path) -> tuple[int, str]:
    """Run one ``--version`` probe through the shared execution target."""
    from ..exec import PHASE_RUN, LocalTarget

    try:
        result = LocalTarget(cwd, default_timeout=20).run(argv, cwd=str(cwd), timeout=20, phase=PHASE_RUN)
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - defensive
        return 1, str(exc)
    return result.exit_code, result.output


def _worker_path(root: Path | None, base_path: str) -> str:
    """Prepend a worker's own bin directories so its venv tools win."""
    if root is None:
        return base_path
    parts = [str(root / ".venv" / "bin"), str(root / "bin"), base_path]
    return os.pathsep.join(part for part in parts if part)


def probe(
    language_id: str,
    *,
    root: str | Path | None = None,
    which: Callable[[str], str | None] | None = None,
    run: Callable[[list[str], Path], tuple[int, str]] | None = None,
) -> ProbeResult:
    """Probe *language_id*'s declared toolchain on *root* (default: this machine)."""
    adapter = registry.get(language_id)
    worker_root = Path(root).expanduser().resolve() if root else None
    root_path = worker_root or Path.cwd()
    if which is None:
        search_path = _worker_path(worker_root, os.environ.get("PATH", ""))

        def resolve(name: str) -> str | None:
            return shutil.which(name, path=search_path)
    else:
        resolve = which
    execute = run or _default_run

    statuses: list[ToolStatus] = []
    for check in _toolchain_of(adapter):
        candidates = (check.argv[0], *check.fallback) if check.argv else check.fallback
        path = next((found for name in candidates if (found := resolve(name))), None)
        if not path:
            statuses.append(ToolStatus(name=check.name, required=check.required, found=False))
            continue
        rc, output = execute([path, *check.argv[1:]], root_path)
        statuses.append(
            ToolStatus(
                name=check.name,
                required=check.required,
                found=True,
                path=path,
                version=_first_line(output) if rc == 0 else "",
            )
        )
    return ProbeResult(language=adapter.id, root=str(root_path), tools=tuple(statuses))


def _toolchain_of(adapter: object) -> tuple[ToolCheck, ...]:
    declared = getattr(adapter, "toolchain", ()) or ()
    return tuple(tool for tool in declared if isinstance(tool, ToolCheck))
