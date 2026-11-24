"""Project manifest: ``relpath -> {sha256, node_id, stage}``.

The manifest is written atomically.  It is the source of truth for detecting
hand edits: a file whose on-disk hash differs from the recorded hash is never
silently overwritten by a job.  ``force_accept`` is the explicit escape hatch.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import layout, paths

IGNORE_DIRS = {
    ".git", ".nai", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache",
    ".venv", "venv", "node_modules", ".agent",
}


@dataclass(frozen=True)
class Mismatch:
    relpath: str
    node_id: str | None
    stage: str | None
    expected: str | None
    actual: str | None
    reason: str


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: str | Path) -> str | None:
    try:
        return sha256_bytes(Path(path).read_bytes())
    except OSError:
        return None


def read(project_root: str | Path) -> dict[str, dict[str, Any]]:
    path = layout.manifest_path(project_root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    files = data.get("files", data)
    return {str(k): v for k, v in files.items() if isinstance(v, dict)} if isinstance(files, dict) else {}


def write(project_root: str | Path, files: dict[str, dict[str, Any]]) -> Path:
    """Atomically replace the manifest; entries are sorted for determinism."""
    clean: dict[str, dict[str, Any]] = {}
    for relpath in sorted(files):
        try:
            layout.relative_parts(relpath)
        except ValueError:
            continue
        entry = files[relpath]
        if not isinstance(entry, dict):
            continue
        clean[relpath] = {
            "sha256": str(entry.get("sha256") or ""),
            "node_id": entry.get("node_id"),
            "stage": entry.get("stage"),
        }
    payload = json.dumps({"version": 1, "files": clean}, indent=2, sort_keys=True) + "\n"
    return paths.write_atomic(layout.manifest_path(project_root), payload, root=project_root)


def record(
    project_root: str | Path,
    relpath: str,
    *,
    node_id: str | None = None,
    stage: str | None = None,
    content: str | bytes | None = None,
) -> dict[str, Any]:
    """Record the current hash of *relpath* (or of *content* when supplied)."""
    parts = layout.relative_parts(relpath)
    rel = "/".join(parts)
    if content is None:
        digest = sha256_file(paths.resolve_relative(project_root, rel))
        if digest is None:
            raise FileNotFoundError(rel)
    else:
        digest = sha256_text(content) if isinstance(content, str) else sha256_bytes(content)
    files = read(project_root)
    files[rel] = {"sha256": digest, "node_id": node_id, "stage": stage}
    write(project_root, files)
    return files[rel]


def remove(project_root: str | Path, relpath: str) -> None:
    files = read(project_root)
    if files.pop(str(relpath), None) is not None:
        write(project_root, files)


def verify(project_root: str | Path) -> list[Mismatch]:
    """Compare every manifest entry with the file currently on disk."""
    root = Path(project_root).expanduser().resolve()
    mismatches: list[Mismatch] = []
    for relpath, entry in sorted(read(root).items()):
        expected = str(entry.get("sha256") or "")
        node_id = entry.get("node_id")
        stage = entry.get("stage")
        try:
            path = paths.resolve_relative(root, relpath)
        except (ValueError, OSError) as exc:
            mismatches.append(Mismatch(relpath, node_id, stage, expected, None, f"unsafe path: {exc}"))
            continue
        if not path.is_file():
            mismatches.append(Mismatch(relpath, node_id, stage, expected, None, "missing"))
            continue
        actual = sha256_file(path)
        if actual != expected:
            mismatches.append(Mismatch(relpath, node_id, stage, expected, actual, "modified"))
    return mismatches


def force_accept(project_root: str | Path, relpath: str) -> dict[str, Any] | None:
    """Accept the current on-disk bytes for one entry (explicit force action)."""
    files = read(project_root)
    entry = files.get(str(relpath))
    if entry is None:
        return None
    path = paths.resolve_relative(project_root, relpath)
    if not path.is_file():
        files.pop(str(relpath), None)
        write(project_root, files)
        return None
    entry = {**entry, "sha256": sha256_file(path)}
    files[str(relpath)] = entry
    write(project_root, files)
    return entry


def detect_hand_edits(store: Any, project: dict[str, Any], *, force: bool = False) -> list[Mismatch]:
    """Emit events and stale nodes for hand edits; optionally accept them."""
    root = project.get("workspace_path")
    if not root:
        return []
    mismatches = verify(root)
    if not mismatches:
        return []
    if force:
        for item in mismatches:
            force_accept(root, item.relpath)
        return []
    for item in mismatches:
        try:
            store.record_event(
                project["id"],
                "project.hand_edit",
                f"Hand edit detected in {item.relpath} ({item.reason})",
                node_id=item.node_id,
                level="warning",
                data={
                    "path": item.relpath,
                    "reason": item.reason,
                    "expected_sha256": item.expected,
                    "actual_sha256": item.actual,
                    "stage": item.stage,
                },
            )
        except Exception:
            pass
        if item.node_id:
            try:
                node = store.get_node(item.node_id)
                if node is not None and node.get("status") != "stale":
                    store.transition_node_status(item.node_id, "stale", note=f"Hand edit: {item.relpath}")
            except Exception:
                pass
    return mismatches


def scan(project_root: str | Path, *, node_id: str | None = None, stage: str = "imported") -> dict[str, dict[str, Any]]:
    """Build manifest entries for existing files (used by migration/relocation)."""
    root = Path(project_root).expanduser().resolve()
    files: dict[str, dict[str, Any]] = {}
    for current, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in IGNORE_DIRS)
        for name in sorted(names):
            path = Path(current) / name
            if name in {layout.MANIFEST_NAME, layout.MARKER_NAME}:
                continue
            try:
                rel = path.relative_to(root).as_posix()
                files[rel] = {"sha256": sha256_file(path), "node_id": node_id, "stage": stage}
            except (OSError, ValueError):
                continue
    return files
