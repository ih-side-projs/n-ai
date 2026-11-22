import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nai.store import Store
from nai.web.app import _list_project_files, _safe_workspace_path, create_app, render_markdown
from nai.web.models import ModelServerClient, _extract_running_models


@pytest.fixture
def web_db(tmp_path: Path):
    db_path = tmp_path / ".nai" / "nai.sqlite3"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with Store(db_path) as store:
        store.save_model_profile("local-qwen", {"model_id": "qwen-reasoner", "ctx": 32768, "max_tokens": 2048},
                                 {"model_id": "qwen-coder", "ctx": 32768, "max_tokens": 2048})
        project_id = store.create_project(
            "Dashboard sample", "A local-only project brief", workspace_path=workspace,
            settings={"model_profile": "local-qwen", "budget": {"max_tokens": 50000}},
        )
        root_id = store.create_node(project_id, "sample-app", "system", objective="A tiny application", spec_text="# Sample\n\nA **working** sample.")
        store.replace_requirements(root_id, [{"id": "R1", "text": "Returns a value", "testable": True}])
        store.upsert_test_record(root_id, "tests/test_sample.py", ["R1"], "deadbeef", last_result="pass")
        job_id = store.create_job(project_id, "implement", "coder", node_id=root_id, attempt=2)
        store.update_job(job_id, status="running", started_at="2026-11-16T12:00:00+00:00", tokens_in=120, tokens_out=35)
        store.record_event(project_id, "job.started", "Implementation started", node_id=root_id, job_id=job_id)
        store.add_artifact(project_id, "report", "# Build report\n\nAll good.", node_id=root_id, filename="report.md")
    return db_path, project_id, root_id, job_id, workspace


def test_web_dashboard_and_json_project_views_are_read_only(web_db):
    db, project_id, root_id, job_id, workspace = web_db
    client = TestClient(create_app(db))

    response = client.get("/")
    assert response.status_code == 200
    assert "Dashboard sample" in response.text
    assert "New project" in response.text

    api = client.get("/api/projects")
    assert api.status_code == 200
    project = api.json()["projects"][0]
    assert project["node_count"] == 1
    assert project["node_counts"]["draft"] == 1
    assert project["percent_done"] == 0.0

    detail = client.get(f"/api/projects/{project_id}").json()
    assert detail["tree"][0]["name"] == "sample-app"
    assert detail["runner"]["status"] == "not-reported"

    node = client.get(f"/api/projects/{project_id}/nodes/{root_id}")
    assert node.status_code == 200
    node_data = node.json()
    assert node_data["node"]["id"] == root_id
    assert node_data["traceability"][0]["requirement"]["id"] == "R1"
    assert node_data["traceability"][0]["tests"][0]["last_result"] == "pass"

    with Store(db, read_only=True) as store:
        assert store.get_project(project_id)["name"] == "Dashboard sample"
        assert store.get_node(root_id)["status"] == "draft"


def test_project_pages_logs_metrics_and_artifacts(web_db):
    db, project_id, root_id, job_id, workspace = web_db
    client = TestClient(create_app(db))

    project_page = client.get(f"/projects/{project_id}")
    assert project_page.status_code == 200
    assert "Specification tree" in project_page.text
    assert "Runner" in project_page.text

    node_page = client.get(f"/projects/{project_id}/nodes/{root_id}")
    assert node_page.status_code == 200
    assert "Traceability" in node_page.text
    assert "Returns a value" in node_page.text

    logs = client.get(f"/api/projects/{project_id}/logs?job_id={job_id}")
    assert logs.status_code == 200
    assert {event["type"] for event in logs.json()["events"]} >= {"job.queued", "job.started"}
    assert client.get(f"/projects/{project_id}/logs?job_id=not-a-job").status_code == 404

    metrics = client.get(f"/api/projects/{project_id}/metrics").json()
    assert metrics["tokens_in"] == 120
    assert metrics["tokens_out"] == 35
    assert metrics["retries"] == 1
    assert metrics["by_node_kind"]["system"]["retries"] == 1
    assert metrics["by_model"]["qwen-coder"]["tokens_in"] == 120
    assert metrics["model_swaps"]["available"] is False

    artifacts = client.get(f"/api/projects/{project_id}/artifacts").json()["artifacts"]
    assert len(artifacts) == 1
    artifact_id = artifacts[0]["id"]
    view = client.get(f"/projects/{project_id}/artifacts/{artifact_id}")
    assert view.status_code == 200
    assert "Build report" in view.text
    download = client.get(f"/projects/{project_id}/artifacts/{artifact_id}?download=true")
    assert download.status_code == 200
    assert download.content.startswith(b"# Build report")


def test_workspace_file_browser_and_traversal_protection(web_db):
    db, project_id, root_id, job_id, workspace = web_db
    (workspace / "README.md").write_text("# Workspace\n\nRead me.", encoding="utf-8")
    (workspace / ".env").write_text("SECRET=not-for-the-browser", encoding="utf-8")
    client = TestClient(create_app(db))

    listed = client.get(f"/api/projects/{project_id}/files").json()["files"]
    assert [entry["path"] for entry in listed] == ["README.md"]
    viewed = client.get(f"/projects/{project_id}/files/README.md")
    assert viewed.status_code == 200
    assert "Read me." in viewed.text
    assert client.get(f"/projects/{project_id}/files/.env").status_code == 404
    assert client.get(f"/projects/{project_id}/files/..%2F.secret").status_code in {400, 404}


def test_csrf_required_for_post_and_valid_token_is_accepted(web_db):
    db, project_id, root_id, job_id, workspace = web_db
    client = TestClient(create_app(db))
    first = client.get("/models")
    assert first.status_code == 200
    token = client.cookies.get("nai_csrf")
    assert token
    denied = client.post("/models/smoke-test", data={"model_id": "m"})
    assert denied.status_code == 403
    allowed = client.post("/models/smoke-test", data={"csrf_token": token, "model_id": "m"})
    # This is rejected as an unavailable/unlisted model, proving CSRF passed.
    assert allowed.status_code == 400
    assert "CSRF" not in allowed.text


def test_optional_bearer_token_authenticates_lan_requests(web_db):
    db, project_id, root_id, job_id, workspace = web_db
    client = TestClient(create_app(db, bearer_token="test-local-secret"))
    assert client.get("/").status_code == 200  # redirects to /auth then renders auth form
    auth_page = client.get("/auth")
    assert auth_page.status_code == 200
    token = client.cookies.get("nai_csrf")
    auth = client.post("/auth", data={"csrf_token": token, "token": "test-local-secret"}, follow_redirects=False)
    assert auth.status_code == 303
    assert client.get("/").status_code == 200


def test_markdown_renderer_escapes_raw_html_and_javascript_links():
    html = render_markdown("# Hello\n\n<script>alert(1)</script>\n\n[bad](javascript:alert(2))")
    assert "<script" not in html
    assert "javascript:" not in html
    assert "<h1>Hello</h1>" in html


def test_project_files_skips_hidden_paths_and_safe_path_rejects_symlinks(web_db, tmp_path):
    db, project_id, root_id, job_id, workspace = web_db
    (workspace / "visible.txt").write_text("visible")
    (workspace / ".private").mkdir()
    (workspace / ".private" / "secret.txt").write_text("secret")
    assert [f["path"] for f in _list_project_files(workspace)] == ["visible.txt"]
    with pytest.raises(Exception):
        _safe_workspace_path(workspace, "../outside", allow_hidden=False)


def test_model_server_running_payload_shapes():
    assert _extract_running_models(["reasoner", "coder"])[0]["id"] == "reasoner"
    assert _extract_running_models({"running": [{"model_id": "reasoner", "status": "ready"}]}) == [
        {"id": "reasoner", "status": "ready"}
    ]
    assert _extract_running_models({"data": [{"name": "coder"}]})[0]["id"] == "coder"


def test_model_server_unconfigured_is_nonfatal():
    snapshot = ModelServerClient(None).snapshot()
    assert snapshot["configured"] is False
    assert snapshot["healthy"] is None
    assert snapshot["models"] == []


def test_missing_database_returns_actionable_503(tmp_path):
    client = TestClient(create_app(tmp_path / "missing.sqlite3"))
    response = client.get("/api/projects")
    assert response.status_code == 503
    assert "database not found" in response.json()["detail"]


def test_model_page_uses_local_server_models_running_and_smoke_test(web_db):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def _send(self, payload, content_type="application/json"):
            body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/health":
                self._send(b"OK", "text/plain")
            elif self.path == "/v1/models":
                self._send({"data": [{"id": "model-test", "owned_by": "local"}]})
            elif self.path == "/running":
                self._send({"running": ["model-test"]})
            else:
                self.send_error(404)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            if self.path == "/v1/chat/completions" and payload.get("model") == "model-test":
                self._send({"choices": [{"message": {"content": "nai-ok"}}],
                            "usage": {"prompt_tokens": 4, "completion_tokens": 2}})
            else:
                self.send_error(404)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        db, project_id, root_id, job_id, workspace = web_db
        with Store(db) as store:
            store.save_model_profile("local-qwen", {"model_id": "model-test", "ctx": 32768, "max_tokens": 2048},
                                     {"model_id": "model-test-coder", "ctx": 32768, "max_tokens": 2048})
        base_url = f"http://127.0.0.1:{server.server_port}"
        client = TestClient(create_app(db, llama_swap_url=base_url))
        page = client.get("/models")
        assert page.status_code == 200
        assert "model-test" in page.text
        snapshot = client.get("/api/models").json()["server"]
        assert snapshot["healthy"] is True
        assert snapshot["loaded_models"][0]["id"] == "model-test"
        token = client.cookies.get("nai_csrf")
        result = client.post("/models/smoke-test", data={"csrf_token": token, "model_id": "model-test"})
        assert result.status_code == 200
        assert "Smoke test passed" in result.text
        assert "nai-ok" in result.text
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_runner_status_file_is_exposed_in_json(web_db, tmp_path):
    db, project_id, root_id, job_id, workspace = web_db
    runner_file = tmp_path / "runner-status.json"
    runner_file.write_text(json.dumps({"status": "idle", "queue_depth": {"reasoner": 2, "coder": 0},
                                       "heartbeat_at": "2026-11-16T12:00:00Z"}), encoding="utf-8")
    client = TestClient(create_app(db, runner_status_file=runner_file))
    state = client.get("/api/runner").json()
    assert state["status"] == "idle"
    assert state["queue_depth"]["reasoner"] == 2


def test_sse_backlog_emits_project_events(web_db):
    import asyncio

    from nai.web.app import WebSettings, _event_stream

    db, project_id, root_id, job_id, workspace = web_db

    class DisconnectedRequest:
        calls = 0
        async def is_disconnected(self):
            self.calls += 1
            return self.calls > 1

    settings = WebSettings(database_path=db)
    async def collect():
        output = []
        async for chunk in _event_stream(settings, project_id, job_id, None, DisconnectedRequest()):
            output.append(chunk)
        return output

    messages = asyncio.run(collect())
    assert messages
    assert any("job.queued" in message or "job.started" in message for message in messages)
    assert all(message.startswith("id: ") for message in messages)


def test_model_server_refuses_non_loopback_url():
    with pytest.raises(ValueError, match="loopback"):
        ModelServerClient("https://example.com")


def test_workspace_file_browser_excludes_symlink_escape(web_db, tmp_path):
    db, project_id, root_id, job_id, workspace = web_db
    outside = tmp_path / "private.txt"
    outside.write_text("not a project file", encoding="utf-8")
    link = workspace / "escape.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable on this filesystem")
    assert "escape.txt" not in {f["path"] for f in _list_project_files(workspace)}
    with pytest.raises(Exception):
        _safe_workspace_path(workspace, "escape.txt", allow_hidden=False)
