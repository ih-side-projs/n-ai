"""Snapshot determinism, pruning and hostile-archive rejection."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from nai.remote.errors import SnapshotError
from nai.remote.snapshots import extract_snapshot, prune_snapshots, snapshot_tree

IGNORE = (".nai", ".git", "__pycache__", "*.pyc", ".venv")


def _tree(root: Path) -> None:
    (root / "a.py").write_text("print(1)\n", encoding="utf-8")
    (root / "pkg").mkdir()
    (root / "pkg" / "b.py").write_text("x = 2\n", encoding="utf-8")
    (root / "run.sh").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    (root / "run.sh").chmod(0o755)
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "a.pyc").write_text("junk", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (root / ".nai").mkdir()
    (root / ".nai" / "state.json").write_text("{}", encoding="utf-8")


def _add(tar: tarfile.TarFile, name: str, payload: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    tar.addfile(info, io.BytesIO(payload))


def _archive(path: Path, build) -> Path:
    with tarfile.open(path, "w:gz") as tar:
        build(tar)
    return path


def test_snapshot_is_deterministic_and_ignores_ephemera(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    _tree(root)
    first = snapshot_tree(root, IGNORE, dest_dir=tmp_path / "snaps")
    second = snapshot_tree(root, IGNORE, dest_dir=tmp_path / "snaps")
    assert first.sha == second.sha
    assert first.files == 3
    with tarfile.open(first.path, "r:gz") as tar:
        members = tar.getmembers()
        assert sorted(member.name for member in members) == ["a.py", "pkg/b.py", "run.sh"]
        modes = {member.name: member.mode for member in members}
        assert modes["run.sh"] == 0o755 and modes["a.py"] == 0o644
        assert all(member.mtime == 0 and member.uid == 0 for member in members)


def test_snapshot_changes_with_content(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    _tree(root)
    first = snapshot_tree(root, IGNORE, dest_dir=tmp_path / "snaps")
    (root / "a.py").write_text("print(2)\n", encoding="utf-8")
    second = snapshot_tree(root, IGNORE, dest_dir=tmp_path / "snaps")
    assert first.sha != second.sha


def test_symlinks_are_not_snapshotted(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "real.py").write_text("x = 1\n", encoding="utf-8")
    (root / "link.py").symlink_to("/etc/passwd")
    snap = snapshot_tree(root, IGNORE, dest_dir=tmp_path / "snaps")
    with tarfile.open(snap.path, "r:gz") as tar:
        assert [member.name for member in tar.getmembers()] == ["real.py"]


def test_prune_keeps_the_newest(tmp_path: Path) -> None:
    snaps = tmp_path / "snaps"
    snaps.mkdir()
    for index in range(4):
        path = snaps / f"{index:064x}.tar.gz"
        path.write_bytes(b"x")
    removed = prune_snapshots(snaps, keep=2)
    assert len(removed) == 2
    assert len(list(snaps.glob("*.tar.gz"))) == 2


def test_round_trip_extracts_the_tree(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    _tree(root)
    snap = snapshot_tree(root, IGNORE, dest_dir=tmp_path / "snaps")
    dest = tmp_path / "out"
    written = extract_snapshot(snap.path, dest)
    assert sorted(written) == ["a.py", "pkg/b.py", "run.sh"]
    assert (dest / "pkg" / "b.py").read_text(encoding="utf-8") == "x = 2\n"
    assert (dest / "run.sh").stat().st_mode & 0o100


def test_absolute_member_is_rejected(tmp_path: Path) -> None:
    archive = _archive(tmp_path / "abs.tar.gz", lambda tar: _add(tar, "/etc/evil", b"x"))
    with pytest.raises(SnapshotError, match="absolute"):
        extract_snapshot(archive, tmp_path / "out")


def test_parent_escape_is_rejected(tmp_path: Path) -> None:
    archive = _archive(tmp_path / "dots.tar.gz", lambda tar: _add(tar, "../escape.txt", b"x"))
    with pytest.raises(SnapshotError, match="escapes"):
        extract_snapshot(archive, tmp_path / "out")


def test_escaping_symlink_is_rejected(tmp_path: Path) -> None:
    def build(tar: tarfile.TarFile) -> None:
        info = tarfile.TarInfo("link")
        info.type = tarfile.SYMTYPE
        info.linkname = "../../etc/passwd"
        tar.addfile(info)

    archive = _archive(tmp_path / "link.tar.gz", build)
    with pytest.raises(SnapshotError, match="link"):
        extract_snapshot(archive, tmp_path / "out")


def test_device_member_is_rejected(tmp_path: Path) -> None:
    def build(tar: tarfile.TarFile) -> None:
        info = tarfile.TarInfo("dev")
        info.type = tarfile.CHRTYPE
        tar.addfile(info)

    archive = _archive(tmp_path / "dev.tar.gz", build)
    with pytest.raises(SnapshotError, match="device"):
        extract_snapshot(archive, tmp_path / "out")


def test_member_and_byte_limits_are_enforced(tmp_path: Path) -> None:
    def build(tar: tarfile.TarFile) -> None:
        for index in range(5):
            _add(tar, f"f{index}.txt", b"0123456789")

    archive = _archive(tmp_path / "many.tar.gz", build)
    with pytest.raises(SnapshotError, match="members"):
        extract_snapshot(archive, tmp_path / "a", max_members=2)
    with pytest.raises(SnapshotError, match="bytes"):
        extract_snapshot(archive, tmp_path / "b", max_total_bytes=10)


def test_truncated_archive_is_rejected(tmp_path: Path) -> None:
    broken = tmp_path / "broken.tar.gz"
    broken.write_bytes(b"not a gzip stream")
    with pytest.raises(SnapshotError, match="cannot open"):
        extract_snapshot(broken, tmp_path / "out")
