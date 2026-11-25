"""Store-level tests: schema, tokens, atomic claim, leases and cancellation."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nai.store import Store
from nai.store.errors import StoreError


def _stamp(seconds: float) -> str:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds)
    return base.isoformat(timespec="microseconds").replace("+00:00", "Z")


def test_migration_adds_worker_and_task_tables(store: Store) -> None:
    assert store.schema_version >= 8
    workers = {row["name"] for row in store.conn.execute("PRAGMA table_info(workers)").fetchall()}
    tasks = {row["name"] for row in store.conn.execute("PRAGMA table_info(remote_tasks)").fetchall()}
    assert {"id", "name", "token_hash", "capabilities_json", "toolchains_json", "last_seen"} <= workers
    assert {"id", "project_id", "job_id", "recipe", "params_json", "snapshot_sha", "required_caps",
            "status", "lease_expires_at", "worker_id", "redeliveries", "result_json"} <= tasks


def test_worker_registry_round_trip(store: Store) -> None:
    token = "plaintext-token"
    created = store.add_worker("mac", Store.hash_worker_token(token),
                               capabilities=["macos", "Python"], toolchains={"python": "3.12.4"})
    assert created["capabilities"] == ["macos", "python"]
    assert created["toolchains"] == {"python": "3.12.4"}
    assert created["revoked"] is False
    assert store.get_worker("mac")["id"] == created["id"]
    assert [w["name"] for w in store.list_workers()] == ["mac"]
    assert store.worker_for_token(token)["id"] == created["id"]
    assert store.worker_for_token("wrong") is None
    assert store.revoke_worker(created["id"]) is True
    assert store.get_worker(created["id"])["revoked"] is True
    assert store.worker_for_token(token) is None
    assert store.revoke_worker("nobody") is False


def test_duplicate_worker_name_is_rejected(store: Store) -> None:
    store.add_worker("mac", Store.hash_worker_token("a"))
    with pytest.raises(StoreError):
        store.add_worker("mac", Store.hash_worker_token("b"))


def test_token_is_never_stored_in_plaintext(store: Store) -> None:
    token = "super-secret"
    store.add_worker("mac", Store.hash_worker_token(token))
    stored = store.conn.execute("SELECT token_hash FROM workers").fetchone()["token_hash"]
    assert stored != token
    assert stored == Store.hash_worker_token(token)
    assert len(stored) == 64


def test_capability_mismatch_returns_no_claim(store: Store, project, worker, linux_worker) -> None:
    store.create_remote_task(project["id"], "verify", "a" * 64, required_caps=["macos"])
    assert store.claim_remote_task(linux_worker["id"], ["linux", "python"]) is None
    claimed = store.claim_remote_task(worker["id"], ["macos", "python"])
    assert claimed is not None and claimed["worker_id"] == worker["id"]


def test_claim_is_atomic_across_connections(tmp_path: Path) -> None:
    db = tmp_path / "atomic.sqlite3"
    setup = Store(db)
    try:
        pid = setup.create_project("p", "b", workspace_path=tmp_path / "w")
        setup.create_remote_task(pid, "echo", "b" * 64, params={"params": {"message": "hi"}})
        setup.add_worker("a", Store.hash_worker_token("a"), capabilities=["macos"])
        setup.add_worker("b", Store.hash_worker_token("b"), capabilities=["macos"])
    finally:
        setup.close()

    results: list[dict | None] = []
    barrier = threading.Barrier(2)

    def claim(name: str) -> None:
        # Each thread opens its own connection, like two worker processes would.
        with Store(db) as instance:
            worker = instance.get_worker(name)
            barrier.wait()
            results.append(instance.claim_remote_task(worker["id"], ["macos"]))

    threads = [threading.Thread(target=claim, args=("a",)),
               threading.Thread(target=claim, args=("b",))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    claimed = [r for r in results if r is not None]
    assert len(claimed) == 1, results


def test_expired_lease_requeues_then_fails_as_infrastructure(store: Store, project, worker) -> None:
    task = store.create_remote_task(project["id"], "echo", "c" * 64, required_caps=["macos"])
    assert store.claim_remote_task(worker["id"], ["macos"], lease_seconds=60, now=_stamp(0))
    # Still inside the lease: nothing expires.
    assert store.expire_remote_tasks(now=_stamp(30)) == []
    assert store.get_remote_task(task["id"])["status"] == "claimed"
    # Lease expired once -> requeued, redeliveries counted.
    assert store.expire_remote_tasks(now=_stamp(120)) == [task["id"]]
    requeued = store.get_remote_task(task["id"])
    assert requeued["status"] == "queued" and requeued["redeliveries"] == 1
    assert requeued["worker_id"] is None
    # Two more losses reach the third redelivery: infrastructure failure.
    assert store.claim_remote_task(worker["id"], ["macos"], now=_stamp(130))["id"] == task["id"]
    store.expire_remote_tasks(now=_stamp(200))
    assert store.claim_remote_task(worker["id"], ["macos"], now=_stamp(210))["id"] == task["id"]
    store.expire_remote_tasks(now=_stamp(300))
    failed = store.get_remote_task(task["id"])
    assert failed["status"] == "failed"
    assert failed["redeliveries"] == 3
    assert failed["result"]["infrastructure"] is True
    assert failed["error"].startswith("infrastructure:")


def test_heartbeat_extends_the_lease_and_reports_cancel(store: Store, project, worker) -> None:
    task = store.create_remote_task(project["id"], "echo", "d" * 64, required_caps=["macos"])
    store.claim_remote_task(worker["id"], ["macos"], lease_seconds=60, now=_stamp(0))
    beat = store.heartbeat_remote_task(task["id"], worker["id"], lease_seconds=60, now=_stamp(30))
    assert beat is not None and beat["cancel"] is False
    assert beat["lease_expires_at"] > _stamp(60)
    assert store.heartbeat_remote_task(task["id"], "someone-else", now=_stamp(35)) is None
    assert store.cancel_remote_task(task["id"]) is True
    beat = store.heartbeat_remote_task(task["id"], worker["id"], now=_stamp(40))
    assert beat["cancel"] is True and beat["status"] == "cancelled"


def test_finish_requires_the_owning_worker(store: Store, project, worker) -> None:
    task = store.create_remote_task(project["id"], "echo", "e" * 64, required_caps=["macos"])
    store.claim_remote_task(worker["id"], ["macos"])
    assert store.finish_remote_task(task["id"], "intruder", {"exit_code": 0}) is False
    assert store.finish_remote_task(task["id"], worker["id"], {"exit_code": 0, "output": "ok"}) is True
    done = store.get_remote_task(task["id"])
    assert done["status"] == "done" and done["result"]["output"] == "ok"
    assert store.finish_remote_task(task["id"], worker["id"], {"exit_code": 1}) is False


def test_cancelled_task_ignores_a_late_result(store: Store, project, worker) -> None:
    task = store.create_remote_task(project["id"], "echo", "f" * 64, required_caps=["macos"])
    store.claim_remote_task(worker["id"], ["macos"])
    store.cancel_remote_task(task["id"], reason="owner")
    assert store.finish_remote_task(task["id"], worker["id"], {"exit_code": 0}) is False
    assert store.get_remote_task(task["id"])["status"] == "cancelled"


def test_fail_remote_task_marks_infrastructure(store: Store, project, worker) -> None:
    task = store.create_remote_task(project["id"], "echo", "9" * 64,
                                    params={"params": {"message": "x"}}, required_caps=["macos"])
    store.claim_remote_task(worker["id"], ["macos"])
    assert store.fail_remote_task(task["id"], "intruder", "nope") is False
    assert store.fail_remote_task(task["id"], worker["id"], "snapshot 404") is True
    failed = store.get_remote_task(task["id"])
    assert failed["status"] == "failed"
    assert failed["error"] == "infrastructure: snapshot 404"
    assert failed["result"]["infrastructure"] is True


def test_online_workers_respect_the_lease_window(store: Store, worker) -> None:
    store.touch_worker(worker["id"], now=_stamp(0))
    assert [w["name"] for w in store.online_workers(now=_stamp(10))] == ["macbook"]
    assert store.online_workers(now=_stamp(10_000)) == []
    assert store.workers_with_caps(["macos"], now=_stamp(10_000)) == []

