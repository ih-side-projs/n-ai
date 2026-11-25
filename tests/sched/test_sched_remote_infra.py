"""Remote waiting/infrastructure failures requeue without burning an attempt."""

from __future__ import annotations

from sched_helpers import make_node, make_runner

from nai.sched.registry import JobResult


class WorkerUnavailable(Exception):
    """Mirrors nai.remote.errors.WorkerUnavailable (matched by class name)."""


class InfrastructureError(Exception):
    """Mirrors nai.remote.errors.InfrastructureError (matched by class name)."""


def test_waiting_for_a_worker_requeues_without_counting_an_attempt(store, pid, tmp_path):
    node = make_node(store, pid, "leaf", status="ready")
    job = store.create_job(pid, "implement", "coder", node_id=node)
    calls = {"n": 0}

    def waiting(ctx):
        calls["n"] += 1
        if calls["n"] < 3:
            raise WorkerUnavailable("waiting for worker (macos)")
        return JobResult.success()

    r = make_runner(store, None, tmp_path)
    r.registry.register("implement", waiting)
    for _ in range(4):
        r.step()

    assert store.jobs[job]["status"] == "succeeded"
    assert calls["n"] == 3
    # Attempts are counted once at the first start; waiting never adds one.
    assert store.nodes[node]["attempts"] == 1
    assert len(store.events_of("remote.waiting")) == 2
    assert r.stats["jobs_requeued"] == 2


def test_infrastructure_failures_are_bounded_and_named(store, pid, tmp_path):
    node = make_node(store, pid, "leaf", status="ready")
    job = store.create_job(pid, "implement", "coder", node_id=node)

    def broken(ctx):
        raise InfrastructureError("infrastructure: worker lost the lease 3 times")

    r = make_runner(store, None, tmp_path, remote_infra_retries=2)
    r.registry.register("implement", broken)
    for _ in range(6):
        r.step()

    assert store.jobs[job]["status"] == "failed"
    assert store.jobs[job]["error"].startswith("infrastructure:")
    assert store.nodes[node]["attempts"] == 1
    assert len(store.events_of("remote.infrastructure")) == 2
