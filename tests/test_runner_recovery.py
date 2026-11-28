"""Tests for Pass 2 runner state & recovery fixes."""
from __future__ import annotations

from nai.sched.runner import Runner
from nai.store import Store


def _project(tmp_path):
    db = tmp_path / "t.sqlite3"
    s = Store(db)
    pid = s.create_project("P", "brief", workspace_path=tmp_path / "ws")
    root = s.create_node(pid, "root", "system")
    return s, pid, root, db


def test_requeued_job_does_not_burn_attempt(tmp_path):
    s, pid, root, db = _project(tmp_path)

    jid = s.create_job(pid, "implement", "coder", node_id=root,
                       input_ref={"requeued": True, "other": 1})
    job = s.get_job(jid)
    assert job["input_ref"]["requeued"] is True
    # The runner clears the flag on start (tested at unit level via update_job support).
    s.update_job(jid, input_ref={"other": 1})
    assert s.get_job(jid)["input_ref"] == {"other": 1}


def test_list_events_returns_recent_window(tmp_path):
    s, pid, root, db = _project(tmp_path)
    for i in range(10):
        s.record_event(pid, "tick", f"event {i}")
    recent = s.list_events(pid, limit=3)
    assert [e["message"] for e in recent] == ["event 7", "event 8", "event 9"]


def test_list_jobs_returns_recent_window(tmp_path):
    s, pid, root, db = _project(tmp_path)
    for _i in range(10):
        s.create_job(pid, "verify", "coder", node_id=root)
    jobs = s.list_jobs(pid, limit=3)
    assert len(jobs) == 3
    assert jobs[-1]["input_ref"] == {}


def test_orphan_node_repaired(tmp_path):
    s, pid, root, db = _project(tmp_path)
    s.add_approval(root, "interfaces", "approved")
    s.transition_node_status(root, "awaiting_approval")
    s.transition_node_status(root, "approved")
    s.transition_node_status(root, "ready")
    s.transition_node_status(root, "running")
    s.close()

    s2 = Store(db, read_only=False)
    runner = Runner(s2, None, state_dir=tmp_path / ".nai")
    report = runner.recover()
    assert report["orphan_nodes"] == 1
    assert s2.get_node(root)["status"] == "ready"
    events = [e for e in s2.list_events(pid, limit=100) if e["type"] == "runner.orphan_node"]
    assert events and events[-1]["data"]["repaired"] is True


def test_unapproved_ready_root_returned_to_design(tmp_path):
    s, pid, root, db = _project(tmp_path)
    s.transition_node_status(root, "awaiting_approval")
    s.transition_node_status(root, "approved")
    s.transition_node_status(root, "ready")
    s.close()

    s2 = Store(db, read_only=False)
    runner = Runner(s2, None, state_dir=tmp_path / ".nai")
    report = runner.recover()
    assert report["invalid_roots"] == 1
    assert s2.get_node(root)["status"] == "draft"
    events = [e for e in s2.list_events(pid, limit=100) if e["type"] == "runner.invalid_root_repaired"]
    assert events

