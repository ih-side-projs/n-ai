"""End-to-end: a real ``nai-worker`` process claims and runs an ``echo`` recipe."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from nai.remote.server import create_worker_app
from nai.remote.target import RemoteTarget
from nai.store import Store

ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def live_server(store: Store, snapshots: Path):
    server_store = Store(store.db_path, allow_thread_sharing=True)
    app = create_worker_app(server_store, snapshot_dir=snapshots, poll_seconds=0.02, max_wait=5.0)
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started, "worker API did not start"
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        server_store.close()


def _worker_config(tmp_path: Path, server: str, token: str) -> Path:
    path = tmp_path / "worker.toml"
    path.write_text(
        f"""
[worker]
name = "macbook"
server = "{server}"
token = "{token}"
work_dir = "{tmp_path / 'work'}"
capabilities = ["macos", "python"]
python = "{sys.executable}"
heartbeat_seconds = 1
claim_wait = 15

[recipes.echo]
argv = ["echo", "{{message}}"]
""",
        encoding="utf-8",
    )
    return path


def _run_worker(*args: str, tmp_path: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    return subprocess.run([sys.executable, "-m", "nai.remote.worker", *args],
                          cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=60)


def test_worker_doctor_checks_the_handshake(live_server: str, store: Store, worker, tmp_path: Path) -> None:
    config = _worker_config(tmp_path, live_server, worker["token"])
    ok = _run_worker("doctor", "--config", str(config), tmp_path=tmp_path)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "OK: worker is ready" in ok.stdout
    assert "health: HTTP 200" in ok.stdout and "heartbeat: HTTP 200" in ok.stdout

    bad = _worker_config(tmp_path, live_server, "wrong-token")
    failed = _run_worker("doctor", "--config", str(bad), tmp_path=tmp_path)
    assert failed.returncode == 1
    assert "token rejected" in failed.stderr


def test_real_worker_process_runs_an_echo_recipe(
    live_server: str, store: Store, project, workspace: Path, worker, snapshots: Path, tmp_path: Path
) -> None:
    config_path = _worker_config(tmp_path, live_server, worker["token"])

    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    worker_proc = subprocess.Popen(
        [sys.executable, "-m", "nai.remote.worker", "run", "--config", str(config_path), "--once"],
        cwd=str(tmp_path), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        target = RemoteTarget(
            workspace, store,
            project_id=project["id"], recipe="echo", params={"message": "hi-from-mac"},
            required_caps=["macos"], snapshot_dir=snapshots, poll_seconds=0.05,
            worker_wait_seconds=30.0, default_timeout=60.0,
        )
        result = target.run(["echo", "hi-from-mac"], timeout=60)
        target.close()
        out, _ = worker_proc.communicate(timeout=60)
    finally:
        if worker_proc.poll() is None:
            worker_proc.kill()
            worker_proc.communicate()

    assert result.exit_code == 0, result.output
    assert result.output.strip() == "hi-from-mac"
    assert result.mode == "remote"
    assert any("toolchain python" in note for note in result.notes)
    assert worker_proc.returncode == 0
    assert "running echo" in out and "stopped after 1 task(s)" in out

    tasks = store.list_remote_tasks(project_id=project["id"])
    assert tasks[0]["status"] == "done"
    assert tasks[0]["worker_id"] == worker["id"]
    assert tasks[0]["redeliveries"] == 0
    assert tasks[0]["result"]["output"].strip() == "hi-from-mac"
    assert (tmp_path / "work" / "projects").is_dir()
