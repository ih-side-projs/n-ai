"""Packaging: the migration, console script and launchd agent ship with the project."""

from __future__ import annotations

import plistlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_remote_migration_is_tracked_and_packaged() -> None:
    migration = ROOT / "nai" / "store" / "migrations" / "0008_remote_workers.sql"
    assert migration.is_file()
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'migrations/*.sql' in pyproject
    assert list((ROOT / "nai" / "store" / "migrations").glob("0008_*.sql")) == [migration]


def test_worker_console_script_is_declared() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'nai-worker = "nai.remote.worker:worker_main"' in pyproject


def test_launchd_agent_is_a_valid_plist() -> None:
    path = ROOT / "deploy" / "nai-worker.plist"
    data = plistlib.loads(path.read_bytes())
    assert data["Label"] == "local.nai.worker"
    assert data["ProgramArguments"][1] == "run"
    assert "--config" in data["ProgramArguments"]


def test_worker_module_runs_from_the_command_line() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "nai.remote.worker", "--help"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0
    for action in ("init", "run", "doctor"):
        assert action in result.stdout


def test_remote_package_exports_the_documented_surface() -> None:
    import nai.remote as remote

    for name in ("RemoteTarget", "snapshot_tree", "extract_snapshot", "validate_params",
                 "render_argv", "target_for", "route_for", "node_platform"):
        assert hasattr(remote, name), name
