import hashlib
import json
from pathlib import Path

import pytest

from nai.store import ExportImportError, Store


def _build_exportable_project(store, tmp_path):
    project = store.create_project(
        "Export Me", "Brief", workspace_path=tmp_path / "source-workspace",
        settings={"model_profile": "local", "test_commands": ["pytest -q"]},
    )
    root = store.create_node(project, "export-me", "system", spec_text="# Export Me")
    child = store.create_node(project, "worker", "component", parent_id=root, spec_text="worker")
    store.replace_requirements(child, [{"id": "R1", "text": "Works", "testable": True}])
    store.upsert_test_record(child, "tests/test_worker.py", ["R1"], "frozen123", last_result="pass", last_run_at="2026-01-01T00:00:00Z")
    job = store.create_job(project, "implement", "coder", node_id=child, input_ref={"prompt": "do it"})
    store.update_job(job, status="succeeded", result_ref={"files": ["worker.py"]}, tokens_in=8, tokens_out=9)
    store.add_approval(child, "component-spec", "approved", note="looks good")
    artifact = store.add_artifact(project, "spec", "# Worker spec\n", node_id=child, filename="worker.md")
    cmd = store.enqueue_command(project, "pause")
    store.claim_commands()
    store.complete_command(cmd)
    store.record_event(project, "custom.event", "hello", node_id=child, data={"x": 1})
    return project, root, child, artifact


@pytest.mark.parametrize("archive", [True, False])
def test_project_export_import_round_trip(tmp_path, archive):
    source_db = tmp_path / "source.sqlite3"
    bundle = tmp_path / ("project.zip" if archive else "project-bundle")
    with Store(source_db) as source:
        project, root, child, artifact_id = _build_exportable_project(source, tmp_path)
        source_artifact = source.list_artifacts(project)[0]
        artifact_bytes = (Path(source.get_project(project)["workspace_path"]) / source_artifact["path"]).read_bytes()
        assert hashlib.sha1(artifact_bytes).hexdigest() == source_artifact["sha1"]
        source.export_project(project, bundle)

    target_db = tmp_path / "target.sqlite3"
    with Store(target_db) as target:
        imported = target.import_project(bundle)
        assert imported != project
        imported_project = target.get_project(imported)
        assert imported_project["name"] == "Export Me"
        assert imported_project["settings"]["model_profile"] == "local"
        imported_nodes = target.list_nodes(imported)
        imported_root = imported_nodes[0]
        assert imported_root["id"] != root
        imported_child = target.list_nodes(imported, parent_id=imported_root["id"])[0]
        assert imported_child["id"] != child
        assert imported_child["parent_id"] == imported_root["id"]
        assert target.list_requirements(imported_child["id"])[0]["id"] == "R1"
        assert target.list_test_records(imported_child["id"])[0]["frozen_hash"] == "frozen123"
        jobs = target.list_jobs(imported)
        assert jobs[0]["status"] == "succeeded"
        assert target.list_approvals(imported_child["id"])[0]["decision"] == "approved"
        artifacts = target.list_artifacts(imported)
        assert len(artifacts) == 1
        imported_path = Path(imported_project["workspace_path"]) / artifacts[0]["path"]
        assert imported_path.read_bytes() == artifact_bytes
        assert artifacts[0]["id"] != artifact_id
        assert any(e["type"] == "project.imported" for e in target.list_events(imported))
        # Only terminal command history is included; queued/running work is not replayed.
        assert target.list_commands(imported)[0]["status"] == "succeeded"


def test_import_rejects_unsupported_bundle(tmp_path, store):
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "manifest.json").write_text(json.dumps({"format": "other", "format_version": 1}))
    with pytest.raises(ExportImportError, match="unsupported"):
        store.import_project(bad)


def test_daily_backup_and_retention(tmp_path):
    backup_dir = tmp_path / "backups"
    with Store(tmp_path / "db.sqlite3", backup_dir=backup_dir, backup_keep=2) as store:
        p = store.create_project("backup-test")
        assert store.backup_if_due(today="20261101") is not None
        assert store.backup_if_due(today="20261101") is None
        store.update_project(p, brief="updated")
        store.backup_now(today="20261102")
        store.backup_now(today="20261103")
        copies = sorted(backup_dir.glob("nai-*.sqlite3"))
        assert [p.name for p in copies] == ["nai-20261102.sqlite3", "nai-20261103.sqlite3"]
        # Backups are real SQLite snapshots with the schema.
        import sqlite3
        check = sqlite3.connect(copies[-1])
        try:
            assert check.execute("SELECT name FROM projects WHERE id=?", (p,)).fetchone()[0] == "backup-test"
        finally:
            check.close()


def test_configured_backup_is_created_automatically_on_startup(tmp_path):
    backup_dir = tmp_path / "auto-backups"
    with Store(tmp_path / "auto.sqlite3", backup_dir=backup_dir) as _store:
        pass
    assert len(list(backup_dir.glob("nai-*.sqlite3"))) == 1


def test_daily_backup_scheduler_is_started_and_stopped_with_store(tmp_path):
    store = Store(tmp_path / "scheduled.sqlite3", backup_dir=tmp_path / "scheduled-backups")
    thread = store._backup_thread
    assert thread is not None and thread.is_alive()
    store.close()
    assert not thread.is_alive()


def test_daily_snapshot_refreshes_after_database_changes(tmp_path):
    import datetime
    import sqlite3

    backup_dir = tmp_path / "refresh-backups"
    with Store(tmp_path / "refresh.sqlite3", backup_dir=backup_dir) as store:
        day = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d")
        daily = backup_dir / f"nai-{day}.sqlite3"
        initial_mtime = daily.stat().st_mtime_ns
        project = store.create_project("included-after-write")
        refreshed = store.backup_if_due(today=day)
        assert refreshed == daily
        assert daily.stat().st_mtime_ns >= initial_mtime
        snapshot = sqlite3.connect(daily)
        try:
            assert snapshot.execute("SELECT name FROM projects WHERE id=?", (project,)).fetchone()[0] == "included-after-write"
        finally:
            snapshot.close()
