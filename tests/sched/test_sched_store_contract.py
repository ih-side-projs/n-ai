"""The scheduler against the Store interface in WP2's INTERFACES.md.

Every test runs against FakeStore. When the real package is importable (`nai.store`), the same tests also run against it:
that is the check that FakeStore really matches WP2. They use only methods listed in INTERFACES.md, and inline execution
only (a Store connection is not shared between threads here).
"""
import importlib.util

import pytest
from fake_store import FakeStore
from sched_helpers import chat_handler, make_node, make_runner, noop

from nai.sched import JobResult
from nai.testing.stub_server import StubServer, reply

HAVE_REAL = importlib.util.find_spec("nai.store") is not None


@pytest.fixture(params=["fake", pytest.param("real", marks=pytest.mark.skipif(not HAVE_REAL, reason="nai.store (WP2) is not installed in this environment"))])
def st(request, tmp_path):
    if request.param == "fake":
        yield FakeStore()
    else:
        from nai.store import Store
        s = Store(str(tmp_path / "nai.sqlite3"))
        yield s
        s.close()


def project(st, tmp_path):
    return st.create_project("p", "brief", str(tmp_path / "ws"))


def events(st, pid, etype):
    return [e for e in st.list_events(pid, limit=100000) if e.get("type", e.get("event_type")) == etype]


def test_contract_job_lifecycle_tokens_events_artifacts(st, tmp_path):
    pid = project(st, tmp_path)
    nid = make_node(st, pid, "leaf", "ready")
    j = st.create_job(pid, "implement", "coder", node_id=nid)
    with StubServer(coder=[reply("hi", prompt_tokens=30, completion_tokens=5)]) as s:
        r = make_runner(st, s, tmp_path)
        r.registry.register("implement", chat_handler)
        r.run(drain=True)
    job = st.get_job(j)
    assert job["status"] == "succeeded" and job["tokens_in"] == 30 and job["tokens_out"] == 5
    assert events(st, pid, "model.call") and events(st, pid, "job.finished")
    arts = st.list_artifacts(pid, node_id=nid)
    assert len(arts) == 1 and arts[0]["kind"] == "log"
    assert st.get_node(nid)["attempts"] == 1


def test_contract_readiness_planner_and_dependencies(st, tmp_path):
    pid = project(st, tmp_path)
    root = make_node(st, pid, "root", "draft", kind="component")
    st.add_approval(root, "interfaces", "approved")
    a = make_node(st, pid, "a", "approved", parent_id=root)
    b = make_node(st, pid, "b", "approved", parent_id=root, depends_on=[a])
    order = []

    def planner(pc):
        for n in pc.idle_ready_nodes():
            st.create_job(pc.project["id"], "implement", "coder", node_id=n["id"])

    def impl(ctx):
        order.append(ctx.node["name"])
        for s_ in ("running", "verifying", "done"):
            ctx.store.transition_node_status(ctx.node["id"], s_)
    r = make_runner(st, None, tmp_path)
    r.registry.register("implement", impl, uses_model=False)
    r.register_planner(planner)
    r.run(drain=True)
    assert order == ["a", "b"] and st.get_node(b)["status"] == "done"


def test_contract_commands_pause_retry_approve_cancel(st, tmp_path):
    pid = project(st, tmp_path)
    failed = make_node(st, pid, "f", "failed")
    awaiting = make_node(st, pid, "w", "awaiting_approval")
    queued = st.create_job(pid, "verify", "coder")
    r = make_runner(st, None, tmp_path)
    st.enqueue_command(pid, "retry_node", node_id=failed)
    st.enqueue_command(pid, "approve", node_id=awaiting, payload={"stage": "component-spec"})
    st.enqueue_command(pid, "cancel_job", payload={"job_id": queued})
    r.step()
    assert st.get_node(failed)["status"] == "ready"
    assert st.get_node(awaiting)["status"] in ("approved", "ready")
    assert st.get_job(queued)["status"] == "cancelled"
    assert st.claim_commands() == []                                                            # all three were completed


def test_contract_crash_recovery_and_budget_failure(st, tmp_path):
    pid = project(st, tmp_path)
    nid = make_node(st, pid, "n", "running", attempts=0)
    stuck = st.create_job(pid, "verify", "coder", node_id=nid)
    st.update_job(stuck, status="running", started_at="2026-11-16T00:00:00+00:00")
    over = make_node(st, pid, "o", "ready", attempts=3)
    refused = st.create_job(pid, "implement", "coder", node_id=over)
    r = make_runner(st, None, tmp_path, budgets={"max_attempts": 3})
    r.registry.register("verify", noop, uses_model=False)
    r.registry.register("implement", noop, uses_model=False)
    r.run(drain=True)
    assert st.get_job(stuck)["status"] == "succeeded" and events(st, pid, "runner.recovered_job")
    assert st.get_job(refused)["status"] == "failed" and st.get_node(over)["status"] == "failed"


def test_contract_pause_restore_from_events_after_restart(st, tmp_path):
    pid = project(st, tmp_path)
    j = st.create_job(pid, "verify", "coder")
    r1 = make_runner(st, None, tmp_path)
    st.enqueue_command(pid, "pause")
    r1.step()
    r2 = make_runner(st, None, tmp_path)
    r2.registry.register("verify", noop, uses_model=False)
    r2.step()
    assert st.get_job(j)["status"] == "queued"
