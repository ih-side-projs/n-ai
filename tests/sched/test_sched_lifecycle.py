"""SCH-7 / X4 recovery and shutdown, SCH-1 planners and plugins, SCH-8 windows in the runner, SCH-9 health, transient failures."""
import sys
import threading
import time
import types
from datetime import datetime

import pytest
from fake_store import FakeStore
from sched_helpers import FakeClock, make_node, make_runner, noop

from nai.sched import JobResult, read_health
from nai.sched.plugins import load_plugins
from nai.sched.registry import HandlerRegistry


def wait_for(cond, timeout=5.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.01)
    return False


# ------------------------------------------------------------------------------------------ SCH-7 recovery
def test_SCH7_running_jobs_are_requeued_and_run_again_after_a_crash(store, pid, tmp_path):
    nid = make_node(store, pid, "n", "running")
    j = store.create_job(pid, "verify", "coder", node_id=nid)
    store.update_job(j, status="running", started_at="2026-11-16T00:00:00+00:00")        # the runner died mid-job
    r = make_runner(store, None, tmp_path)
    r.registry.register("verify", noop, uses_model=False)
    r.run(drain=True)
    ev = store.events_of("runner.recovered_job")
    assert len(ev) == 1 and ev[0]["job_id"] == j and ev[0]["level"] == "warning"
    assert store.jobs[j]["status"] == "succeeded" and store.jobs[j]["started_at"] != "2026-11-16T00:00:00+00:00"
    assert store.events_of("runner.started")[0]["data"] == {"requeued_jobs": 1, "orphan_nodes": 0}


def test_SCH7_orphaned_running_nodes_are_reported_not_touched(store, pid, tmp_path):
    a, b = make_node(store, pid, "a", "running"), make_node(store, pid, "b", "verifying")
    covered = make_node(store, pid, "c", "running")
    store.create_job(pid, "verify", "coder", node_id=covered)
    r = make_runner(store, None, tmp_path)
    rep = r.recover()
    assert rep == {"requeued_jobs": 0, "orphan_nodes": 2}
    assert {e["node_id"] for e in store.events_of("runner.orphan_node")} == {a, b}
    assert store.get_node(a)["status"] == "running"


def test_SCH7_recovery_is_idempotent_and_skips_archived_projects(store, pid, tmp_path):
    j = store.create_job(pid, "verify", "coder")
    store.update_job(j, status="running")
    old = store.create_project("old")
    store.projects[old]["status"] = "archived"
    oj = store.create_job(old, "verify", "coder")
    store.update_job(oj, status="running")
    r = make_runner(store, None, tmp_path)
    assert r.recover()["requeued_jobs"] == 1 and r.recover()["requeued_jobs"] == 0
    assert store.jobs[oj]["status"] == "running"


def test_X4_hard_stop_cancels_running_job_and_requeues_it_for_the_next_start(store, pid, tmp_path):
    j = store.create_job(pid, "verify", "coder")
    started = threading.Event()

    def long(ctx):
        started.set()
        for _ in range(1000):
            ctx.check()
            time.sleep(0.01)
    r = make_runner(store, None, tmp_path, threaded=True)
    r.registry.register("verify", long, uses_model=False)
    threading.Thread(target=lambda: (started.wait(5), r.stop(graceful=False)), daemon=True).start()
    r.run()
    assert store.jobs[j]["status"] == "queued" and store.jobs[j]["started_at"] is None       # not 'cancelled': it continues next time
    assert r.stats["jobs_requeued"] == 1
    r2 = make_runner(store, None, tmp_path)
    r2.registry.register("verify", noop, uses_model=False)
    r2.run(drain=True)
    assert store.jobs[j]["status"] == "succeeded"


def test_X4_graceful_stop_lets_the_running_job_finish_and_starts_no_more(store, pid, tmp_path):
    j1, j2 = store.create_job(pid, "verify", "coder"), store.create_job(pid, "review", "coder")
    started, release = threading.Event(), threading.Event()

    def slow(ctx):
        started.set()
        release.wait(5)
        return JobResult.success()
    r = make_runner(store, None, tmp_path, threaded=True)
    r.registry.register("verify", slow, uses_model=False)
    r.registry.register("review", noop, uses_model=False)

    def driver():
        started.wait(5)
        r.stop()
        time.sleep(0.2)
        release.set()
    threading.Thread(target=driver, daemon=True).start()
    r.run()
    assert store.jobs[j1]["status"] == "succeeded" and store.jobs[j2]["status"] == "queued"


def test_SCH7_run_releases_lock_and_second_runner_is_refused(store, pid, tmp_path):
    from nai.sched import RunnerAlreadyRunning
    from nai.sched.health import RunnerLock
    r = make_runner(store, None, tmp_path)
    held = RunnerLock(tmp_path / "state")
    held.acquire()
    with pytest.raises(RunnerAlreadyRunning):
        r.run(drain=True)
    held.release()
    r.run(drain=True)                                                                          # lock free again


# ------------------------------------------------------------------------------------------ planners / plugins
def test_SCH1_planner_creates_first_job_for_idle_ready_nodes_and_readiness_is_applied(store, pid, tmp_path):
    root = make_node(store, pid, "root", "approved", kind="component")
    store.add_approval(root, "interfaces", "approved")
    kids = [make_node(store, pid, f"k{i}", "approved", parent_id=root) for i in range(2)]
    seen = []

    def planner(pc):
        for n in pc.idle_ready_nodes():
            if n["kind"] == "leaf":
                seen.append(n["name"])
                store.create_job(pc.project["id"], "implement", "coder", node_id=n["id"])
    done = []

    def impl(ctx):
        done.append(ctx.node["name"])
        ctx.store.transition_node_status(ctx.node["id"], "running")
        ctx.store.transition_node_status(ctx.node["id"], "verifying")
        ctx.store.transition_node_status(ctx.node["id"], "done")
        return JobResult.success()
    r = make_runner(store, None, tmp_path)
    r.registry.register("implement", impl, uses_model=False)
    r.register_planner(planner)
    r.run(drain=True)
    assert sorted(seen) == ["k0", "k1"] and sorted(done) == ["k0", "k1"]                     # not re-planned while a job existed
    assert [store.get_node(k)["status"] for k in kids] == ["done", "done"]


def test_SCH4_dependent_node_starts_only_after_its_dependency_is_done(store, pid, tmp_path):
    root = make_node(store, pid, "root", "draft", kind="component")
    store.add_approval(root, "interfaces", "approved")
    a = make_node(store, pid, "a", "approved", parent_id=root)
    b = make_node(store, pid, "b", "approved", parent_id=root, depends_on=[a])
    order = []

    def planner(pc):
        for n in pc.idle_ready_nodes():
            store.create_job(pc.project["id"], "implement", "coder", node_id=n["id"])

    def impl(ctx):
        order.append(ctx.node["name"])
        for st in ("running", "verifying", "done"):
            ctx.store.transition_node_status(ctx.node["id"], st)
    r = make_runner(store, None, tmp_path)
    r.registry.register("implement", impl, uses_model=False)
    r.register_planner(planner)
    r.run(drain=True)
    assert order == ["a", "b"]


def test_SCH1_planner_context_helpers_and_error_isolation(store, pid, tmp_path):
    n = make_node(store, pid, "n", "ready")
    j = store.create_job(pid, "verify", "coder", node_id=n)
    got = {}

    def good(pc):
        got["ready"] = [x["id"] for x in pc.nodes_with_status("ready")]
        got["active"] = [x["id"] for x in pc.active_jobs(n)]
        got["idle"] = pc.idle_ready_nodes()
        got["usage"] = pc.usage(pc.nodes_by_id[n])

    def bad(pc):
        raise RuntimeError("planner bug")
    r = make_runner(store, None, tmp_path)
    r.register_planner(bad)
    r.register_planner(good)
    r.step()
    assert got["ready"] == [n] and got["active"] == [j] and got["idle"] == [] and got["usage"].attempts == 0
    err = store.events_of("planner.error")[0]
    assert "planner bug" in err["message"] and err["level"] == "error" and "Traceback" in err["data"]["trace"]


def test_SCH1_planners_skip_paused_projects(store, tmp_path):
    a, b = store.create_project("a"), store.create_project("b")
    r = make_runner(store, None, tmp_path)
    seen = []
    r.register_planner(lambda pc: seen.append(pc.project["id"]))
    store.enqueue_command(a, "pause")
    r.step()
    r.step()
    assert set(seen) == {b}


def test_plugins_are_loaded_by_module_name_and_missing_or_broken_ones_are_skipped(store, tmp_path, monkeypatch, capsys):
    good = types.ModuleType("fake_nai_plugin")
    good.register = lambda runner: runner.registry.register("verify", noop, uses_model=False)
    noreg = types.ModuleType("fake_noreg")
    monkeypatch.setitem(sys.modules, "fake_nai_plugin", good)
    monkeypatch.setitem(sys.modules, "fake_noreg", noreg)
    r = make_runner(store, None, tmp_path)
    loaded = load_plugins(r, ["fake_nai_plugin", "nai.not_installed_yet", "fake_noreg"])
    assert loaded == ["fake_nai_plugin"] and r.registry.kinds() == ["verify"]
    assert "has no register(runner) function" in capsys.readouterr().out


def test_plugin_that_fails_inside_its_own_imports_is_reported(store, tmp_path, monkeypatch, capsys):
    (tmp_path / "broken_plugin.py").write_text("import a_dependency_that_is_not_installed_xyz\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    r = make_runner(store, None, tmp_path)
    assert load_plugins(r, ["broken_plugin"]) == []
    assert "plugin broken_plugin failed to import" in capsys.readouterr().out                 # absent dependency is not "plugin absent"


# ------------------------------------------------------------------------------------------ SCH-8 windows
def test_SCH8_no_job_starts_outside_the_window_and_one_starts_when_it_opens(store, pid, tmp_path):
    j = store.create_job(pid, "verify", "coder")
    clock = FakeClock(local=datetime(2026, 11, 16, 12, 0))
    r = make_runner(store, None, tmp_path, clock=clock, windows=["22:00-06:00"])
    r.registry.register("verify", noop, uses_model=False)
    for _ in range(3):
        r.step()
    assert store.jobs[j]["status"] == "queued" and read_health(tmp_path / "state")["state"] == "out_of_window"
    clock.advance(10 * 3600 + 60)                                                              # 22:01
    r.step()
    assert store.jobs[j]["status"] == "succeeded"


def test_SCH8_window_closing_does_not_interrupt_a_running_job(store, pid, tmp_path):
    j1, j2 = store.create_job(pid, "verify", "coder"), store.create_job(pid, "review", "coder")
    clock = FakeClock(local=datetime(2026, 11, 16, 5, 59))
    r = make_runner(store, None, tmp_path, clock=clock, windows=["22:00-06:00"])

    def closes_window(ctx):
        clock.advance(120)                                                                     # now 06:01
    r.registry.register("verify", closes_window, uses_model=False)
    r.registry.register("review", noop, uses_model=False)
    r.step()
    r.step()
    assert store.jobs[j1]["status"] == "succeeded" and store.jobs[j2]["status"] == "queued"


def test_SCH8_until_stops_the_runner_after_the_running_job(store, pid, tmp_path):
    j1, j2 = store.create_job(pid, "verify", "coder"), store.create_job(pid, "review", "coder")
    clock = FakeClock(local=datetime(2026, 11, 16, 12, 0))
    r = make_runner(store, None, tmp_path, clock=clock, until=datetime(2026, 11, 16, 12, 30))
    r.registry.register("verify", lambda ctx: clock.advance(3600), uses_model=False)         # the first job runs past --until
    r.registry.register("review", noop, uses_model=False)
    r.run()
    assert store.jobs[j1]["status"] == "succeeded" and store.jobs[j2]["status"] == "queued"
    h = read_health(tmp_path / "state")
    assert h["state"] == "stopped" and h["stop_reason"] == "--until time reached"


# ------------------------------------------------------------------------------------------ SCH-9 health
def test_SCH9_health_describes_current_job_loaded_model_and_queue_depth(store, pid, tmp_path):
    from nai.testing.stub_server import StubServer, reply
    store.create_job(pid, "verify", "reasoner")
    store.create_job(pid, "implement", "coder")
    store.create_job(pid, "implement", "coder")
    snap = {}
    from sched_helpers import chat_handler

    def peek(ctx):
        r = chat_handler(ctx)
        snap.update(read_health(tmp_path / "state") or {})
        return r
    with StubServer(consult=[reply("a")], coder=[reply("b")] * 2) as s:
        r = make_runner(store, s, tmp_path)
        r.registry.register("verify", peek)
        r.registry.register("implement", chat_handler)
        r.step()                                                                              # runs the reasoner job inline
        h = read_health(tmp_path / "state")
        r.run(drain=True)
    assert h["queue_depth"] == {"reasoner": 1, "coder": 2} or h["queue_depth"] == {"coder": 2, "reasoner": 1}
    assert h["alive"] and h["loaded_model"] == "r" and h["state"] in ("idle", "running") and h["pid"]
    assert h["handlers"] == ["implement", "verify"] and h["stats"]["swaps"] == 1
    end = read_health(tmp_path / "state")
    assert end["state"] == "stopped" and end["alive"] is False and end["stats"]["jobs_succeeded"] == 3


def test_SCH9_heartbeat_keeps_ticking_while_a_long_job_runs(store, pid, tmp_path, monkeypatch):
    from nai.sched import runner as runner_mod
    store.create_job(pid, "verify", "coder")
    writes, release = [], threading.Event()
    real = runner_mod.write_health
    monkeypatch.setattr(runner_mod, "write_health", lambda d, data: (writes.append((data["state"], len(data["current_jobs"]))), real(d, data)))

    def long(ctx):
        release.wait(5)
    r = make_runner(store, None, tmp_path, threaded=True, heartbeat_seconds=0.05, poll_seconds=0.02)
    r.registry.register("verify", long, uses_model=False)
    threading.Thread(target=lambda: (time.sleep(0.6), release.set()), daemon=True).start()
    r.run(drain=True)
    during = [w for w in writes if w == ("running", 1)]
    assert len(during) >= 6, writes                                                           # ~every 50ms while the job ran


# ------------------------------------------------------------------------------------------ transient model-server failures
class ServerUnavailable(Exception):
    pass


def test_X4_unreachable_model_server_requeues_the_job_and_backs_off(store, pid, tmp_path):
    j = store.create_job(pid, "implement", "coder")
    free = store.create_job(pid, "verify", "coder")
    attempts = {"n": 0}

    def flaky(ctx):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ServerUnavailable("connection refused")
        return JobResult.success()
    clock = FakeClock()
    r = make_runner(store, None, tmp_path, clock=clock, transient_retries=5)
    r.registry.register("implement", flaky)
    r.registry.register("verify", noop, uses_model=False)
    r.step()
    assert store.jobs[j]["status"] == "queued" and store.jobs[free]["status"] in ("queued", "succeeded")
    assert len(store.events_of("runner.server_unavailable")) == 1
    r.step()                                                                                  # backoff: model job waits, free job runs
    assert attempts["n"] == 1 and store.jobs[free]["status"] == "succeeded"
    assert read_health(tmp_path / "state")["state"] == "backoff"
    clock.advance(3)
    r.step()                                                                                  # 2nd failure, backoff doubles
    assert attempts["n"] == 2 and r._backoff_delay == 8.0
    clock.advance(10)
    r.step()
    assert attempts["n"] == 3 and store.jobs[j]["status"] == "succeeded"
    assert store.get_node  # sanity


def test_X4_transient_retries_are_limited(store, pid, tmp_path):
    j = store.create_job(pid, "implement", "coder")
    clock = FakeClock()
    r = make_runner(store, None, tmp_path, clock=clock, transient_retries=2)
    r.registry.register("implement", lambda ctx: (_ for _ in ()).throw(ServerUnavailable("down")))
    for _ in range(6):
        r.step()
        clock.advance(100)
    assert store.jobs[j]["status"] == "failed" and "unreachable after 3 attempts" in store.jobs[j]["error"]


def test_X4_real_connection_refused_is_recognised_as_transient(store, pid, tmp_path):
    from sched_helpers import chat_handler
    from nai.config import Config
    from nai.llm import LLM
    j = store.create_job(pid, "implement", "coder")
    r = make_runner(store, None, tmp_path)
    r.llm = LLM("http://127.0.0.1:9/v1", "none", stream=False, show_thinking=False, probe_running=False)
    r.app_config = Config()
    r.registry.register("implement", chat_handler)
    r.step()
    assert store.jobs[j]["status"] == "queued" and store.events_of("runner.server_unavailable")
