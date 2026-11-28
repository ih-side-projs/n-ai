"""Deterministic project snapshots and safe extraction.

A snapshot is a ``tar.gz`` of the project tree minus the adapter's
``snapshot_ignore`` patterns.  Identical trees produce byte-identical archives
(fixed ordering, timestamps, ownership and modes), so the sha256 of the archive
is a content address the orchestrator can store and the worker can verify.
"""

from __future__ import annotations

import fnmatch
import gzip
import hashlib
import io
import os
import re
import shutil
import tarfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .errors import SnapshotError

MAX_MEMBERS = 20_000
MAX_TOTAL_BYTES = 512 * 1024 * 1024
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
#: Public alias for callers that validate a snapshot address.
SHA_RE = _SHA_RE


@dataclass(frozen=True)
class Snapshot:
    """A content-addressed project archive."""

    sha: str
    path: Path
    files: int

    def to_dict(self) -> dict[str, object]:
        return {"sha": self.sha, "path": str(self.path), "files": self.files}


def _ignored(rel: str, patterns: tuple[str, ...]) -> bool:
    """True when any path component or the whole path matches an ignore pattern."""
    parts = rel.split("/")
    for pattern in patterns:
        if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(parts[-1], pattern):
            return True
        if any(fnmatch.fnmatch(part, pattern) for part in parts[:-1]):
            return True
    return False


def _iter_files(root: Path, patterns: tuple[str, ...]) -> list[tuple[str, Path]]:
    """Sorted (relative POSIX path, absolute path) pairs, symlinks skipped."""
    found: list[tuple[str, Path]] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(dirpath)
        rel_dir = base.relative_to(root).as_posix()
        keep: list[str] = []
        for name in sorted(dirnames):
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            if not _ignored(rel, patterns):
                keep.append(name)
        dirnames[:] = keep
        for name in sorted(filenames):
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            if _ignored(rel, patterns):
                continue
            path = base / name
            if path.is_symlink():
                continue  # never ship a link: it could point outside the tree
            if not path.is_file():
                continue
            found.append((rel, path))
    found.sort(key=lambda item: item[0])
    return found


def snapshot_tree(
    root: str | Path,
    ignore: Iterable[str] = (),
    *,
    dest_dir: str | Path | None = None,
    keep: int = 5,
) -> Snapshot:
    """Archive *root* deterministically and return its content address.

    *dest_dir* defaults to ``<root>/.nai/snapshots`` (which is itself ignored,
    so snapshots never nest).  Older archives beyond *keep* are pruned.
    """
    root = Path(root).resolve()
    if not root.is_dir():
        raise SnapshotError(f"project root is not a directory: {root}")
    patterns = tuple(str(p) for p in ignore if str(p).strip())
    files = _iter_files(root, patterns)
    out_dir = Path(dest_dir).resolve() if dest_dir else (root / ".nai" / "snapshots")
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / f".{os.getpid()}.{os.urandom(6).hex()}.tmp"
    try:
        with tmp.open("wb") as raw, gzip.GzipFile(
            filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=6
        ) as gz, tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:
            for rel, path in files:
                data = path.read_bytes()
                info = tarfile.TarInfo(name=rel)
                info.size = len(data)
                info.mtime = 0
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.mode = 0o755 if os.access(path, os.X_OK) else 0o644
                tar.addfile(info, io.BytesIO(data))
        digest = hashlib.sha256()
        with tmp.open("rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
        sha = digest.hexdigest()
        target = out_dir / f"{sha}.tar.gz"
        os.replace(tmp, target)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    prune_snapshots(out_dir, keep=keep, protect=sha)
    return Snapshot(sha=sha, path=target, files=len(files))


def prune_snapshots(dest_dir: str | Path, *, keep: int = 5, protect: str | None = None) -> list[Path]:
    """Keep the newest *keep* archives (never the one just written)."""
    out_dir = Path(dest_dir)
    archives = sorted(
        (p for p in out_dir.glob("*.tar.gz") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    removed: list[Path] = []
    for path in archives[max(1, int(keep)):]:
        if protect and path.name == f"{protect}.tar.gz":
            continue
        path.unlink(missing_ok=True)
        removed.append(path)
    return removed


def safe_member_name(name: str) -> PurePosixPath:
    """Validate an archive member name; raise :class:`SnapshotError` when unsafe."""
    if not isinstance(name, str) or not name or "\x00" in name:
        raise SnapshotError("snapshot member has an empty or invalid name")
    text = name.replace("\\", "/")
    if text.startswith("/") or (len(text) > 1 and text[1] == ":"):
        raise SnapshotError(f"snapshot member is absolute: {name}")
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise SnapshotError(f"snapshot member escapes the root: {name}")
    if not parts:
        raise SnapshotError(f"snapshot member has no usable path: {name}")
    return PurePosixPath(*parts)


def extract_snapshot(
    archive: str | Path,
    dest: str | Path,
    *,
    max_members: int = MAX_MEMBERS,
    max_total_bytes: int = MAX_TOTAL_BYTES,
) -> list[str]:
    """Extract *archive* into *dest*, rejecting anything that could escape it.

    Absolute paths, ``..`` segments, links (symbolic or hard), devices and
    FIFOs are refused outright; the worker's snapshot never contains them, so a
    hostile archive is a protocol violation rather than a best-effort case.
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    try:
        tar = tarfile.open(archive, "r:gz")
    except (tarfile.TarError, OSError) as exc:
        raise SnapshotError(f"cannot open snapshot: {exc}") from None
    written: list[str] = []
    total = 0
    with tar:
        members = tar.getmembers()
        if len(members) > int(max_members):
            raise SnapshotError(f"snapshot has {len(members)} members (limit {max_members})")
        for member in members:
            rel = safe_member_name(member.name)
            if member.issym() or member.islnk():
                raise SnapshotError(f"snapshot member is a link: {member.name}")
            if member.isdev() or member.isfifo():
                raise SnapshotError(f"snapshot member is a device: {member.name}")
            target = root / rel
            resolved = target.resolve()
            if resolved != root and root not in resolved.parents:
                raise SnapshotError(f"snapshot member escapes the root: {member.name}")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise SnapshotError(f"unsupported snapshot member: {member.name}")
            if member.size < 0:
                raise SnapshotError(f"snapshot member has a negative size: {member.name}")
            total += member.size
            if total > int(max_total_bytes):
                raise SnapshotError(f"snapshot exceeds {max_total_bytes} bytes")
            target.parent.mkdir(parents=True, exist_ok=True)
            source = tar.extractfile(member)
            if source is None:  # pragma: no cover - isfile() implies a payload
                raise SnapshotError(f"snapshot member has no content: {member.name}")
            with source, target.open("wb") as out:
                shutil.copyfileobj(source, out)
            target.chmod(0o755 if member.mode & 0o100 else 0o644)
            written.append(rel.as_posix())
    return written

