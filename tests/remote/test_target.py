"""``RemoteTarget``: enqueue, wait, cancel, time out and surface infrastructure errors."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from nai.remote.errors import InfrastructureError, RecipeError, WorkerUnavailable
from nai.remote.target import RemoteTarget


def _ok(**overrides):
    payload = {
        "exit_code": 0, "output": "ok\n", "duration": 0.2, "timed_out": False,
        "toolchains": {"python": "3.13.0"}, "error": "",
    }
    payload.update(overrides)
    return payload


def _target(workspace: Path, store, project, *, recipe="echo", params=None, **kwargs) -> RemoteTarget:
    options = {
        "project_id": project["id"],
        "recipe": recipe,
        "params": {"message": "hi"} if params is None and recipe == "echo" else params,
        "required_caps": ["macos"],
        "snapshot_ignore": (".nai", "__pycache__"),
        "snapshot_dir": workspace / ".nai" / "snapshots",
        "poll_seconds": 0.01,
        "worker_wait_seconds": 5.0,
        "expire_seconds": 1.0,
    }
    options.update(kwargs)
    return RemoteTarget(workspace, store, **options)


def test_target_runs_the_recipe_and_returns_the_result(store, project, workspace, worker, fake_worker) -> None:
    target = _target(workspace, store, project)
    with fake_worker(store, worker, lambda task: _ok()):
        result = target.run(["echo", "hi"], timeout=10)
    assert result.exit_code == 0 and result.output == "ok\n"
    assert result.mode == "remote" and result.network == "worker"
    assert result.ok and "toolchain python 3.13.0" in result.notes
    task = store.list_remote_tasks(project_id=project["id"])[0]
    assert task["status"] == "done" and task["worker_id"] == worker["id"]
    assert (workspace / ".nai" / "snapshots" / f"{task['snapshot_sha']}.tar.gz").is_file()


def test_target_reports_a_failing_command(store, project, workspace, worker, fake_worker) -> None:
    target = _target(workspace, store, project)
    with fake_worker(store, worker, lambda task: _ok(exit_code=3, output="3 failed")):
        result = target.run(["echo", "hi"], timeout=10)
    assert result.exit_code == 3 and "3 failed" in result.output
    assert not result.ok


def test_target_waits_for_a_capable_worker_then_gives_up(store, project, workspace, linux_worker) -> None:
    target = _target(workspace, store, project, worker_wait_seconds=0.05)
    with pytest.raises(WorkerUnavailable, match=r"waiting for worker \(macos\)"):
        target.run(["echo", "hi"], timeout=5)
    # The task is left queued for the next run, not failed.
    assert store.list_remote_tasks(project_id=project["id"])[0]["status"] == "queued"


def test_target_bounds_the_wait_for_a_busy_worker(store, project, workspace, worker) -> None:
    # The worker is online and capable but never claims (busy with other work).
    target = _target(workspace, store, project, worker_wait_seconds=0.05)
    with pytest.raises(WorkerUnavailable, match="to claim echo"):
        target.run(["echo", "hi"], timeout=0.2)


def test_target_cancel_stops_the_wait(store, thread_store, project, workspace, worker, fake_worker) -> None:
    target = _target(workspace, thread_store, project)
    release = threading.Event()

    def hold(task):
        release.wait(5)
        return None  # a cancelled task never reports

    with fake_worker(store, worker, hold):
        box: dict = {}
        thread = threading.Thread(target=lambda: box.setdefault("result", target.run(["echo", "hi"], timeout=30)))
        thread.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not target._task_id:
            time.sleep(0.01)
        assert target.cancel() is True
        thread.join(timeout=10)
        release.set()
    assert not thread.is_alive()
    result = box["result"]
    assert result.cancelled and result.exit_code == 130


def test_target_times_out_and_cancels_the_task(store, project, workspace, worker, fake_worker) -> None:
    target = _target(workspace, store, project)
    release = threading.Event()

    def hold(task):
        release.wait(5)
        return None

    with fake_worker(store, worker, hold):
        result = target.run(["echo", "hi"], timeout=0.2)
        release.set()
    assert result.timed_out and result.exit_code == 124
    assert store.list_remote_tasks(project_id=project["id"])[0]["status"] == "cancelled"


def test_target_raises_on_an_infrastructure_failure(store, project, workspace, monkeypatch) -> None:
    target = _target(workspace, store, project)
    original = store.create_remote_task

    def failing_create(*args, **kwargs):
        task = original(*args, **kwargs)
        store.conn.execute(
            "UPDATE remote_tasks SET status='failed', error=? WHERE id=?",
            ("infrastructure: worker lost the lease 3 times", task["id"]),
        )
        return task

    monkeypatch.setattr(store, "create_remote_task", failing_create)
    with pytest.raises(InfrastructureError, match="infrastructure"):
        target.run(["echo", "hi"], timeout=5)


def test_invalid_params_are_rejected_before_queueing(store, project, workspace) -> None:
    with pytest.raises(RecipeError):
        _target(workspace, store, project, params={"message": "a; rm -rf /"})
    with pytest.raises(RecipeError, match="unknown recipe"):
        _target(workspace, store, project, recipe="nope")


def test_capabilities_and_describe(store, project, workspace) -> None:
    target = _target(workspace, store, project)
    caps = target.capabilities()
    assert caps.mode == "remote" and caps.real_cancel is True
    assert "recipe echo" in target.describe() and "caps=macos" in target.describe()
    target.close()
    target.close()  # idempotent
