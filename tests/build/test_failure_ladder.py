"""Tests for BLD-9 failure ladder and BLD-10 incremental rebuild."""

from pathlib import Path
from types import SimpleNamespace

from nai.build.plugin import plan
from tests.build.conftest import FakeStore


def _make_pc(store: FakeStore, project_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        project=store.get_project(project_id),
        store=store,
        active_jobs=lambda nid: [
            j
            for j in store.list_jobs(project_id)
            if j["node_id"] == nid and j["status"] in ("queued", "running")
        ],
    )


def test_BLD9_failure_ladder_coder_retries_then_diagnose_then_escalate(tmp_path: Path):
    store = FakeStore(tmp_path)
    pid = store.create_project(
        "ladder",
        workspace_path=str(tmp_path),
        settings={"max_attempts_coder": 2, "max_attempts_diagnosed": 1},
    )
    nid = store.create_node(pid, "comp", "leaf", spec_text="- R1: return 1")
    store.transition_node_status(nid, "awaiting_approval")
    store.transition_node_status(nid, "approved")
    store.transition_node_status(nid, "ready")

    # 1st implement + verify failure -> planner retries implement (attempt 2/2)
    j_imp1 = store.create_job(pid, "implement", "coder", node_id=nid, attempt=1)
    store.update_job(j_imp1, status="succeeded")
    j_ver1 = store.create_job(pid, "verify", "coder", node_id=nid)
    store.update_job(j_ver1, status="failed", result_ref={"output": "AssertionError on R1"})
    store.transition_node_status(nid, "running")
    store.transition_node_status(nid, "verifying")
    store.transition_node_status(nid, "failed")

    plan(_make_pc(store, pid))
    queued = store.list_jobs(pid, status="queued")
    assert len(queued) == 1
    assert queued[0]["kind"] == "implement"
    assert queued[0]["attempt"] == 2
    assert "AssertionError on R1" in queued[0]["input_ref"]["retry_feedback"]
    assert store.get_node(nid)["status"] == "ready"

    # 2nd implement + verify failure -> coder cap (2) reached -> planner enqueues diagnose
    store.update_job(queued[0]["id"], status="succeeded")
    j_ver2 = store.create_job(pid, "verify", "coder", node_id=nid)
    store.update_job(j_ver2, status="failed", result_ref={"output": "Still failing R1"})
    store.transition_node_status(nid, "running")
    store.transition_node_status(nid, "verifying")
    store.transition_node_status(nid, "failed")

    plan(_make_pc(store, pid))
    queued = store.list_jobs(pid, status="queued")
    assert len(queued) == 1
    assert queued[0]["kind"] == "diagnose"
    assert queued[0]["model_role"] == "reasoner"

    # Diagnose says implementation fault -> planner enqueues 1 diagnosed implement retry
    store.update_job(
        queued[0]["id"],
        status="succeeded",
        result_ref={"fault_kind": "implementation", "revised_brief": "Fix edge case"},
    )
    store.transition_node_status(nid, "running")
    store.transition_node_status(nid, "verifying")
    store.transition_node_status(nid, "failed")

    plan(_make_pc(store, pid))
    queued = store.list_jobs(pid, status="queued")
    assert len(queued) == 1
    assert queued[0]["kind"] == "implement"
    assert queued[0]["input_ref"]["revised_brief"] == "Fix edge case"

    # Diagnosed retry also fails -> node moves to escalated and gets escalated diagnose pass
    store.update_job(queued[0]["id"], status="succeeded")
    j_ver3 = store.create_job(pid, "verify", "coder", node_id=nid)
    store.update_job(j_ver3, status="failed", result_ref={"output": "Final failure"})
    store.transition_node_status(nid, "running")
    store.transition_node_status(nid, "verifying")
    store.transition_node_status(nid, "failed")

    plan(_make_pc(store, pid))
    assert store.get_node(nid)["status"] == "escalated"

    # Next plan tick queues the larger-budget escalated diagnose pass
    plan(_make_pc(store, pid))
    esc_jobs = [
        j for j in store.list_jobs(pid, status="queued")
        if j["kind"] == "diagnose" and j["input_ref"].get("escalated_pass")
    ]
    assert len(esc_jobs) == 1


def test_BLD10_incremental_rebuild_fast_verifies_unchanged_built_hash(tmp_path: Path):
    store = FakeStore(tmp_path)
    pid = store.create_project("inc", workspace_path=str(tmp_path))
    nid = store.create_node(pid, "mod", "leaf", spec_text="- R1: ok")
    store.transition_node_status(nid, "awaiting_approval")
    store.transition_node_status(nid, "approved")
    store.transition_node_status(nid, "ready")
    store.transition_node_status(nid, "running")
    store.transition_node_status(nid, "verifying")
    store.transition_node_status(nid, "done")  # snapshots content_hash -> built_hash

    test_file = tmp_path / "tests" / "test_mod.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("def test_R1_ok():\n    assert True\n", encoding="utf-8")
    store.upsert_test_record(nid, "tests/test_mod.py", ["R1"], "deadbeef")

    # Mark stale without changing spec/contract -> built_hash == content_hash
    store.transition_node_status(nid, "stale", note="Upstream check")
    plan(_make_pc(store, pid))

    assert store.get_node(nid)["status"] == "ready"
    queued = store.list_jobs(pid, status="queued")
    assert len(queued) == 1
    assert queued[0]["kind"] == "verify"