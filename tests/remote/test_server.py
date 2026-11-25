"""Worker API: auth, long-poll claim, validation, snapshots and results."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from nai.remote.server import check_bind, is_loopback
from nai.store import Store
from tests.remote.conftest import auth


def test_health_needs_no_token(api) -> None:
    response = api.get("/worker/v1/health")
    assert response.status_code == 200 and response.json()["ok"] is True


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer nope"}, {"Authorization": "Basic x"}])
def test_endpoints_require_a_valid_token(api, headers) -> None:
    assert api.post("/worker/v1/heartbeat", json={}, headers=headers).status_code == 401
    assert api.post("/worker/v1/claim", json={}, headers=headers).status_code == 401
    assert api.post("/worker/v1/result", json={"task_id": "x", "exit_code": 0}, headers=headers).status_code == 401
    assert api.get(f"/worker/v1/snapshot/{'a' * 64}", headers=headers).status_code == 401


def test_revoked_token_is_rejected(api, store, worker) -> None:
    assert api.post("/worker/v1/heartbeat", json={}, headers=auth(worker["token"])).status_code == 200
    store.revoke_worker(worker["id"])
    assert api.post("/worker/v1/heartbeat", json={}, headers=auth(worker["token"])).status_code == 401


def test_heartbeat_records_liveness_and_capabilities(api, store, worker) -> None:
    response = api.post(
        "/worker/v1/heartbeat",
        json={"capabilities": ["macos", "python", "xcode"], "toolchains": {"python": "3.13.0"}},
        headers=auth(worker["token"]),
    )
    assert response.status_code == 200
    stored = store.get_worker(worker["id"])
    assert stored["capabilities"] == ["macos", "python", "xcode"]
    assert stored["toolchains"] == {"python": "3.13.0"}
    assert stored["last_seen"]


def test_claim_is_empty_without_work(api, worker) -> None:
    response = api.post("/worker/v1/claim", json={"wait": 0}, headers=auth(worker["token"]))
    assert response.status_code == 204


def test_claim_returns_a_task_with_its_snapshot(api, store, project, worker) -> None:
    task = store.create_remote_task(
        project["id"], "echo", "d" * 64,
        params={"params": {"message": "hello"}, "cache_dirs": [".venv"]}, required_caps=["macos"],
    )
    response = api.post("/worker/v1/claim", json={"wait": 0}, headers=auth(worker["token"]))
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == task["id"] and body["recipe"] == "echo"
    assert body["params"]["params"]["message"] == "hello"
    assert body["required_caps"] == ["macos"]


def test_claim_skips_a_task_whose_params_are_invalid(api, store, project, worker) -> None:
    bad = store.create_remote_task(
        project["id"], "verify", "e" * 64,
        params={"params": {"targets": ["../../etc/passwd"]}}, required_caps=["macos"],
    )
    response = api.post("/worker/v1/claim", json={"wait": 0}, headers=auth(worker["token"]))
    assert response.status_code == 204
    failed = store.get_remote_task(bad["id"])
    assert failed["status"] == "done"
    assert failed["result"]["infrastructure"] is True
    assert "infrastructure:" in failed["result"]["error"]


def test_claim_rejects_an_unknown_recipe(api, store, project, worker) -> None:
    bad = store.create_remote_task(project["id"], "definitely-not-a-recipe", "f" * 64,
                                   required_caps=["macos"])
    assert api.post("/worker/v1/claim", json={"wait": 0}, headers=auth(worker["token"])).status_code == 204
    assert store.get_remote_task(bad["id"])["result"]["infrastructure"] is True


def test_capability_mismatch_never_claims(api, store, project, linux_worker) -> None:
    _echo(store, project, "a")
    response = api.post("/worker/v1/claim", json={"wait": 0}, headers=auth(linux_worker["token"]))
    assert response.status_code == 204


ECHO_PARAMS = {"params": {"message": "hello"}}


def _echo(store, project, sha_char: str, **kwargs):
    """Queue a valid ``echo`` task (its ``message`` parameter is required)."""
    return store.create_remote_task(
        project["id"], "echo", sha_char * 64, params=dict(ECHO_PARAMS), required_caps=["macos"], **kwargs
    )


def test_snapshot_download(api, snapshots: Path, worker) -> None:
    sha = "b" * 64
    payload = b"tar-bytes"
    (snapshots / f"{sha}.tar.gz").write_bytes(payload)
    response = api.get(f"/worker/v1/snapshot/{sha}", headers=auth(worker["token"]))
    assert response.status_code == 200 and response.content == payload
    assert response.headers["content-type"] == "application/gzip"
    assert api.get(f"/worker/v1/snapshot/{'c' * 64}", headers=auth(worker["token"])).status_code == 404
    assert api.get("/worker/v1/snapshot/not-a-sha", headers=auth(worker["token"])).status_code == 400


def test_result_round_trip_and_ownership(api, store, project, worker) -> None:
    task = _echo(store, project, "1")
    api.post("/worker/v1/claim", json={"wait": 0}, headers=auth(worker["token"]))
    body = {
        "task_id": task["id"], "exit_code": 2, "output": "boom", "duration": 1.5,
        "timed_out": False, "toolchains": {"python": "3.13.0"}, "error": "",
    }
    assert api.post("/worker/v1/result", json=body, headers=auth(worker["token"])).status_code == 200
    done = store.get_remote_task(task["id"])
    assert done["status"] == "done"
    assert done["result"]["exit_code"] == 2 and done["result"]["output"] == "boom"
    assert done["result"]["toolchains"] == {"python": "3.13.0"}
    # A second report for the same task is a conflict, not a silent overwrite.
    assert api.post("/worker/v1/result", json=body, headers=auth(worker["token"])).status_code == 409


def test_result_for_another_workers_task_is_rejected(api, store, project, worker, linux_worker) -> None:
    task = _echo(store, project, "2")
    api.post("/worker/v1/claim", json={"wait": 0}, headers=auth(worker["token"]))
    response = api.post(
        "/worker/v1/result",
        json={"task_id": task["id"], "exit_code": 0},
        headers=auth(linux_worker["token"]),
    )
    assert response.status_code == 409
    assert store.get_remote_task(task["id"])["status"] == "claimed"


def test_heartbeat_carries_the_cancel_flag(api, store, project, worker) -> None:
    task = _echo(store, project, "3")
    api.post("/worker/v1/claim", json={"wait": 0}, headers=auth(worker["token"]))
    beat = api.post("/worker/v1/heartbeat", json={"task_id": task["id"]}, headers=auth(worker["token"]))
    assert beat.json()["cancel"] is False
    store.cancel_remote_task(task["id"], reason="owner")
    beat = api.post("/worker/v1/heartbeat", json={"task_id": task["id"]}, headers=auth(worker["token"]))
    assert beat.json()["cancel"] is True


def test_claim_long_polls_until_work_arrives(api, store, project, worker) -> None:
    def enqueue() -> None:
        time.sleep(0.2)
        # A separate connection, like another process enqueueing work.
        with Store(store.db_path) as other:
            other.create_remote_task(project["id"], "echo", "4" * 64,
                                     params={"params": {"message": "x"}}, required_caps=["macos"])

    thread = threading.Thread(target=enqueue)
    thread.start()
    response = api.post("/worker/v1/claim", json={"wait": 2}, headers=auth(worker["token"]))
    thread.join(timeout=5)
    assert response.status_code == 200
    assert json.loads(response.content)["recipe"] == "echo"


def test_result_can_report_an_infrastructure_failure(api, store, project, worker) -> None:
    task = _echo(store, project, "5")
    api.post("/worker/v1/claim", json={"wait": 0}, headers=auth(worker["token"]))
    response = api.post(
        "/worker/v1/result",
        json={"task_id": task["id"], "exit_code": 1, "error": "snapshot download failed",
              "infrastructure": True},
        headers=auth(worker["token"]),
    )
    assert response.status_code == 200 and response.json()["infrastructure"] is True
    failed = store.get_remote_task(task["id"])
    assert failed["status"] == "failed"
    assert failed["error"] == "infrastructure: snapshot download failed"


def test_bind_guard_requires_tls_or_an_explicit_lan_flag() -> None:
    assert is_loopback("127.0.0.1") and is_loopback("::1") and is_loopback("localhost")
    assert not is_loopback("192.168.1.20")
    check_bind("127.0.0.1")
    check_bind("192.168.1.20", certfile="c.pem", keyfile="k.pem")
    check_bind("192.168.1.20", insecure_lan=True)
    with pytest.raises(ValueError, match="refusing to bind"):
        check_bind("0.0.0.0")

