"""Fixtures for the remote-worker tests: a real store, an in-process API and a fake worker."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from nai.remote.server import create_worker_app
from nai.store import Store


@pytest.fixture
def store(tmp_path: Path):
    with Store(tmp_path / "nai.sqlite3") as instance:
        yield instance


@pytest.fixture
def project(store: Store, tmp_path: Path) -> dict[str, Any]:
    pid = store.create_project(
        "Remote project",
        "A project brief",
        workspace_path=tmp_path / "ws",
        settings={"remote": {"enabled": True}},
    )
    return store.get_project(pid)


@pytest.fixture
def workspace(project: dict[str, Any]) -> Path:
    root = Path(project["workspace_path"])
    root.mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n", encoding="utf-8")
    return root


@pytest.fixture
def worker(store: Store) -> dict[str, Any]:
    token = "token-macbook"
    record = store.add_worker(
        "macbook", Store.hash_worker_token(token), capabilities=["macos", "python"]
    )
    store.touch_worker(record["id"])
    return {**record, "token": token}


@pytest.fixture
def linux_worker(store: Store) -> dict[str, Any]:
    token = "token-linux"
    record = store.add_worker("linuxbox", Store.hash_worker_token(token), capabilities=["linux", "python"])
    store.touch_worker(record["id"])
    return {**record, "token": token}


@pytest.fixture
def snapshots(tmp_path: Path) -> Path:
    path = tmp_path / "snapshots"
    path.mkdir()
    return path


@pytest.fixture
def api(store: Store, snapshots: Path):
    # The API serves requests from a thread pool: its own connection, serialized.
    server_store = Store(store.db_path, allow_thread_sharing=True)
    app = create_worker_app(server_store, snapshot_dir=snapshots, poll_seconds=0.01, max_wait=0.5)
    try:
        yield TestClient(app)
    finally:
        server_store.close()


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class FakeWorker:
    """Claim and finish tasks in-process, the way ``nai-worker`` does.

    ``handler(task)`` returns the result payload, or ``None`` to simulate a
    worker that never reports (a lost lease).
    """

    def __init__(self, store: Store, worker: dict[str, Any], handler: Callable[[dict], dict | None]):
        self.store = store
        self.worker = worker
        self.handler = handler
        self.stop = threading.Event()
        self.seen: list[str] = []
        self.thread = threading.Thread(target=self._loop, name="fake-worker", daemon=True)

    def _loop(self) -> None:
        # A worker is another process: it opens its own connection.
        with Store(self.store.db_path) as store:
            worker = store.get_worker(self.worker["id"])
            while not self.stop.is_set():
                task = store.claim_remote_task(
                    worker["id"], worker["capabilities"], lease_seconds=60
                )
                if task is None:
                    time.sleep(0.01)
                    continue
                self.seen.append(task["id"])
                result = self.handler(task)
                if result is not None:
                    store.finish_remote_task(task["id"], worker["id"], result)
                time.sleep(0.01)

    def __enter__(self) -> FakeWorker:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop.set()
        self.thread.join(timeout=5)


@pytest.fixture
def fake_worker():
    return FakeWorker


class LockedStore:
    """A thread-shareable store behind a lock, like the runner's ``StoreProxy``."""

    def __init__(self, store: Store):
        self._store = store
        self._lock = threading.Lock()

    def __getattr__(self, name: str):
        attr = getattr(self._store, name)
        if not callable(attr):
            return attr

        def call(*args, **kwargs):
            with self._lock:
                return attr(*args, **kwargs)

        return call


@pytest.fixture
def thread_store(store: Store):
    """The same database, safe to use from a background thread (as handlers do)."""
    with Store(store.db_path, allow_thread_sharing=True) as raw:
        yield LockedStore(raw)
