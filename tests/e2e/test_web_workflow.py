"""Pass 9: full web workflow against the real store and command processor.

The browser-facing routes write commands to the same SQLite store the runner
reads. These tests drive the FastAPI app with ASGI, then let a real
CommandProcessor consume the queued commands, so the STO-3 boundary is covered
end to end rather than with store fakes.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from nai.sched import Runner, SchedConfig
from nai.store import Store
from nai.web import create_app


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    db = tmp_path / ".nai" / "nai.sqlite3"
    db.parent.mkdir(parents=True, exist_ok=True)
    Store(db, read_only=False).close()
    return db


@pytest.fixture
def app(db_path: Path, monkeypatch):
    monkeypatch.setenv("NAI_DB_PATH", str(db_path))
    monkeypatch.delenv("NAI_WEB_TOKEN", raising=False)
    return create_app(db_path)


@pytest.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        yield c


async def _csrf(client: AsyncClient) -> str:
    response = await client.get("/")
    assert response.status_code == 200
    token = response.cookies.get("nai_csrf")
    assert token
    return token


async def _create_project(client: AsyncClient, name: str = "Web E2E") -> tuple[str, str]:
    token = await _csrf(client)
    response = await client.post("/projects/create", data={
        "csrf_token": token, "name": name, "brief": "Build a tiny verified thing",
    }, follow_redirects=False)
    assert response.status_code == 303, response.text
    pid = response.headers["location"].rsplit("/", 1)[-1]
    return pid, token


def _process_commands(db_path: Path, tmp_path: Path) -> None:
    state_dir = tmp_path / "runner-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    store = Store(db_path, read_only=False)
    runner = Runner(store, None, sched=SchedConfig(poll_seconds=0.01), state_dir=state_dir)
    try:
        runner._process_commands()
    finally:
        runner.close()
        store.close()


def _snapshot(db_path: Path, pid: str) -> dict:
    store = Store(db_path, read_only=True)
    try:
        project = store.get_project(pid)
        nodes = {}
        for node in store.list_nodes(pid, parent_id=None):
            nodes[node["id"]] = node
            for child in store.list_nodes(pid, parent_id=node["id"]):
                nodes[child["id"]] = child
        return {
            "project": project,
            "nodes": nodes,
            "approvals": {nid: store.list_approvals(nid) for nid in nodes},
            "events": store.list_events(pid, limit=10000),
            "jobs": store.list_jobs(pid, limit=10000),
            "commands": store.conn.execute(
                "SELECT * FROM commands WHERE project_id=? ORDER BY created_at, id", (pid,)
            ).fetchall(),
        }
    finally:
        store.close()


async def test_web_command_queue_full_control_flow(client: AsyncClient, db_path: Path, tmp_path: Path):
    pid, token = await _create_project(client)
    with Store(db_path, read_only=False) as store:
        root = store.list_nodes(pid, parent_id=None)[0]["id"]

    # Design edit is a direct, safe write; approvals are not.
    response = await client.post(f"/projects/{pid}/nodes/{root}/edit", data={
        "csrf_token": token, "spec_text": "# Root\n\nEdited from the web workflow.\n",
    }, follow_redirects=False)
    assert response.status_code == 303
    snap = _snapshot(db_path, pid)
    assert "Edited from the web workflow" in snap["nodes"][root]["spec_text"]
    with Store(db_path, read_only=False) as store:
        store.transition_node_status(root, "draft", note="reopened after edit")
        store.transition_node_status(root, "awaiting_approval", note="ready for review")

    response = await client.post(f"/projects/{pid}/nodes/{root}/approve", data={
        "csrf_token": token, "stage": "interfaces", "note": "looks good",
    }, follow_redirects=False)
    assert response.status_code == 303
    snap = _snapshot(db_path, pid)
    assert any(dict(c)["action"] == "approve" and dict(c)["status"] == "queued" for c in snap["commands"])
    assert snap["approvals"][root] == [], "web must not write approvals directly"

    _process_commands(db_path, tmp_path)
    snap = _snapshot(db_path, pid)
    assert snap["nodes"][root]["status"] == "approved"
    assert snap["approvals"][root][0]["stage"] == "interfaces"
    assert snap["approvals"][root][0]["decision"] == "approved"
    assert not [c for c in snap["commands"] if dict(c)["error"]]

    # Reject walks the legal awaiting_approval -> stale -> draft path.
    with Store(db_path, read_only=False) as store:
        child = store.create_node(pid, "child", "component", parent_id=root, objective="Child")
        store.transition_node_status(child, "awaiting_approval")
    response = await client.post(f"/projects/{pid}/nodes/{child}/reject", data={
        "csrf_token": token, "stage": "component-spec", "note": "rewrite this",
    }, follow_redirects=False)
    assert response.status_code == 303
    _process_commands(db_path, tmp_path)
    snap = _snapshot(db_path, pid)
    assert snap["nodes"][child]["status"] == "draft"
    assert snap["approvals"][child][0]["decision"] == "rejected"

    # Retry resets budget through CommandProcessor and does not add an attempt.
    with Store(db_path, read_only=False) as store:
        broken = store.create_node(pid, "broken", "component", parent_id=root, objective="Broken")
        for status in ("awaiting_approval", "approved", "ready", "running", "failed"):
            store.transition_node_status(broken, status)
        store.increment_node_attempts(broken, 2)
    attempts_before = _snapshot(db_path, pid)["nodes"][broken]["attempts"]
    response = await client.post(f"/projects/{pid}/nodes/{broken}/retry", data={"csrf_token": token},
                                 follow_redirects=False)
    assert response.status_code == 303
    _process_commands(db_path, tmp_path)
    snap = _snapshot(db_path, pid)
    assert snap["nodes"][broken]["status"] == "ready"
    assert snap["nodes"][broken]["attempts"] == attempts_before
    assert any(e["type"] == "node.retry" and e.get("node_id") == broken for e in snap["events"])


    # Re-run a stage enqueues a real job with the requested stage marker.
    response = await client.post(f"/projects/{pid}/nodes/{root}/rerun_stage", data={
        "csrf_token": token, "stage": "verify",
    }, follow_redirects=False)
    assert response.status_code == 303
    _process_commands(db_path, tmp_path)
    jobs = _snapshot(db_path, pid)["jobs"]
    assert any(j["kind"] == "verify" and j.get("input_ref", {}).get("stage") == "verify" for j in jobs)

    # Priority order persists in project settings; no job priority column exists yet.
    response = await client.post(f"/projects/{pid}/control/reorder", data={
        "csrf_token": token, "node_order": json.dumps([child, root]),
    })
    assert response.status_code == 200
    _process_commands(db_path, tmp_path)
    settings = _snapshot(db_path, pid)["project"]["settings"]
    assert settings["node_order"] == [child, root]

    # Pause/resume and cancel are also consumed by the same CommandProcessor.
    for action in ("pause", "resume"):
        response = await client.post(f"/projects/{pid}/control/{action}", data={"csrf_token": token})
        assert response.status_code == 200
        _process_commands(db_path, tmp_path)
    snap = _snapshot(db_path, pid)
    kinds = {e["type"] for e in snap["events"]}
    assert {"project.paused", "project.resumed"} <= kinds

    with Store(db_path, read_only=False) as store:
        job_id = store.create_job(pid, "verify", "coder", node_id=root)
    response = await client.post(f"/projects/{pid}/control/cancel", data={
        "csrf_token": token, "job_id": job_id,
    })
    assert response.status_code == 200
    _process_commands(db_path, tmp_path)
    job = next(j for j in _snapshot(db_path, pid)["jobs"] if j["id"] == job_id)
    assert job["status"] == "cancelled"

    # Phase views remain available throughout the mixed design/implementation state.
    response = await client.get(f"/api/projects/{pid}/phase")
    assert response.status_code == 200
    assert response.json()["phase"] in {"design", "implementation", "mixed"}




async def test_web_project_lifecycle_cancel_archive_and_filter(client: AsyncClient, db_path: Path, tmp_path: Path):
    """Cancel leaves Active; archived projects hide until the owner asks for them."""
    pid, token = await _create_project(client, "Visible Project")

    with Store(db_path, read_only=False) as store:
        hidden = store.create_project("Hidden Test", "old test project", settings={})
        store.create_node(hidden, "root", "system")
        store.set_project_status(hidden, "archived")

    response = await client.get("/projects")
    assert response.status_code == 200
    assert "Visible Project" in response.text
    assert "Hidden Test" not in response.text
    assert "Show archived (1)" in response.text

    response = await client.get("/projects?show_archived=1")
    assert "Hidden Test" in response.text
    assert "Hide archived" in response.text

    # Archive enqueues a command; the project status changes when the runner processes it.
    response = await client.post(f"/projects/{pid}/archive", data={
        "csrf_token": token,
    }, follow_redirects=False)
    assert response.status_code == 303
    assert _snapshot(db_path, pid)["project"]["status"] == "active"
    _process_commands(db_path, tmp_path)
    assert _snapshot(db_path, pid)["project"]["status"] == "archived"

    response = await client.get("/projects")
    assert "Visible Project" not in response.text

    response = await client.post(f"/projects/{pid}/unarchive", data={
        "csrf_token": token,
    }, follow_redirects=False)
    assert response.status_code == 303
    _process_commands(db_path, tmp_path)
    assert _snapshot(db_path, pid)["project"]["status"] == "active"

    # Project-wide cancel must not leave the card showing Active.
    response = await client.post(f"/projects/{pid}/control/cancel", data={
        "csrf_token": token,
    })
    assert response.status_code == 200
    _process_commands(db_path, tmp_path)
    snap = _snapshot(db_path, pid)
    assert snap["project"]["status"] == "cancelled"
    assert any(e["type"] == "project.cancelled" for e in snap["events"])

    response = await client.get("/projects")
    assert "status-cancelled" in response.text

    # Reopen restores a cancelled project to active via the same command.
    response = await client.post(f"/projects/{pid}/unarchive", data={
        "csrf_token": token,
    }, follow_redirects=False)
    assert response.status_code == 303
    _process_commands(db_path, tmp_path)
    assert _snapshot(db_path, pid)["project"]["status"] == "active"


async def test_web_pages_json_api_and_tool_execution(client: AsyncClient, db_path: Path, tmp_path: Path):
    """Every major screen renders, its JSON mirror responds, and tools execute in a project workspace."""
    pid, token = await _create_project(client, "Web Pages E2E")
    with Store(db_path, read_only=False) as store:
        root = store.list_nodes(pid, parent_id=None)[0]["id"]
        workspace = Path(store.get_project(pid)["workspace_path"])
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "sample.json").write_text('{"ok": true}', encoding="utf-8")
        store.add_approval(root, "component-spec", "approved", note="fixture")

    html_paths = [
        "/", "/projects", f"/projects/{pid}", f"/projects/{pid}/nodes/{root}",
        f"/projects/{pid}/nodes/{root}/edit",
        f"/projects/{pid}/logs", f"/projects/{pid}/metrics", f"/projects/{pid}/settings",
        f"/projects/{pid}/artifacts", "/approvals", "/escalations", "/tools", "/models", "/service",
    ]
    for path in html_paths:
        response = await client.get(path)
        assert response.status_code == 200, (path, response.status_code, response.text[:200])

    # Control forms must be AJAX-handled; a raw form POST would render the JSON
    # response as a bare browser page.
    listing = await client.get("/")
    detail = await client.get(f"/projects/{pid}")
    node_page = await client.get(f"/projects/{pid}/nodes/{root}")
    service_page = await client.get("/service")
    assert "control-form" in listing.text and "data-confirm" in listing.text
    assert "control-form" in detail.text and "data-confirm" in detail.text
    assert "confirm-form" in node_page.text  # mark stale confirmation
    # The stop-service control only renders while the supervisor is running.
    if "Stop service" in service_page.text:
        assert "confirm-form" in service_page.text
    for rendered in (listing.text, detail.text, node_page.text, service_page.text):
        assert "onsubmit=" not in rendered

    api_paths = [
        "/api/projects", f"/api/projects/{pid}", f"/api/projects/{pid}/phase",
        f"/api/projects/{pid}/nodes/{root}", f"/api/projects/{pid}/logs",
        f"/api/projects/{pid}/metrics", f"/api/projects/{pid}/artifacts",
        f"/api/projects/{pid}/files", "/api/models", "/api/runner",
        "/api/approvals", "/api/escalations", "/api/tools", "/api/service",
    ]
    for path in api_paths:
        response = await client.get(path)
        assert response.status_code == 200, (path, response.status_code, response.text[:200])
        assert isinstance(response.json(), dict), path

    response = await client.post("/tools/call", data={
        "csrf_token": token, "tool_name": "read_json",
        "arguments": json.dumps({"path": "sample.json"}), "workspace": str(workspace),
    })
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["isError"] is False, (payload["workspace"], _snapshot(db_path, pid)["project"]["workspace_path"])
    assert '"ok": true' in payload["result"]

    response = await client.post("/tools/call", data={
        "csrf_token": token, "tool_name": "read_json",
        "arguments": json.dumps({"path": "sample.json"}), "workspace": str(tmp_path),
    })
    assert response.status_code == 200
    assert response.json()["isError"] is True

