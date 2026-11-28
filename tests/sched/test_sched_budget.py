"""SCH-5 / X5: per-node budgets enforced before a job starts and between model calls; restart-safe."""
from datetime import datetime, timedelta, timezone

from fake_store import FakeStore
from sched_helpers import make_node, make_runner, noop

from nai.sched import JobResult
from nai.testing.stub_server import StubServer, reply


def finished_job(store, pid, nid, kind="implement", tokens_in=0, tokens_out=0, seconds=0, status="failed"):
    jid = store.create_job(pid, kind, "coder", node_id=nid)
    t0 = datetime(2026, 11, 15, tzinfo=timezone.utc)
    store.update_job(jid, status=status, tokens_in=tokens_in, tokens_out=tokens_out, started_at=t0.isoformat(),
                     ended_at=(t0 + timedelta(seconds=seconds)).isoformat())
    return jid


def test_SCH5_attempts_exhausted_fails_job_and_node_before_running(store, pid, tmp_path):
    nid = make_node(store, pid, "n", "ready", attempts=3)
    j = store.create_job(pid, "implement", "coder", node_id=nid)
    ran = []
    r = make_runner(store, None, tmp_path, budgets={"max_attempts": 3})
    r.registry.register("implement", lambda ctx: ran.append(1), uses_model=False)
    r.run(drain=True)
    assert not ran and store.jobs[j]["status"] == "failed" and "attempts 3 reached the limit of 3" in store.jobs[j]["error"]
    assert store.get_node(nid)["status"] == "failed"                             # ready -> running -> failed (only legal path)
    ev = store.events_of("budget.exceeded")[0]
    assert ev["data"]["budget"]["max_attempts"] == 3 and ev["level"] == "warning"
    assert store.events_of("node.budget_failed")


def test_SCH5_attempts_are_counted_per_implement_job_started(store, pid, tmp_path):
    nid = make_node(store, pid, "n", "running")
    jobs = [store.create_job(pid, "implement", "coder", node_id=nid) for _ in range(5)]
    ran = []
    r = make_runner(store, None, tmp_path, budgets={"max_attempts": 3})
    r.registry.register("implement", lambda ctx: ran.append(ctx.job["id"]) or JobResult.failure("nope"), uses_model=False)
    r.run(drain=True)
    assert ran == jobs[:3] and store.get_node(nid)["attempts"] == 3
    assert [store.jobs[j]["status"] for j in jobs] == ["failed"] * 5 and store.get_node(nid)["status"] == "failed"


def test_SCH5_attempt_limit_does_not_block_other_job_kinds_so_the_ladder_can_diagnose(store, pid, tmp_path):
    nid = make_node(store, pid, "n", "failed", attempts=9)
    j = store.create_job(pid, "diagnose", "reasoner", node_id=nid)
    r = make_runner(store, None, tmp_path, budgets={"max_attempts": 3})
    r.registry.register("diagnose", noop, uses_model=False)
    r.run(drain=True)
    assert store.jobs[j]["status"] == "succeeded" and store.get_node(nid)["attempts"] == 9


def test_SCH5_token_budget_from_history_survives_restart(store, pid, tmp_path):
    nid = make_node(store, pid, "n", "ready")
    finished_job(store, pid, nid, tokens_in=900, tokens_out=200)
    j = store.create_job(pid, "diagnose", "reasoner", node_id=nid)
    r = make_runner(store, None, tmp_path, budgets={"max_tokens": 1000})         # a fresh Runner rebuilds usage from stored jobs
    r.registry.register("diagnose", noop, uses_model=False)
    r.run(drain=True)
    assert "tokens 1100 reached the limit of 1000" in store.jobs[j]["error"] and store.get_node(nid)["status"] == "failed"


def test_SCH5_wall_clock_budget_from_history(store, pid, tmp_path):
    nid = make_node(store, pid, "n", "running")
    finished_job(store, pid, nid, seconds=500)
    j = store.create_job(pid, "verify", "coder", node_id=nid)
    r = make_runner(store, None, tmp_path, budgets={"max_wall_seconds": 400})
    r.registry.register("verify", noop, uses_model=False)
    r.run(drain=True)
    assert "wall-clock 500s reached the limit of 400s" in store.jobs[j]["error"] and store.get_node(nid)["status"] == "failed"


def test_SCH5_budget_enforced_between_model_calls_of_a_running_job(store, pid, tmp_path):
    nid = make_node(store, pid, "n", "running")
    j = store.create_job(pid, "verify", "coder", node_id=nid)
    calls = []

    def three_calls(ctx):
        for i in range(3):
            ctx.client.chat("coder", [{"role": "user", "content": str(i)}])
            calls.append(i)
        return JobResult.success()
    with StubServer(coder=[reply("x", prompt_tokens=400, completion_tokens=100)] * 3) as s:
        r = make_runner(store, s, tmp_path, budgets={"max_tokens": 800})
        r.registry.register("verify", three_calls)
        r.run(drain=True)
    assert calls == [0] and "tokens 1000 reached the limit of 800" in store.jobs[j]["error"]     # stopped after call 2's accounting
    assert (store.jobs[j]["tokens_in"], store.jobs[j]["tokens_out"]) == (800, 200)
    assert store.get_node(nid)["status"] == "failed" and store.events_of("budget.exceeded")


def test_SCH5_project_settings_override_runner_defaults(store, tmp_path):
    pid = store.create_project("p", workspace_path=str(tmp_path), settings={"budgets": {"max_tokens": 100}})
    nid = make_node(store, pid, "n", "running")
    finished_job(store, pid, nid, tokens_in=150)
    j = store.create_job(pid, "verify", "coder", node_id=nid)
    r = make_runner(store, None, tmp_path)                                       # runner default max_tokens is 2,000,000
    r.registry.register("verify", noop, uses_model=False)
    r.run(drain=True)
    assert store.jobs[j]["status"] == "failed" and "limit of 100" in store.jobs[j]["error"]


def test_SCH5_partial_budget_dict_is_completed_with_built_in_defaults(store, pid, tmp_path):
    from nai.sched.budget import Budget, budget_for
    assert budget_for({"max_attempts": 2}, None) == Budget(2, 7200.0, 2_000_000)


def test_SCH5_multiplier_lets_an_escalation_pass_run(store, tmp_path):
    pid = store.create_project("p", workspace_path=str(tmp_path), settings={"budgets": {"max_tokens": 100}})
    nid = make_node(store, pid, "n", "running")
    finished_job(store, pid, nid, tokens_in=150)
    boosted = store.create_job(pid, "review", "coder", node_id=nid, input_ref={"budget_multiplier": 3})
    r = make_runner(store, None, tmp_path)
    r.registry.register("review", noop, uses_model=False)
    r.run(drain=True)
    assert store.jobs[boosted]["status"] == "succeeded"


def test_SCH5_jobs_without_a_node_have_no_node_budget(store, pid, tmp_path):
    j = store.create_job(pid, "decompose", "reasoner")
    r = make_runner(store, None, tmp_path, budgets={"max_tokens": 1, "max_attempts": 1})
    r.registry.register("decompose", noop, uses_model=False)
    r.run(drain=True)
    assert store.jobs[j]["status"] == "succeeded"


def test_SCH5_node_that_cannot_legally_fail_is_reported_not_forced(store, pid, tmp_path):
    nid = make_node(store, pid, "n", "draft")
    finished_job(store, pid, nid, tokens_in=5000)
    j = store.create_job(pid, "decompose", "reasoner", node_id=nid)
    r = make_runner(store, None, tmp_path, budgets={"max_tokens": 100})
    r.registry.register("decompose", noop, uses_model=False)
    r.run(drain=True)
    assert store.jobs[j]["status"] == "failed" and store.get_node(nid)["status"] == "draft"
    assert "cannot move to failed" in store.events_of("budget.node_not_failed")[0]["message"]


def test_SCH5_retry_grants_a_fresh_budget_and_that_survives_restart(store, pid, tmp_path):
    nid = make_node(store, pid, "n", "failed", attempts=3)
    finished_job(store, pid, nid, tokens_in=900)
    r1 = make_runner(store, None, tmp_path, budgets={"max_attempts": 3, "max_tokens": 1000})
    r1.registry.register("implement", noop, uses_model=False)
    store.enqueue_command(pid, "retry_node", node_id=nid)
    r1.step()
    assert store.get_node(nid)["status"] == "ready"
    r2 = make_runner(store, None, tmp_path, budgets={"max_attempts": 3, "max_tokens": 1000})   # restart: base comes from the node.retry event
    r2.registry.register("implement", noop, uses_model=False)
    j = store.create_job(pid, "implement", "coder", node_id=nid)
    r2.run(drain=True)
    assert store.jobs[j]["status"] == "succeeded" and store.get_node(nid)["attempts"] == 4


def test_SCH5_events_order_independence_for_retry_base(tmp_path):
    for newest_first in (False, True):
        s = FakeStore(events_newest_first=newest_first)
        pid = s.create_project("p", workspace_path=str(tmp_path))
        nid = make_node(s, pid, "n", "failed", attempts=3)
        finished_job(s, pid, nid, tokens_in=900)
        r1 = make_runner(s, None, tmp_path)
        for _ in range(2):                                                          # two retries: the second base must win
            s.enqueue_command(pid, "retry_node", node_id=nid)
            r1.step()
            s.transition_node_status(nid, "running")
            s.transition_node_status(nid, "failed")
            finished_job(s, pid, nid, tokens_in=100)
            r1.tracker.add(nid, 100, 0.0)                                           # the runner would have seen this job finish
        r2 = make_runner(s, None, tmp_path)
        r2.step()
        u = r2.tracker.usage(nid, 3)
        assert u.tokens == 100 and u.attempts == 0, newest_first
