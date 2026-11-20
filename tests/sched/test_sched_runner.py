"""SCH-1/2/3 end to end: job lifecycle, X6 recording, model-affinity batching, priority, threaded execution."""
import threading
import time

import pytest
from fake_store import FakeStore
from sched_helpers import chat_handler, make_node, make_runner, noop

from nai import terminal
from nai.sched import JobResult
from nai.testing.stub_server import StubServer, reply


def jobs_by_status(store, status):
    return [j for j in store.jobs.values() if j["status"] == status]


# ------------------------------------------------------------------------------------------ lifecycle / X6
def test_SCH1_job_runs_and_is_recorded(store, pid, tmp_path):
    nid = make_node(store, pid, "leaf-a", "ready")
    jid = store.create_job(pid, "implement", "coder", node_id=nid)
    with StubServer(coder=[reply("hello", prompt_tokens=40, completion_tokens=7)]) as s:
        r = make_runner(store, s, tmp_path)
        r.registry.register("implement", chat_handler)
        stats = r.run(drain=True)
    j = store.jobs[jid]
    assert j["status"] == "succeeded" and j["result_ref"] == {"text": "hello"}
    assert (j["tokens_in"], j["tokens_out"]) == (40, 7) and j["started_at"] and j["ended_at"] and j["error"] is None
    assert stats["jobs_succeeded"] == 1 and stats["model_calls"] == 1
    kinds = [e["type"] for e in store.events if e["job_id"] == jid]
    assert kinds[0] == "job.started" and "model.call" in kinds and kinds[-1] == "job.finished"
    assert store.get_node(nid)["attempts"] == 1                                  # implement counts an attempt


def test_X6_model_call_event_and_full_artifact(store, pid, tmp_path):
    jid = store.create_job(pid, "verify", "reasoner")
    with StubServer(consult=[reply("answer", prompt_tokens=50, completion_tokens=9)]) as s:
        r = make_runner(store, s, tmp_path)
        r.registry.register("verify", chat_handler)
        r.run(drain=True)
    (ev,) = store.events_of("model.call")
    d = ev["data"]
    assert (d["model"], d["role"], d["prompt_tokens"], d["completion_tokens"], d["swapped"]) == ("r", "reasoner", 50, 9, True)
    assert d["swap_method"] == "running" and ev["job_id"] == jid
    (art,) = store.artifacts
    assert art["kind"] == "log" and art["filename"] == f"{jid}-call-001.json" and d["artifact_id"] == art["id"]
    import json
    full = json.loads(art["content"])
    assert full["request"]["messages"][0]["content"] == f"job {jid}" and full["response"]["content"] == "answer"
    assert full["meta"]["job_id"] == jid


def test_X6_swap_recorded_with_count_and_seconds(store, pid, tmp_path):
    for role in ("reasoner", "coder", "coder"):
        store.create_job(pid, "verify" if role == "reasoner" else "implement", role)
    with StubServer(consult=[reply("a")], coder=[reply("b"), reply("c")], swap_delay=0.05) as s:
        r = make_runner(store, s, tmp_path)
        r.registry.register("verify", chat_handler)
        r.registry.register("implement", chat_handler)
        stats = r.run(drain=True)
    assert stats["swaps"] == 2 and stats["swap_seconds"] >= 0.1
    swaps = store.events_of("model.swap")
    assert [e["data"]["model"] for e in swaps] == ["r", "c"] and all(e["data"]["seconds_is_estimate"] for e in swaps)


def test_X6_call_artifacts_can_be_disabled_and_need_a_workspace(store, tmp_path):
    p_no_ws = store.create_project("nows")
    store.create_job(p_no_ws, "verify", "reasoner")
    with StubServer(consult=[reply("a")]) as s:
        r = make_runner(store, s, tmp_path)
        r.registry.register("verify", chat_handler)
        r.run(drain=True)
    assert store.artifacts == [] and store.events_of("model.call")[0]["data"]["artifact_id"] is None


def test_SCH1_unknown_kind_fails_with_clear_message(store, pid, tmp_path):
    jid = store.create_job(pid, "diagnose", "reasoner")
    r = make_runner(store, None, tmp_path)
    r.registry.register("verify", noop)
    r.run(drain=True)
    assert store.jobs[jid]["status"] == "failed"
    assert store.jobs[jid]["error"] == "no handler registered for job kind 'diagnose' (registered: verify)"


def test_SCH1_handler_exception_dict_and_none_returns(store, pid, tmp_path):
    ids = {k: store.create_job(pid, k, "coder") for k in ("implement", "verify", "review", "diagnose")}

    def boom(ctx):
        raise ValueError("kaput")
    r = make_runner(store, None, tmp_path)
    r.registry.register("implement", boom, uses_model=False)
    r.registry.register("verify", lambda ctx: {"ok": 1}, uses_model=False)
    r.registry.register("review", lambda ctx: None, uses_model=False)
    r.registry.register("diagnose", lambda ctx: "nonsense", uses_model=False)
    r.run(drain=True)
    assert store.jobs[ids["implement"]]["status"] == "failed" and store.jobs[ids["implement"]]["error"] == "ValueError: kaput"
    fin = [e for e in store.events_of("job.finished") if e["job_id"] == ids["implement"]][0]
    assert "Traceback" in fin["data"]["trace"] and fin["level"] == "error"
    assert store.jobs[ids["verify"]]["status"] == "succeeded" and store.jobs[ids["verify"]]["result_ref"] == {"ok": 1}
    assert store.jobs[ids["review"]]["status"] == "succeeded" and store.jobs[ids["review"]]["result_ref"] is None
    assert store.jobs[ids["diagnose"]]["status"] == "failed" and "expected JobResult" in store.jobs[ids["diagnose"]]["error"]


def test_SCH1_failure_result_and_requeue_result(store, pid, tmp_path):
    a, b = store.create_job(pid, "verify", "coder"), store.create_job(pid, "review", "coder")
    calls = {"n": 0}

    def requeue_once(ctx):
        calls["n"] += 1
        return JobResult(requeue=True) if calls["n"] == 1 else JobResult.success(done=True)
    r = make_runner(store, None, tmp_path)
    r.registry.register("verify", lambda ctx: JobResult.failure("tests fail", failing=3), uses_model=False)
    r.registry.register("review", requeue_once, uses_model=False)
    r.run(drain=True)
    assert (store.jobs[a]["status"], store.jobs[a]["error"], store.jobs[a]["result_ref"]) == ("failed", "tests fail", {"failing": 3})
    assert store.jobs[b]["status"] == "succeeded" and calls["n"] == 2 and r.stats["jobs_requeued"] == 1


def test_WEB5_log_lines_become_job_events(store, pid, tmp_path):
    jid = store.create_job(pid, "verify", "coder")

    def talky(ctx):
        terminal.log("coder", "step 1 → write_file a.py")
        terminal.log("coder", "something odd", "yellow")
        return JobResult.success()
    r = make_runner(store, None, tmp_path)
    r.registry.register("verify", talky, uses_model=False)
    r.run(drain=True)
    logs = [e for e in store.events_of("job.log") if e["job_id"] == jid]
    assert [(e["message"], e["level"]) for e in logs] == [("[coder] step 1 → write_file a.py", "info"), ("[coder] something odd", "warning")]
    terminal.log("x", "after the run")                                           # sink removed: nothing recorded
    assert len(store.events_of("job.log")) == 2


def test_WEB5_log_events_capped_per_job(store, pid, tmp_path):
    store.create_job(pid, "verify", "coder")
    r = make_runner(store, None, tmp_path, max_log_events_per_job=3)
    r.registry.register("verify", lambda ctx: [terminal.log("t", f"l{i}") for i in range(10)] and None, uses_model=False)
    r.run(drain=True)
    assert len(store.events_of("job.log")) == 3


def test_ctx_helpers_enqueue_event_artifact(store, pid, tmp_path):
    nid = make_node(store, pid, "n", "ready")
    first = store.create_job(pid, "author_tests", "reasoner", node_id=nid)

    def h(ctx):
        ctx.event("custom.thing", "hello", level="warning", data={"a": 1})
        aid = ctx.artifact("report", "body", "r.md")
        ctx.enqueue("implement", "coder", input_ref={"from": ctx.job["id"]})
        return JobResult.success(artifact=aid)
    r = make_runner(store, None, tmp_path)
    r.registry.register("author_tests", h, uses_model=False)
    r.registry.register("implement", noop, uses_model=False)
    r.run(drain=True)
    assert store.events_of("custom.thing")[0]["node_id"] == nid
    (second,) = [j for j in store.jobs.values() if j["kind"] == "implement"]
    assert second["node_id"] == nid and second["input_ref"] == {"from": first} and second["status"] == "succeeded"
    assert store.artifacts[0]["filename"] == "r.md"


# ------------------------------------------------------------------------------------------ SCH-2 batching end to end
def request_models(stub):
    return [r["model"] for r in stub.requests]


def test_SCH2_batching_end_to_end_minimises_swaps(store, pid, tmp_path):
    for role in ("reasoner", "coder") * 3:                                       # interleaved r c r c r c
        store.create_job(pid, "verify", role)
    with StubServer(consult=[reply("a")] * 3, coder=[reply("b")] * 3) as s:
        r = make_runner(store, s, tmp_path)
        r.registry.register("verify", chat_handler)
        stats = r.run(drain=True)
        assert request_models(s) == ["r", "r", "r", "c", "c", "c"]
    assert stats["swaps"] == 2 and r.batcher.switches == 1


def test_SCH2_consecutive_cap_end_to_end(store, pid, tmp_path):
    for role in ("reasoner", "coder") * 3:
        store.create_job(pid, "verify", role)
    with StubServer(consult=[reply("a")] * 3, coder=[reply("b")] * 3) as s:
        r = make_runner(store, s, tmp_path, max_consecutive_jobs=2)
        r.registry.register("verify", chat_handler)
        r.run(drain=True)
        assert request_models(s) == ["r", "r", "c", "c", "r", "c"]


def test_SCH2_shared_model_means_no_swap_between_roles(store, pid, tmp_path):
    """Batching is keyed by model id: reasoner and coder pointing at one model never count as a swap."""
    for role in ("reasoner", "coder", "reasoner"):
        store.create_job(pid, "verify", role)
    with StubServer(consult=[reply("a")] * 3, models=("r", "c")) as s:
        r = make_runner(store, s, tmp_path)
        r.app_config.profiles["default"] = r.app_config.profiles["default"].__class__(
            "default", r.app_config.profiles["default"].reasoner, r.app_config.profiles["default"].reasoner)
        r.registry.register("verify", chat_handler)
        r.run(drain=True)
        assert request_models(s) == ["r", "r", "r"] and r.stats["swaps"] == 1       # only the first load


def test_SCH3_priority_across_node_depth_then_age(store, pid, tmp_path):
    root = make_node(store, pid, "root", "running", kind="system")
    sub = make_node(store, pid, "sub", "running", kind="subsystem", parent_id=root)
    leaf = make_node(store, pid, "leaf", "running", parent_id=sub)
    order = []
    j_leaf = store.create_job(pid, "implement", "coder", node_id=leaf)
    j_sub1 = store.create_job(pid, "verify", "coder", node_id=sub)
    j_root = store.create_job(pid, "review", "coder", node_id=root)
    j_sub2 = store.create_job(pid, "diagnose", "coder", node_id=sub)
    j_project = store.create_job(pid, "decompose", "coder")                       # no node: project level, runs first

    def rec(ctx):
        order.append(ctx.job["id"])
        return JobResult.success()
    r = make_runner(store, None, tmp_path)
    for k in ("implement", "verify", "review", "diagnose", "decompose"):
        r.registry.register(k, rec, uses_model=False)
    r.run(drain=True)
    assert order == [j_project, j_root, j_sub1, j_sub2, j_leaf]


def test_SCH3_age_survives_the_store_returning_newest_first(tmp_path):
    class Newest(FakeStore):
        def list_jobs(self, *a, **k):
            return super().list_jobs(*a, **k)[::-1]
    s = Newest()
    pid = s.create_project("p", workspace_path=str(tmp_path))
    ids = [s.create_job(pid, "verify", "coder") for _ in range(3)]
    order = []
    r = make_runner(s, None, tmp_path)
    r.registry.register("verify", lambda ctx: order.append(ctx.job["id"]) or None, uses_model=False)
    r.step()                                                                     # first sight happens in one listing...
    assert order == [ids[2]]                                                     # ...so equal-age ties fall back to id order
    r.run(drain=True)
    assert sorted(order) == sorted(ids)


def test_SCH3_jobs_of_projects_are_served_by_the_same_rules(store, tmp_path):
    p1, p2 = store.create_project("a"), store.create_project("b")
    j2 = store.create_job(p2, "verify", "coder")
    j1 = store.create_job(p1, "verify", "coder")
    order = []
    r = make_runner(store, None, tmp_path)
    r.registry.register("verify", lambda ctx: order.append(ctx.job["id"]) or None, uses_model=False)
    r.run(drain=True)
    assert set(order) == {j1, j2}


# ------------------------------------------------------------------------------------------ threaded execution
def test_SCH1_threaded_job_uses_store_through_proxy(store, pid, tmp_path):
    nid = make_node(store, pid, "n", "ready")
    jid = store.create_job(pid, "verify", "coder", node_id=nid)
    seen = {}

    def h(ctx):
        seen["thread"] = threading.current_thread().name
        ctx.store.record_event(pid, "from.worker", "hi")                          # proxied to the main thread
        seen["node"] = ctx.store.get_node(nid)["name"]
        with pytest.raises(Exception) as ei:                                      # exceptions travel back to the worker
            ctx.store.transition_node_status(nid, "done")
        seen["exc"] = type(ei.value).__name__
        return JobResult.success()
    r = make_runner(store, None, tmp_path, threaded=True)
    r.registry.register("verify", h, uses_model=False)
    r.run(drain=True)
    assert seen["thread"].startswith("nai-job") and seen["node"] == "n" and seen["exc"] == "InvalidTransition"
    assert store.events_of("from.worker") and store.jobs[jid]["status"] == "succeeded"


def test_SCH3_concurrency_runs_same_model_jobs_in_parallel(store, pid, tmp_path):
    for _ in range(3):
        store.create_job(pid, "verify", "coder")
    gate, active, peak = threading.Lock(), [0], [0]

    def h(ctx):
        with gate:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.15)
        with gate:
            active[0] -= 1
        return JobResult.success()
    r = make_runner(store, None, tmp_path, threaded=True, concurrency=3)
    r.registry.register("verify", h, uses_model=False)
    r.run(drain=True)
    assert peak[0] == 3 and len(jobs_by_status(store, "succeeded")) == 3


def test_SCH3_default_concurrency_is_one(store, pid, tmp_path):
    for _ in range(3):
        store.create_job(pid, "verify", "coder")
    active, peak = [0], [0]

    def h(ctx):
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        time.sleep(0.05)
        active[0] -= 1
    r = make_runner(store, None, tmp_path, threaded=True)
    r.registry.register("verify", h, uses_model=False)
    r.run(drain=True)
    assert peak[0] == 1


def test_SCH2_no_swap_under_a_running_job_with_concurrency(store, pid, tmp_path):
    store.create_job(pid, "verify", "reasoner")
    store.create_job(pid, "implement", "coder")
    release = threading.Event()
    started = []

    def slow_r(ctx):
        started.append("r-start")
        release.wait(5)
        ctx.client.chat("reasoner", [{"role": "user", "content": "x"}])
        started.append("r-end")
        return JobResult.success()

    def c(ctx):
        started.append("c-start")
        ctx.client.chat("coder", [{"role": "user", "content": "y"}])
        return JobResult.success()
    with StubServer(consult=[reply("a")], coder=[reply("b")]) as s:
        r = make_runner(store, s, tmp_path, threaded=True, concurrency=2)
        r.registry.register("verify", slow_r)
        r.registry.register("implement", c)
        threading.Timer(0.4, release.set).start()
        r.run(drain=True)
    assert started == ["r-start", "r-end", "c-start"]


def test_threaded_store_call_without_a_running_loop_fails_fast(store, pid, tmp_path, monkeypatch):
    from nai.sched import store_api
    monkeypatch.setattr(store_api, "CALL_TIMEOUT", 0.2)
    r = make_runner(store, None, tmp_path)
    out = {}

    def worker():
        try:
            r.store_proxy.list_projects()
        except Exception as e:
            out["e"] = e
    t = threading.Thread(target=worker)
    t.start()
    t.join(3)
    assert "not serviced" in str(out["e"])
