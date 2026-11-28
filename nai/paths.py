"""Filesystem guards for project layout paths (validation and atomic writes).

``nai.layout`` is deliberately pure.  This module contains the small amount of
I/O needed to validate output roots, markers, symlink containment and atomic
file replacement.  Every path accepted below a project root is relative-only
and is resolved before containment is checked.
"""
from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import layout


class LayoutError(ValueError):
    """Raised when a project/output path violates the layout policy."""


SYSTEM_DIRS = (
    "/etc", "/usr", "/bin", "/sbin", "/lib", "/lib64", "/boot",
    "/dev", "/proc", "/sys", "/var", "/opt", "/root",
)


def dangerous_root_reason(path: str | Path) -> str | None:
    """Return a human-readable reason when *path* is a forbidden root."""
    resolved = Path(path).expanduser().resolve()
    if resolved == Path("/"):
        return "refusing to use the filesystem root"
    if resolved == Path.home().resolve():
        return "refusing to use the home directory itself"
    for system in SYSTEM_DIRS:
        root = Path(system).resolve()
        if resolved == root or root in resolved.parents:
            return f"refusing to use system directory {system}"
    return None


def validate_output_root(value: str | Path, *, create: bool = True) -> Path:
    """Expand, resolve and validate an output root (may be absolute)."""
    root = Path(value).expanduser().resolve()
    reason = dangerous_root_reason(root)
    if reason:
        raise LayoutError(reason)
    if root.exists() and not root.is_dir():
        raise LayoutError(f"output root is not a directory: {root}")
    if create:
        root.mkdir(parents=True, exist_ok=True)
    return root


def default_output_root(db_path: str | Path) -> Path:
    """Default output root next to (not inside) the ``.nai`` state directory."""
    db = Path(db_path).expanduser().resolve()
    parent = db.parent
    if parent.name == ".nai":
        return parent.parent / "outputs"
    return parent / "outputs"


def resolve_output_root(
    explicit: str | Path | None = None,
    *,
    settings: dict[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
    db_path: str | Path | None = None,
) -> Path:
    """Resolve precedence: explicit > NAI_OUTPUT_ROOT > settings > default."""
    source: Mapping[str, str] = os.environ if env is None else env
    values = settings or {}
    value = explicit or source.get("NAI_OUTPUT_ROOT") or values.get("output_root")
    if value:
        return validate_output_root(value)
    if db_path is None:
        raise LayoutError("cannot derive a default output root without a database path")
    return validate_output_root(default_output_root(db_path))


def read_marker(project_root: str | Path) -> dict[str, Any] | None:
    path = layout.marker_path(project_root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_marker(project_root: str | Path, *, project_id: str, slug: str, name: str = "") -> Path:
    path = layout.marker_path(project_root)
    payload = {
        "format": "nai-project",
        "version": 1,
        "project_id": project_id,
        "slug": slug,
        "name": name,
    }
    write_atomic(path, json.dumps(payload, indent=2, sort_keys=True) + "\n", root=project_root)
    return path


def prepare_project_dir(
    output_root: str | Path,
    slug: str,
    *,
    project_id: str,
    name: str = "",
    allow_existing_marker: bool = True,
) -> Path:
    """Create (or reuse) ``<output_root>/<slug>`` and write its marker.

    A non-empty directory is only accepted when it carries a matching
    ``.nai-project`` marker; an empty directory is always accepted.
    """
    root = layout.project_root(validate_output_root(output_root), slug)
    reason = dangerous_root_reason(root)
    if reason:
        raise LayoutError(reason)
    if root.is_symlink():
        raise LayoutError(f"project path must not be a symlink: {root}")
    if root.exists():
        if not root.is_dir():
            raise LayoutError(f"project path is not a directory: {root}")
        entries = list(root.iterdir())
        if entries:
            marker = read_marker(root)
            if not allow_existing_marker or marker is None:
                raise LayoutError(f"project directory is not empty and has no marker: {root}")
            if marker.get("slug") != slug or marker.get("project_id") != project_id:
                raise LayoutError(f"project marker does not match {slug!r}: {root}")
    root.mkdir(parents=True, exist_ok=True)
    if read_marker(root) is None:
        write_marker(root, project_id=project_id, slug=slug, name=name)
    return root


def resolve_within(project_root: str | Path, candidate: str | Path) -> Path:
    """Resolve *candidate* and require it to stay below *project_root*."""
    root = Path(project_root).expanduser().resolve()
    resolved = Path(candidate).expanduser().resolve()
    if resolved != root and root not in resolved.parents:
        raise LayoutError(f"path escapes project root: {candidate}")
    return resolved


def resolve_relative(project_root: str | Path, relative: str) -> Path:
    """Resolve a relative-only path below the project root, following symlinks."""
    return resolve_within(project_root, layout.safe_join(project_root, relative))


def write_atomic(path: str | Path, data: str | bytes, *, root: str | Path | None = None) -> Path:
    """Write *data* to *path* atomically (same-directory temp + replace).

    When *root* is supplied, both the target and its parent are resolved and
    required to stay below the project root, so a symlinked directory cannot
    redirect a write outside the project.
    """
    target = Path(path)
    if root is not None:
        resolve_within(root, target)
        resolve_within(root, target.parent)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
    return target
