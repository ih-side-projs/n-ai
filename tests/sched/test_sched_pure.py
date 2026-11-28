"""Pure scheduler logic: SCH-8 windows, SCH-2/SCH-3 selection, X5 budgets, SCH-4 readiness, SCH-9 health and lock."""
import os
from datetime import datetime, timedelta, timezone

import pytest
from fake_store import FakeStore
from sched_helpers import make_node

from nai.sched.budget import Budget, Usage, UsageTracker, budget_for
from nai.sched.config import SchedConfig
from nai.sched.health import RunnerAlreadyRunning, RunnerLock, read_health, write_health
from nai.sched.pick import Batcher, Cand
from nai.sched.readiness import dep_ids, depth_map, promote_ready, walk_nodes
from nai.sched.windows import in_windows, parse_until, parse_window


# ---------------------------------------------------------------- SCH-8 windows
def at(h, m=0):
    return datetime(2026, 11, 16, h, m)


def test_SCH8_no_windows_is_always_open():
    assert in_windows([], at(3))


@pytest.mark.parametrize("h,m,expect", [(22, 0, True), (23, 59, True), (0, 0, True), (5, 59, True), (6, 0, False), (12, 0, False), (21, 59, False)])
def test_SCH8_window_crossing_midnight(h, m, expect):
    assert in_windows(["22:00-06:00"], at(h, m)) is expect


def test_SCH8_daytime_window_and_multiple_windows_and_whole_day():
    assert in_windows(["09:00-17:00"], at(9)) and not in_windows(["09:00-17:00"], at(17))
    assert in_windows(["01:00-02:00", "13:00-14:00"], at(13, 30)) and not in_windows(["01:00-02:00", "13:00-14:00"], at(3))
    assert in_windows(["00:00-00:00"], at(15))


@pytest.mark.parametrize("bad", ["22-06", "25:00-06:00", "22:00-06:60", "", "22:00"])
def test_SCH8_bad_window(bad):
    with pytest.raises(ValueError):
        parse_window(bad)


def test_SCH8_until_parsing():
    assert parse_until("18:30", at(12)) == at(18, 30)
    assert parse_until("06:00", at(12)) == datetime(2026, 11, 17, 6, 0)          # already past today: tomorrow
    assert parse_until("2026-11-20T01:02", at(12)) == datetime(2026, 11, 20, 1, 2)
    with pytest.raises(ValueError):
        parse_until("whenever", at(12))


def test_config_table_and_unknown_key():
    c = SchedConfig.from_table({"concurrency": 2, "budgets": {"max_tokens": 5}})
    assert c.concurrency == 2 and c.budgets == {"max_attempts": 8, "max_wall_seconds": 7200.0, "max_tokens": 5}
    with pytest.raises(ValueError, match="unknown \\[runner\\] key: nope"):
        SchedConfig.from_table({"nope": 1})


# ---------------------------------------------------------------- SCH-2 / SCH-3 selection
def cand(jid, key, depth=1, order=0):
    return Cand({"id": jid}, key, depth, order, "p")


def drain_order(b, cands, now=0.0):
    """Start jobs one at a time (each finishes before the next starts) and return the ids in start order."""
    out, cands = [], list(cands)
    while cands:
        c = b.choose(cands, set(), now)
        if c is None:
            break
        cands.remove(c)
        b.note_started(c, now)
        b.note_loaded(c.key)
        out.append(c.job["id"])
    return out


def test_SCH3_higher_level_first_then_older_first():
    b = Batcher()
    cs = [cand("deep-old", "r", depth=3, order=0), cand("top-new", "r", depth=0, order=9), cand("mid-old", "r", depth=1, order=1),
          cand("mid-new", "r", depth=1, order=5), cand("project", "r", depth=-1, order=50)]
    assert drain_order(b, cs) == ["project", "top-new", "mid-old", "mid-new", "deep-old"]


def test_SCH2_drains_one_model_before_switching():
    b = Batcher(max_consecutive=100)
    cs = [cand("r1", "r", 0, 0), cand("c1", "c", 0, 1), cand("r2", "r", 0, 2), cand("c2", "c", 0, 3), cand("r3", "r", 0, 4)]
    order = drain_order(b, cs)
    assert order == ["r1", "r2", "r3", "c1", "c2"] and b.switches == 1


def test_SCH2_stays_on_loaded_model_even_if_other_job_is_higher_priority():
    b = Batcher()
    b.note_loaded("c")
    cs = [cand("r-top", "r", 0, 0), cand("c-low", "c", 5, 1)]
    assert b.choose(cs, set(), 0.0).job["id"] == "c-low"


def test_SCH2_consecutive_cap_forces_switch_only_when_other_model_has_work():
    b = Batcher(max_consecutive=2)
    cs = [cand(f"r{i}", "r", 0, i) for i in range(4)] + [cand("c1", "c", 0, 10)]
    assert drain_order(b, cs) == ["r0", "r1", "c1", "r2", "r3"]
    b2 = Batcher(max_consecutive=2)
    assert drain_order(b2, [cand(f"r{i}", "r", 0, i) for i in range(5)]) == ["r0", "r1", "r2", "r3", "r4"]     # no other work: keep going


def test_SCH2_time_cap_forces_switch():
    b = Batcher(max_consecutive=100, max_seconds=60)
    first = cand("r1", "r", 0, 0)
    b.note_started(first, 0.0)
    b.note_loaded("r")
    cs = [cand("r2", "r", 0, 1), cand("c1", "c", 0, 2)]
    assert b.choose(cs, set(), 59.0).job["id"] == "r2"
    assert b.choose(cs, set(), 61.0).job["id"] == "c1"


def test_SCH2_never_switch_under_a_running_model_job():
    b = Batcher()
    b.note_started(cand("r1", "r"), 0.0)
    cs = [cand("c1", "c", 0, 0), cand("r2", "r", 5, 1)]
    assert b.choose(cs, {"r"}, 0.0).job["id"] == "r2"                            # same model keeps going in parallel
    assert b.choose([cand("c1", "c")], {"r"}, 0.0) is None                       # only the other model queued: wait


def test_SCH2_jobs_needing_no_model_never_block_or_swap():
    b = Batcher()
    b.note_started(cand("r1", "r"), 0.0)
    b.note_loaded("r")
    free = cand("verify", None, 9, 9)
    assert b.choose([cand("c1", "c", 0, 0), free], set(), 0.0).job["id"] == "verify"
    b.note_started(free, 1.0)
    assert (b.batch_key, b.consecutive) == ("r", 1)                              # free job did not touch the batch


def test_SCH2_nothing_loaded_highest_priority_decides_and_empty_returns_none():
    b = Batcher()
    assert b.choose([], set(), 0) is None
    assert b.choose([cand("c-top", "c", 0, 5), cand("r-low", "r", 4, 0)], set(), 0).job["id"] == "c-top"


def test_SCH2_startup_loaded_model_does_not_trip_time_cap():
    b = Batcher(max_seconds=60)
    b.note_loaded("r")                                                           # from a /running probe, batch never "started"
    assert not b.capped(10_000.0)


# ---------------------------------------------------------------- X5 budgets
def test_X5_budget_defaults_settings_and_multiplier():
    d = {"max_attempts": 8, "max_wall_seconds": 100.0, "max_tokens": 1000}
    assert budget_for(d, None) == Budget(8, 100.0, 1000)
    assert budget_for(d, {"budgets": {"max_attempts": 2}}) == Budget(2, 100.0, 1000)
    assert budget_for(d, None, 2.0) == Budget(8, 200.0, 2000)


def test_X5_usage_sums_terminal_jobs_and_reset():
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    jobs = [{"node_id": "n", "status": "failed", "tokens_in": 10, "tokens_out": 5,
             "started_at": t0.isoformat(), "ended_at": (t0 + timedelta(seconds=30)).isoformat()},
            {"node_id": "n", "status": "running", "tokens_in": 99, "tokens_out": 0, "started_at": None, "ended_at": None},
            {"node_id": None, "status": "succeeded", "tokens_in": 7}]
    tr = UsageTracker()
    tr.load(jobs)
    assert tr.usage("n", attempts=2) == Usage(2, 15, 30.0)
    tr.add("n", 100, 10.0)
    base = tr.reset("n", attempts=2)
    assert base == Usage(2, 115, 40.0)
    assert tr.usage("n", attempts=3, extra_tokens=4, extra_seconds=1.0) == Usage(1, 4, 1.0)


def test_X5_exceeded_reasons():
    b = Budget(3, 60.0, 100)
    assert UsageTracker.exceeded(Usage(2, 99, 59.0), b) is None
    assert "attempts 3" in UsageTracker.exceeded(Usage(3, 0, 0), b)
    assert "tokens 100" in UsageTracker.exceeded(Usage(0, 100, 0), b)
    assert "wall-clock 60s" in UsageTracker.exceeded(Usage(0, 0, 60.0), b)


# ---------------------------------------------------------------- SCH-4 readiness
@pytest.fixture
def tree(tmp_path):
    s = FakeStore()
    pid = s.create_project("p", workspace_path=str(tmp_path))
    root = make_node(s, pid, "root", "approved", kind="system")
    return s, pid, root


def test_SCH4_walk_and_depth_and_dep_ids(tree):
    s, pid, root = tree
    sub = s.create_node(pid, "sub", "subsystem", parent_id=root)
    leaf = s.create_node(pid, "leaf", "leaf", parent_id=sub, depends_on=[])
    nodes = walk_nodes(s, pid)
    assert [n["name"] for n in nodes] == ["root", "sub", "leaf"]
    assert depth_map(nodes) == {root: 0, sub: 1, leaf: 2}
    assert dep_ids({"depends_on": ["a", {"id": "b"}]}) == ["a", "b"] and dep_ids({}) == []


def test_SCH4_root_needs_no_parent_contract(tree):
    s, pid, root = tree
    assert promote_ready(s, walk_nodes(s, pid)) == [root]
    assert s.get_node(root)["status"] == "ready"


def test_SCH4_child_waits_for_parent_contract_approval(tree):
    s, pid, root = tree
    child = make_node(s, pid, "c", "approved", parent_id=root)
    assert promote_ready(s, [n for n in walk_nodes(s, pid) if n["id"] == child]) == []
    s.add_approval(root, "component-map", "approved")                               # wrong stage does not count
    assert promote_ready(s, walk_nodes(s, pid)) == [root]                           # root only
    s.add_approval(root, "interfaces", "rejected")
    assert child not in promote_ready(s, walk_nodes(s, pid))
    s.add_approval(root, "interfaces", "approved")                                   # latest decision wins
    assert promote_ready(s, walk_nodes(s, pid)) == [child]


def test_SCH4_edited_approval_counts_and_option_can_disable_check(tree):
    s, pid, root = tree
    child = make_node(s, pid, "c", "approved", parent_id=root)
    s.add_approval(root, "interfaces", "edited")
    assert child in promote_ready(s, walk_nodes(s, pid))
    c2 = make_node(s, pid, "c2", "approved", parent_id=make_node(s, pid, "other", "draft", kind="component", parent_id=root))
    assert c2 in promote_ready(s, walk_nodes(s, pid), require_contract=False)


def test_SCH4_dependencies_must_all_be_done(tree):
    s, pid, root = tree
    s.add_approval(root, "interfaces", "approved")
    a = make_node(s, pid, "a", "running", parent_id=root)
    b = make_node(s, pid, "b", "done", parent_id=root)
    c = make_node(s, pid, "c", "approved", parent_id=root, depends_on=[a, b])
    # Amended SCH-4: a healthy-but-not-done dependency permits promotion for
    # test-authoring batching; strict gating is tested with allow_test_authoring=False.
    assert c not in promote_ready(s, walk_nodes(s, pid), allow_test_authoring=False)
    s.transition_node_status(a, "verifying")
    s.transition_node_status(a, "done")
    assert c in promote_ready(s, walk_nodes(s, pid), allow_test_authoring=False)


def test_SCH4_only_approved_nodes_are_promoted(tree):
    s, pid, root = tree
    s.add_approval(root, "interfaces", "approved")
    for st in ("draft", "awaiting_approval", "failed", "done"):
        make_node(s, pid, st, st, parent_id=root)
    assert promote_ready(s, walk_nodes(s, pid)) == [root]


# ---------------------------------------------------------------- SCH-9 health and lock
def test_SCH9_health_roundtrip_alive_and_stale(tmp_path):
    assert read_health(tmp_path) is None
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    write_health(tmp_path, {"heartbeat": now, "state": "running", "loaded_model": "r"})
    h = read_health(tmp_path)
    assert h["alive"] and h["loaded_model"] == "r" and h["heartbeat_age_seconds"] < 5
    old = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat(timespec="seconds")
    write_health(tmp_path, {"heartbeat": old, "state": "running"})
    assert read_health(tmp_path)["alive"] is False
    write_health(tmp_path, {"heartbeat": now, "state": "stopped"})
    assert read_health(tmp_path)["alive"] is False
    (tmp_path / "runner.json").write_text("{broken")
    assert read_health(tmp_path) is None
    assert not list(tmp_path.glob("*.tmp"))


def test_STO3_only_one_runner_holds_the_lock(tmp_path):
    a, b = RunnerLock(tmp_path), RunnerLock(tmp_path)
    a.acquire()
    assert (tmp_path / "runner.lock").read_text() == str(os.getpid())
    with pytest.raises(RunnerAlreadyRunning, match="another runner holds"):
        b.acquire()
    a.release()
    b.acquire()
    b.release()
