"""Tests for the amended spec: blocked status and interface-hash dependencies."""
from __future__ import annotations

from nai.sched.readiness import promote_ready, walk_nodes
from nai.store import Store


def _project(tmp_path):
    s = Store(tmp_path / "t.sqlite3")
    pid = s.create_project("P", "brief", workspace_path=tmp_path / "ws")
    return s, pid, s.create_node(pid, "root", "system", contract={"v": 1})


def _to(s, node_id, target):
    path = {
        "ready": ["awaiting_approval", "approved", "ready"],
        "running": ["awaiting_approval", "approved", "ready", "running"],
        "verifying": ["awaiting_approval", "approved", "ready", "running", "verifying"],
        "done": ["awaiting_approval", "approved", "ready", "running", "verifying", "done"],
    }[target]
    for st in path:
        s.transition_node_status(node_id, st)


def test_blocked_is_reachable_from_ready_running_verifying(tmp_path):
    s, pid, root = _project(tmp_path)
    for status in ("ready", "running", "verifying"):
        node = s.create_node(pid, f"n-{status}", "leaf", parent_id=root)
        _to(s, node, status)
        s.transition_node_status(node, "blocked")
        assert s.get_node(node)["status"] == "blocked"


def test_blocked_can_retry_and_fail(tmp_path):
    s, pid, root = _project(tmp_path)
    node = s.create_node(pid, "n", "leaf", parent_id=root)
    _to(s, node, "ready")
    s.transition_node_status(node, "blocked")
    s.transition_node_status(node, "ready")
    assert s.get_node(node)["status"] == "ready"
    s.transition_node_status(node, "blocked")
    s.transition_node_status(node, "failed")
    assert s.get_node(node)["status"] == "failed"


def test_test_authoring_batching(tmp_path):
    s, pid, root = _project(tmp_path)
    s.add_approval(root, "interfaces", "approved")
    dep = s.create_node(pid, "dep", "leaf", parent_id=root)
    _to(s, dep, "running")
    dependent = s.create_node(pid, "dep2", "leaf", parent_id=root, depends_on=[dep])
    s.transition_node_status(dependent, "awaiting_approval")
    s.transition_node_status(dependent, "approved")
    # Default (SCH-4 amended): can promote for test authoring
    assert dependent in promote_ready(s, walk_nodes(s, pid))
    # Strict gating still available
    assert dependent not in promote_ready(s, walk_nodes(s, pid), allow_test_authoring=False)


def test_dependency_contract_change_stales_dependent_but_spec_change_does_not(tmp_path):
    s, pid, root = _project(tmp_path)
    dep = s.create_node(pid, "dep", "leaf", parent_id=root, spec_text="v1", contract={"api": 1})
    dep2 = s.create_node(pid, "dep2", "leaf", parent_id=root, spec_text="B", depends_on=[dep], contract={})
    _to(s, dep, "done")
    _to(s, dep2, "done")
    h = s.get_node(dep2)["content_hash"]

    s.update_node_spec(dep, "v2")
    assert s.get_node(dep2)["content_hash"] == h

    s.update_node_contract(dep, {"api": 2})
    assert s.get_node(dep2)["content_hash"] != h
    assert s.get_node(dep2)["status"] == "stale"
