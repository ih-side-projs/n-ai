"""Project lifecycle: cancelled and archived states (WEB-10)."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from nai.store import Store
from nai.store.store import PROJECT_STATUSES


def test_project_status_validation(tmp_path: Path):
    store = Store(tmp_path / "nai.sqlite3")
    try:
        pid = store.create_project("Lifecycle", "brief")
        for status in sorted(PROJECT_STATUSES):
            store.set_project_status(pid, status)
            assert store.get_project(pid)["status"] == status
        with pytest.raises(ValueError, match="invalid project status"):
            store.set_project_status(pid, "bogus")
    finally:
        store.close()


def test_migration_to_0004_preserves_child_rows(tmp_path: Path):
    """The projects CHECK migration must not cascade-delete nodes/jobs."""
    from nai.store import store as store_module

    legacy_dir = tmp_path / "legacy-migrations"
    legacy_dir.mkdir()
    for name in ("0001_initial.sql", "0002_extend_web_actions.sql", "0003_extend_job_kinds.sql"):
        shutil.copy(Path(store_module.__file__).parent / "migrations" / name, legacy_dir / name)

    db = tmp_path / "legacy.sqlite3"
    store = Store(db, migration_dir=legacy_dir)
    try:
        assert store.schema_version == 3
        pid = store.create_project("Old", "before migration")
        nid = store.create_node(pid, "root", "system")
        jid = store.create_job(pid, "verify", "coder", node_id=nid)
    finally:
        store.close()

    store = Store(db)
    try:
        assert store.schema_version == 8
        assert store.get_project(pid)["status"] == "active"
        assert store.get_node(nid) is not None
        assert store.get_job(jid) is not None
        store.set_project_status(pid, "cancelled")
        assert store.get_project(pid)["status"] == "cancelled"
    finally:
        store.close()
