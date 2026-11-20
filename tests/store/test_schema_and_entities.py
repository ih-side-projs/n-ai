import sqlite3

import pytest

from nai.store import Store


def test_startup_applies_numbered_migration_and_wal(tmp_path):
    db = tmp_path / "store.sqlite3"
    with Store(db) as store:
        assert store.schema_version >= 1
        assert store.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert store.conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        tables = {r[0] for r in store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        expected = {
            "projects", "nodes", "requirements", "test_records", "jobs", "events",
            "artifacts", "approvals", "model_profiles", "tool_defs", "commands",
            "node_dependencies", "schema_migrations",
        }
        assert expected <= tables
        indexes = {r[0] for r in store.conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert "idx_nodes_project_status" in indexes
        assert "idx_jobs_status_role" in indexes
        assert "idx_events_project_time" in indexes


def test_project_node_requirement_job_and_profile_round_trip(store, project):
    p = store.get_project(project)
    assert p["name"] == "Test Project"
    assert p["settings"]["target_env"]["python"] == "3.12"

    root = store.create_node(project, "test-project", "system", spec_text="# Root")
    child = store.create_node(project, "component-a", "component", parent_id=root, objective="Does A")
    store.set_node_test_command(child, "pytest -q tests/test_component_a.py")
    assert store.increment_node_attempts(child) == 1
    assert store.increment_node_attempts(child, 2) == 3
    assert store.get_node(child)["test_command"] == "pytest -q tests/test_component_a.py"
    assert store.get_node(child)["attempts"] == 3
    store.replace_requirements(child, [
        {"id": "R1", "text": "Does the thing", "testable": True},
        {"id": "E1", "text": "Handles the edge case", "testable": False},
    ])
    assert store.list_requirements(child) == [
        {"id": "E1", "node_id": child, "text": "Handles the edge case", "testable": False},
        {"id": "R1", "node_id": child, "text": "Does the thing", "testable": True},
    ]
    store.upsert_test_record(child, "tests/test_component.py", ["R1"], "abc123", last_result="pass")
    assert store.list_test_records(child)[0]["requirement_ids"] == ["R1"]

    job = store.create_job(project, "implement", "coder", node_id=child, input_ref={"task": "build"})
    store.update_job(job, status="running", started_at="2026-01-01T00:00:00Z", tokens_in=10)
    store.update_job(job, status="succeeded", ended_at="2026-01-01T00:01:00Z", tokens_out=20, result_ref={"ok": True})
    fetched = store.get_job(job)
    assert fetched["status"] == "succeeded"
    assert fetched["input_ref"] == {"task": "build"}
    assert fetched["result_ref"] == {"ok": True}

    store.save_model_profile("local", {"model_id": "reasoner", "ctx": 32768}, {"model_id": "coder"}, swap_cost_seconds=12.5)
    assert store.get_model_profile("local")["reasoner"]["ctx"] == 32768
    store.register_tool_def("read_csv", "Read CSV", {"type": "object"}, "data", "none", 30)
    assert store.list_tool_defs(family="data")[0]["name"] == "read_csv"


def test_foreign_keys_reject_orphan_rows(store, project):
    with pytest.raises(sqlite3.IntegrityError):
        with store._transaction() as conn:
            conn.execute(
                "INSERT INTO jobs(id, project_id, kind, model_role, created_at) VALUES ('j1','missing','implement','coder','now')"
            )


def test_relational_rows_cannot_reference_nodes_from_another_project(store):
    import sqlite3

    project_a = store.create_project("A")
    project_b = store.create_project("B")
    node_b = store.create_node(project_b, "b-root", "system")
    with pytest.raises(sqlite3.IntegrityError):
        store.create_job(project_a, "implement", "coder", node_id=node_b)
    with pytest.raises(sqlite3.IntegrityError):
        store.record_event(project_a, "wrong-project", "not allowed", node_id=node_b)
    with pytest.raises(sqlite3.IntegrityError):
        store.enqueue_command(project_a, "retry_node", node_id=node_b)
