"""SCH-6: pause, resume, cancel, retry, approve, reject through the commands table (the web process only inserts rows)."""
import threading
import time

import pytest
from fake_store import FakeStore, FakeStoreWithStatus
from sched_helpers import make_node, make_runner, noop

from nai.sched import JobResult
from nai.sched.commands import CommandProcessor, normalize


def cmd_status(store, cid):
    c = store.commands[cid]
    return c["status"], c["error"]


def wait_for(cond, timeout=5.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.01)
    return False


def in_thread(fn):
    t = threading.Thread(target=fn, daemon=True)
    t.start()
    return t


@pytest.mark.parametrize("raw,norm", [("cancel job", "cancel_job"), ("Retry-Node", "retry_node"), (" pause ", "pause")])
def test_SCH6_action_spelling_is_normalised(raw, norm):
    assert normalize(raw) == norm


# ------------------------------------------------------------------------------------ pause / resume
def test_SCH6_pause_stops_new_jobs_but_not_running_one_then_resume(store, pid, tmp_path):
    gate, order = threading.Event(), []

    def slow(ctx):
        order.append(("start", ctx.job["id"]))
        gate.wait(5)
        order.append(("end", ctx.job["id"]))
        return JobResult.success()
    j1, j2 = store.create_job(pid, "verify", "coder"), store.create_job(pid, "review", "coder")
    r = make_runner(store, None, tmp_path, threaded=True)
    r.registry.register("verify", slow, uses_model=False)
    r.registry.register("review", slow, uses_model=False)

    def driver():
        assert wait_for(lambda: order == [("start", j1)])
        cid = store.enqueue_command(pid, "pause")
        assert wait_for(lambda: store.commands[cid]["status"] == "done")
        gate.set()                                                               # running job finishes...
        assert wait_for(lambda: store.jobs[j1]["status"] == "succeeded")
        time.sleep(0.3)                                                          # ...and the next one does NOT start
        assert store.jobs[j2]["status"] == "queued"
        store.enqueue_command(pid, "resume")
        assert wait_for(lambda: store.jobs[j2]["status"] == "succeeded")
        r.stop()                                                                 # drain would exit while paused; stop ends the loop
    in_thread(driver)
    r.run()
    assert order == [("start", j1), ("end", j1), ("start", j2), ("end", j2)]
    assert store.events_of("project.paused") and store.events_of("project.resumed")


def test_SCH6_runner_wide_pause_and_health_state(store, pid, tmp_path):
    j = store.create_job(pid, "verify", "coder")
    r = make_runner(store, None, tmp_path)
    r.registry.register("verify", noop, uses_model=False)
    cid = store.enqueue_command(pid, "pause", payload={"scope": "runner"})
    for _ in range(3):
        r.step()
    assert r.paused and store.jobs[j]["status"] == "queued" and cmd_status(store, cid) == ("done", None)
    import json
    assert json.loads((tmp_path / "state" / "runner.json").read_text())["state"] == "paused"
    store.enqueue_command(pid, "resume", payload={"scope": "runner"})
    r.run(drain=True)
    assert store.jobs[j]["status"] == "succeeded"


def test_SCH6_paused_project_does_not_block_other_projects(store, tmp_path):
    a, b = store.create_project("a"), store.create_project("b")
    ja, jb = store.create_job(a, "verify", "coder"), store.create_job(b, "verify", "coder")
    r = make_runner(store, None, tmp_path)
    r.registry.register("verify", noop, uses_model=False)
    store.enqueue_command(a, "pause")
    r.run(drain=True)
    assert store.jobs[jb]["status"] == "succeeded" and store.jobs[ja]["status"] == "queued"


@pytest.mark.parametrize("store_cls", [FakeStore, FakeStoreWithStatus])
def test_SCH6_pause_survives_restart_with_or_without_set_project_status(store_cls, tmp_path):
    s = store_cls()
    pid = s.create_project("p")
    j = s.create_job(pid, "verify", "coder")
    r1 = make_runner(s, None, tmp_path)
    r1.registry.register("verify", noop, uses_model=False)
    s.enqueue_command(pid, "pause")
    r1.step()
    assert pid in r1.paused_projects
    if store_cls is FakeStoreWithStatus:
        assert s.projects[pid]["status"] == "paused"                             # used the store's own status when it has one
    r2 = make_runner(s, None, tmp_path)                                          # "restart"
    r2.registry.register("verify", noop, uses_model=False)
    r2.start()
    r2.step()
    assert pid in r2.paused_projects and s.jobs[j]["status"] == "queued"
    s.enqueue_command(pid, "resume")
    r2.run(drain=True)
    assert s.jobs[j]["status"] == "succeeded"


def test_SCH6_restart_restores_runner_wide_pause(store, pid, tmp_path):
    r1 = make_runner(store, None, tmp_path)
    store.enqueue_command(pid, "pause", payload={"scope": "runner"})
    r1.step()
    r2 = make_runner(store, None, tmp_path)
    r2.start()
    assert r2.paused is True


def test_SCH6_pause_state_restore_is_independent_of_event_order(tmp_path):
    for newest_first in (False, True):
        s = FakeStore(events_newest_first=newest_first)
        pid = s.create_project("p")
        r = make_runner(s, None, tmp_path)
        for act in ("pause", "resume", "pause"):
            s.enqueue_command(pid, act)
            r.step()
        r2 = make_runner(s, None, tmp_path)
        r2.start()
        assert pid in r2.paused_projects, newest_first


# ------------------------------------------------------------------------------------ cancel
def test_SCH6_cancel_queued_job(store, pid, tmp_path):
    j = store.create_job(pid, "verify", "coder")
    r = make_runner(store, None, tmp_path)
    cid = store.enqueue_command(pid, "cancel_job", payload={"job_id": j})
    r.step()
    assert store.jobs[j]["status"] == "cancelled" and cmd_status(store, cid) == ("done", None)


def test_SCH6_cancel_running_job_cooperatively(store, pid, tmp_path):
    j = store.create_job(pid, "verify", "coder")
    started = threading.Event()

    def loop(ctx):
        started.set()
        for _ in range(500):
            ctx.check()
            time.sleep(0.01)
        return JobResult.success()
    r = make_runner(store, None, tmp_path, threaded=True)
    r.registry.register("verify", loop, uses_model=False)

    def driver():
        started.wait(5)
        store.enqueue_command(pid, "cancel_job", payload={"job_id": j})
    in_thread(driver)
    t0 = time.time()
    r.run(drain=True)
    assert store.jobs[j]["status"] == "cancelled" and time.time() - t0 < 4 and r.stats["jobs_cancelled"] == 1


def test_SCH6_cancel_kills_a_running_command_process_group(store, tmp_path):
    ws_dir = tmp_path / "ws"
    pid = store.create_project("p", workspace_path=str(ws_dir), settings={"git": False})
    j = store.create_job(pid, "verify", "coder")
    started = threading.Event()

    def sleeper(ctx):
        started.set()
        out = ctx.ws.run_command("sleep 30; echo unreachable", 60)               # no ctx.check() inside: only the kill ends it
        return JobResult.failure(f"command ended: {out.splitlines()[0]}")
    r = make_runner(store, None, tmp_path, threaded=True)
    r.registry.register("verify", sleeper, uses_model=False)

    def driver():
        started.wait(5)
        time.sleep(0.5)                                                          # let bash start
        store.enqueue_command(pid, "cancel_job", payload={"job_id": j})
    in_thread(driver)
    t0 = time.time()
    r.run(drain=True)
    assert time.time() - t0 < 6, "cancel did not kill the process group"
    assert store.jobs[j]["status"] == "cancelled" and store.jobs[j]["error"] == "command ended: exit code: 130"


def test_SCH6_cancel_by_node_and_errors(store, pid, tmp_path):
    n = make_node(store, pid, "n", "ready")
    j1, j2 = store.create_job(pid, "verify", "coder", node_id=n), store.create_job(pid, "review", "coder", node_id=n)
    other = store.create_job(pid, "diagnose", "coder")
    r = make_runner(store, None, tmp_path)
    c1 = store.enqueue_command(pid, "cancel_job", node_id=n)
    c2 = store.enqueue_command(pid, "cancel_job", payload={"job_id": "nope"})
    c3 = store.enqueue_command(pid, "cancel_job")
    c4 = store.enqueue_command(pid, "cancel_job", node_id="n-without-jobs")
    r.step()
    assert [store.jobs[j]["status"] for j in (j1, j2)] == ["cancelled", "cancelled"] and store.jobs[other]["status"] != "cancelled"
    assert cmd_status(store, c1) == ("done", None)
    assert cmd_status(store, c2)[1] == "job nope is not active"
    assert "needs payload.job_id" in cmd_status(store, c3)[1]
    assert "has no active job" in cmd_status(store, c4)[1]


# ------------------------------------------------------------------------------------ retry
def test_SCH6_retry_moves_failed_node_to_ready_and_errors_otherwise(store, pid, tmp_path):
    failed = make_node(store, pid, "f", "failed", attempts=5)
    done = make_node(store, pid, "d", "done")
    r = make_runner(store, None, tmp_path)
    ok = store.enqueue_command(pid, "retry_node", node_id=failed)
    bad = store.enqueue_command(pid, "retry_node", node_id=done)
    none = store.enqueue_command(pid, "retry_node")
    gone = store.enqueue_command(pid, "retry_node", node_id="zz")
    r.step()
    assert store.get_node(failed)["status"] == "ready" and cmd_status(store, ok) == ("done", None)
    assert "only failed, escalated or stale" in cmd_status(store, bad)[1]
    assert cmd_status(store, none)[1] == "retry_node needs a node_id" and "not found" in cmd_status(store, gone)[1]
    ev = store.events_of("node.retry")[0]
    assert ev["node_id"] == failed and ev["data"]["attempts_base"] == 5


def test_SCH6_retry_works_for_escalated_and_stale(store, pid, tmp_path):
    esc = make_node(store, pid, "e", "failed")
    store.transition_node_status(esc, "escalated")
    stale = make_node(store, pid, "s", "done")
    store.transition_node_status(stale, "stale")
    r = make_runner(store, None, tmp_path)
    for n in (esc, stale):
        store.enqueue_command(pid, "retry_node", node_id=n)
    r.step()
    assert store.get_node(esc)["status"] == store.get_node(stale)["status"] == "ready"


# ------------------------------------------------------------------------------------ approve / reject
def test_SCH6_approve_component_spec_moves_awaiting_node_to_approved(store, pid, tmp_path):
    n = make_node(store, pid, "n", "awaiting_approval")
    r = make_runner(store, None, tmp_path)
    cid = store.enqueue_command(pid, "approve", node_id=n, payload={"stage": "component-spec", "note": "lgtm"})
    r.step()
    # approved, and the same step's readiness pass then promoted it (a root node has no parent contract to wait for)
    assert [e["data"]["to"] for e in store.events_of("node.transition") if e["node_id"] == n][-2:] == ["approved", "ready"]
    assert cmd_status(store, cid) == ("done", None)
    (a,) = store.list_approvals(n)
    assert (a["stage"], a["decision"], a["note"]) == ("component-spec", "approved", "lgtm")


def test_SCH6_approve_cascade_approves_each_awaiting_child(store, pid, tmp_path):
    parent = make_node(store, pid, "p", "draft", kind="component")
    kids = [make_node(store, pid, f"k{i}", "awaiting_approval", parent_id=parent) for i in range(2)]
    draft_kid = make_node(store, pid, "d", "draft", parent_id=parent)
    r = make_runner(store, None, tmp_path)
    store.enqueue_command(pid, "approve", node_id=parent, payload={"stage": "component-spec", "cascade": True})
    r.step()
    assert [store.get_node(k)["status"] for k in kids] == ["approved", "approved"] and store.get_node(draft_kid)["status"] == "draft"   # parent contract not approved: not promoted
    assert all(store.list_approvals(k) for k in kids)


def test_SCH6_other_stages_only_record_the_approval(store, pid, tmp_path):
    n = make_node(store, pid, "n", "draft", kind="system")
    r = make_runner(store, None, tmp_path)
    for stage in ("component-map", "interfaces"):
        store.enqueue_command(pid, "approve", node_id=n, payload={"stage": stage})
    r.step()
    assert store.get_node(n)["status"] == "draft"
    assert [a["stage"] for a in store.list_approvals(n)] == ["component-map", "interfaces"]
    assert len(store.events_of("approval.recorded")) == 2


def test_SCH6_approve_escalation_resolves_to_ready_with_fresh_budget(store, pid, tmp_path):
    n = make_node(store, pid, "n", "failed", attempts=7)
    store.transition_node_status(n, "escalated")
    r = make_runner(store, None, tmp_path)
    store.enqueue_command(pid, "approve", node_id=n, payload={"stage": "escalation", "note": "use approach B"})
    r.step()
    assert store.get_node(n)["status"] == "ready"
    assert r.tracker.usage(n, 7).attempts == 0


def test_SCH6_reject_component_spec_sends_node_back_to_draft_with_feedback(store, pid, tmp_path):
    n = make_node(store, pid, "n", "awaiting_approval")
    r = make_runner(store, None, tmp_path)
    store.enqueue_command(pid, "reject", node_id=n, payload={"stage": "component-spec", "note": "missing error handling"})
    r.step()
    assert store.get_node(n)["status"] == "draft"                                # via stale: STO-4 has no direct edge
    path = [e["data"]["to"] for e in store.events_of("node.transition") if e["node_id"] == n][-2:]
    assert path == ["stale", "draft"]
    ev = store.events_of("approval.recorded")[0]
    assert ev["data"] == {"stage": "component-spec", "decision": "rejected", "note": "missing error handling"}


def test_SCH6_reject_other_stage_leaves_status_alone(store, pid, tmp_path):
    n = make_node(store, pid, "n", "failed")
    store.transition_node_status(n, "escalated")
    r = make_runner(store, None, tmp_path)
    store.enqueue_command(pid, "reject", node_id=n, payload={"stage": "escalation", "note": "no"})
    r.step()
    assert store.get_node(n)["status"] == "escalated" and store.list_approvals(n)[0]["decision"] == "rejected"


@pytest.mark.parametrize("payload,node,msg", [({}, True, "needs payload.stage"), ({"stage": "bogus"}, True, "needs payload.stage"),
                                              ({"stage": "interfaces"}, False, "needs a node_id")])
def test_SCH6_approve_validation_errors(store, pid, tmp_path, payload, node, msg):
    n = make_node(store, pid, "n", "draft")
    r = make_runner(store, None, tmp_path)
    cid = store.enqueue_command(pid, "approve", node_id=n if node else None, payload=payload)
    r.step()
    assert msg in cmd_status(store, cid)[1] and store.list_approvals(n) == []


# ------------------------------------------------------------------------------------ robustness
def test_SCH6_unknown_action_and_crashing_command_are_completed_with_error(store, pid, tmp_path):
    r = make_runner(store, None, tmp_path)
    out = CommandProcessor(r).process({"id": "x", "project_id": pid, "action": "explode", "payload": None})
    assert out == "unsupported action 'explode'"
    n = make_node(store, pid, "n", "failed")
    store.transition_node_status = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db locked"))
    cid = store.enqueue_command(pid, "retry_node", node_id=n)
    r.step()
    assert cmd_status(store, cid) == ("done", "RuntimeError: db locked")
    assert store.events_of("command.error")


def test_SCH6_payload_as_json_text_and_audit_event(store, pid, tmp_path):
    j = store.create_job(pid, "verify", "coder")
    r = make_runner(store, None, tmp_path)
    out = CommandProcessor(r).process({"id": "c", "project_id": pid, "action": "cancel job", "payload": f'{{"job_id": "{j}"}}'})
    assert out is None and store.jobs[j]["status"] == "cancelled"
    cid = store.enqueue_command(pid, "pause")
    r.step()
    ev = store.events_of("command.processed")[0]
    assert ev["data"]["command_id"] == cid and ev["data"]["action"] == "pause" and ev["data"]["error"] is None


def test_SCH6_project_cancel_marks_project_cancelled(store, pid, tmp_path):
    store.create_job(pid, "verify", "coder")
    r = make_runner(store, None, tmp_path)
    cid = store.enqueue_command(pid, "cancel")
    r.step()
    assert store.get_project(pid)["status"] == "cancelled"
    assert cmd_status(store, cid)[1] is None
    assert store.events_of("project.cancelled")


def test_SCH6_archive_and_unarchive_through_command_queue(store, pid, tmp_path):
    r = make_runner(store, None, tmp_path)
    store.enqueue_command(pid, "archive")
    r.step()
    assert store.get_project(pid)["status"] == "archived"

    store.enqueue_command(pid, "unarchive")
    r.step()
    assert store.get_project(pid)["status"] == "active"
    assert store.events_of("project.archived")
    assert store.events_of("project.unarchived")



def test_SCH6_intake_answers_and_begin_design(store, pid, tmp_path):
    root = store.create_node(pid, "root", "system")
    store.update_project(pid, settings={"intake": {"description": "x"}, "intake_complete": False,
                                        "intake_waiting": True})
    r = make_runner(store, None, tmp_path)

    store.enqueue_command(pid, "answer_intake", payload={"answers": {"Q1": "developers"}})
    r.step()
    settings = store.get_project(pid)["settings"]
    assert settings["intake"]["answers"]["Q1"] == "developers"
    assert settings["intake_waiting"] is False
    assert store.events_of("project.intake_answers")

    store.enqueue_command(pid, "begin_design")
    r.step()
    forced_jobs = [j for j in store.list_jobs(pid) if j.get("kind") == "intake"]
    assert len(forced_jobs) == 1
    assert forced_jobs[0]["node_id"] == root
    assert forced_jobs[0]["input_ref"]["force_refine"] is True

    settings = store.get_project(pid)["settings"]
    assert settings["intake_complete"] is True
    assert settings["intake_forced"] is True
    assert settings["clarified"] is True
    assert store.events_of("project.design_forced")



def test_interfaces_approval_can_follow_component_map(store, pid, tmp_path):
    root = store.create_node(pid, "root", "system")
    store.transition_node_status(root, "awaiting_approval")
    store.transition_node_status(root, "approved")
    r = make_runner(store, None, tmp_path)

    store.enqueue_command(pid, "approve", node_id=root, payload={"stage": "interfaces"})
    r.step()

    approvals = store.list_approvals(root)
    assert any(a["stage"] == "interfaces" and a["decision"] == "approved" for a in approvals)
    assert store.get_node(root)["status"] in ("approved", "ready")

