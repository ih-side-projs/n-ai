"""Tests for the newly wired web features: create, tools, settings, service, runner health."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from nai.store import Store
from nai.web import create_app


@pytest.fixture
def web_app(initialized_store, monkeypatch, tmp_path):
    db = initialized_store.conn.execute("PRAGMA database_list").fetchone()[2]
    monkeypatch.setenv("NAI_DB_PATH", db)
    monkeypatch.setenv("NAI_STATE_DIR", str(tmp_path / "no-service"))
    monkeypatch.delenv("NAI_WEB_TOKEN", raising=False)
    return create_app(db)


@pytest.fixture
async def client(web_app):
    async with AsyncClient(transport=ASGITransport(app=web_app), base_url="http://test") as c:
        yield c


async def _csrf(client):
    r = await client.get("/")
    return r.cookies.get("nai_csrf")


class TestProjectCreate:
    async def test_create_project_form_and_submit(self, client):
        token = await _csrf(client)
        r = await client.post("/projects/create", data={
            "csrf_token": token, "name": "Web Created", "brief": "A project made in the UI",
        }, follow_redirects=False)
        assert r.status_code == 303
        assert "/projects/" in r.headers["location"]

    async def test_project_list_shows_created(self, client):
        token = await _csrf(client)
        await client.post("/projects/create", data={"csrf_token": token, "name": "Visible Project"})
        r = await client.get("/")
        assert "Visible Project" in r.text


class TestToolsExecution:
    async def test_tool_call_executes_read_json(self, client, tmp_path):
        token = await _csrf(client)
        r = await client.post("/tools/call", data={
            "csrf_token": token, "tool_name": "read_json",
            "arguments": json.dumps({"path": "x.json"}), "workspace": str(tmp_path),
        })
        assert r.status_code == 200
        data = r.json()
        # Arbitrary workspace is rejected (path traversal protection)
        assert data["isError"] is True
        assert "registered project workspace" in data["result"]
        # Now create file in the default sandbox and call without workspace
        ws = Path(data["workspace"]) if data["workspace"] else None
        if ws is None:
            import os
            ws = Path(os.environ.get("NAI_DB_PATH", ".nai/nai.sqlite3")).parent / "tools-workspace"
        ws.mkdir(parents=True, exist_ok=True)
        (ws / "x.json").write_text('{"ok": true}', encoding="utf-8")
        r = await client.post("/tools/call", data={
            "csrf_token": token, "tool_name": "read_json",
            "arguments": json.dumps({"path": "x.json"}),
        })
        data = r.json()
        assert data["isError"] is False
        assert '"ok": true' in data["result"]

    async def test_tool_call_unknown_tool_is_error(self, client):
        token = await _csrf(client)
        r = await client.post("/tools/call", data={
            "csrf_token": token, "tool_name": "does_not_exist", "arguments": "{}",
        })
        assert r.status_code == 200
        assert r.json()["isError"] is True


class TestSettings:
    async def test_settings_page_and_save(self, client, initialized_store):
        token = await _csrf(client)
        pid = initialized_store.conn.execute("SELECT id FROM projects LIMIT 1").fetchone()[0]
        r = await client.get(f"/projects/{pid}/settings")
        assert r.status_code == 200
        assert "Size limits" in r.text
        r = await client.post(f"/projects/{pid}/settings", data={
            "csrf_token": token, "max_requirements": "6", "max_functions": "9",
            "max_components": "5", "max_depth": "3", "budget_limit_tokens": "1000",
            "test_commands": "pytest -q\nruff check .", "backup_location": "/tmp/bak",
            "target_language": "Python 3.12",
        }, follow_redirects=False)
        assert r.status_code == 303
        saved = initialized_store.get_project(pid)["settings"]
        assert saved["max_components_per_level"] == 5
        assert saved["test_commands"] == ["pytest -q", "ruff check ."]


class TestServicePage:
    async def test_service_page_without_supervisor(self, client):
        r = await client.get("/service")
        assert r.status_code == 200
        assert "not running" in r.text.lower()
        r = await client.get("/api/service")
        assert r.json()["running"] is False


class TestWorkflowPhases:
    async def test_project_page_phase_tabs(self, client, initialized_store):
        pid = initialized_store.conn.execute("SELECT id FROM projects LIMIT 1").fetchone()[0]
        r = await client.get(f"/projects/{pid}")
        assert r.status_code == 200
        assert 'phase-tab' in r.text
        assert 'Design' in r.text
        assert 'Implementation' in r.text

    async def test_design_tab_shows_banner(self, client, initialized_store):
        pid = initialized_store.conn.execute("SELECT id FROM projects LIMIT 1").fetchone()[0]
        r = await client.get(f"/projects/{pid}?phase=design")
        assert r.status_code == 200
        assert "Design phase" in r.text
        assert "iterating on requirements" in r.text

    async def test_implementation_tab_shows_banner(self, client, initialized_store):
        pid = initialized_store.conn.execute("SELECT id FROM projects LIMIT 1").fetchone()[0]
        r = await client.get(f"/projects/{pid}?phase=implementation")
        assert r.status_code == 200
        assert "Implementation phase" in r.text

    async def test_phase_api(self, client, initialized_store):
        pid = initialized_store.conn.execute("SELECT id FROM projects LIMIT 1").fetchone()[0]
        r = await client.get(f"/api/projects/{pid}/phase")
        assert r.status_code == 200
        data = r.json()
        assert data["phase"] in ("design", "implementation", "mixed")
        assert "per_node" in data
        assert "tree" in data

    async def test_node_page_shows_phase(self, client, initialized_store):
        pid = initialized_store.conn.execute("SELECT id FROM projects LIMIT 1").fetchone()[0]
        nid = initialized_store.conn.execute("SELECT id FROM nodes WHERE project_id=? LIMIT 1", (pid,)).fetchone()[0]
        r = await client.get(f"/projects/{pid}/nodes/{nid}")
        assert r.status_code == 200
        assert "PHASE" in r.text
        r = await client.get(f"/api/projects/{pid}/nodes/{nid}")
        assert r.json()["phase"] in ("design", "implementation")


class TestServiceRunningFlag:
    def test_daemon_status_has_running_key(self):
        from nai.service.config import ServiceConfig
        from nai.service.daemon import ServiceDaemon

        cfg = ServiceConfig(state_dir=Path("/tmp/no-such-state"), start_web=False, start_runner=False)
        d = ServiceDaemon(cfg)
        assert d.status()["running"] is True


class TestModelProfileEditing:
    async def test_models_page_has_edit_fields(self, client):
        r = await client.get("/models")
        assert r.status_code == 200
        assert "Reasoner model id" in r.text
        assert "Coder model id" in r.text
        assert 'action="/models/profile"' in r.text

    async def test_save_profile_overrides_default(self, client, initialized_store):
        token = await _csrf(client)
        r = await client.post("/models/profile", data={
            "csrf_token": token, "name": "default", "role": "reasoner",
            "model_id": "my-local-reasoner",
        }, follow_redirects=False)
        assert r.status_code == 303
        prof = initialized_store.get_model_profile("default")
        assert prof["reasoner"]["model_id"] == "my-local-reasoner"
        r = await client.get("/models")
        assert "my-local-reasoner" in r.text


    async def test_saving_one_role_preserves_other(self, client, initialized_store):
        token = await _csrf(client)
        await client.post('/models/profile', data={'csrf_token': token,'name':'default','role':'reasoner','model_id':'my-reasoner'}, follow_redirects=False)
        await client.post('/models/profile', data={'csrf_token': token,'name':'default','role':'coder','model_id':'my-coder'}, follow_redirects=False)
        prof = initialized_store.get_model_profile('default')
        assert prof['reasoner']['model_id'] == 'my-reasoner'
        assert prof['coder']['model_id'] == 'my-coder'
        r = await client.get('/models')
        assert 'my-reasoner' in r.text and 'my-coder' in r.text

    async def test_models_cards_have_readable_layout(self, client):
        r = await client.get("/models")
        assert r.status_code == 200
        assert "metric-value-text" in r.text
        assert "metric-detail" in r.text
        assert "From /v1/models" in r.text


class TestRunnerHealthPath:
    async def test_runner_health_reads_runner_json(self, client, initialized_store, tmp_path, monkeypatch):
        pid = initialized_store.conn.execute("SELECT id FROM projects LIMIT 1").fetchone()[0]
        # Write a health file where SCH-9 actually writes it
        health = tmp_path / "runner.json"
        health.write_text(json.dumps({"state": "idle", "alive": True, "heartbeat": "2026-01-01T00:00:00Z"}))
        monkeypatch.setenv("NAI_RUNNER_STATUS_FILE", str(health))
        r = await client.get(f"/projects/{pid}")
        assert r.status_code == 200
        r = await client.get("/api/projects")
        assert r.status_code == 200


class TestCommandQueueRouting:
    async def test_approve_enqueues_command_only(self, client, initialized_store):
        """STO-3: web writes only to commands; approve/reject/retry never touch status/approvals directly."""
        token = await _csrf(client)
        pid = initialized_store.conn.execute("SELECT id FROM projects LIMIT 1").fetchone()[0]
        nid = initialized_store.conn.execute("SELECT id FROM nodes LIMIT 1", ()).fetchone()
        if not nid:
            nid = initialized_store.conn.execute("SELECT id FROM nodes LIMIT 1", ()).fetchone()[0]
        nid = nid[0]
        before = initialized_store.conn.execute("SELECT COUNT(*) FROM commands").fetchone()[0]
        before_approvals = initialized_store.conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0]
        before_status = initialized_store.get_node(nid)["status"]
        r = await client.post(f"/projects/{pid}/nodes/{nid}/approve", data={
            "csrf_token": token, "note": "ok", "stage": "interfaces",
        }, follow_redirects=False)
        assert r.status_code == 303
        after = initialized_store.conn.execute("SELECT COUNT(*) FROM commands").fetchone()[0]
        assert after == before + 1
        cmd = initialized_store.conn.execute("SELECT * FROM commands ORDER BY created_at DESC LIMIT 1").fetchone()
        assert cmd["action"] == "approve" and cmd["node_id"] == nid
        payload = json.loads(cmd["payload_json"])
        assert payload["stage"] == "interfaces"
        # Directly nothing changed: only the command row was inserted
        assert initialized_store.conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == before_approvals
        assert initialized_store.get_node(nid)["status"] == before_status

    async def test_retry_enqueues_retry_node(self, client, initialized_store):
        token = await _csrf(client)
        pid = initialized_store.conn.execute("SELECT id FROM projects LIMIT 1").fetchone()[0]
        nid = initialized_store.conn.execute("SELECT id FROM nodes LIMIT 1", ()).fetchone()[0]
        r = await client.post(f"/projects/{pid}/nodes/{nid}/retry", data={
            "csrf_token": token,
        }, follow_redirects=False)
        assert r.status_code == 303
        cmd = initialized_store.conn.execute("SELECT * FROM commands ORDER BY created_at DESC LIMIT 1").fetchone()
        assert cmd["action"] == "retry_node"

    async def test_reject_invalid_stage_400(self, client, initialized_store):
        token = await _csrf(client)
        pid = initialized_store.conn.execute("SELECT id FROM projects LIMIT 1").fetchone()[0]
        nid = initialized_store.conn.execute("SELECT id FROM nodes LIMIT 1", ()).fetchone()[0]
        r = await client.post(f"/projects/{pid}/nodes/{nid}/reject", data={
            "csrf_token": token, "note": "no", "stage": "bogus",
        }, follow_redirects=False)
        assert r.status_code == 400


class TestSameOriginLocal:
    async def test_auth_page_loads_fetch_shim(self, initialized_store):
        db = initialized_store.conn.execute("PRAGMA database_list").fetchone()[2]
        app = create_app(db, bearer_token="test-token")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
            r = await c.get("/auth")
        assert r.status_code == 200
        assert 'src="/static/app.js"' in r.text

    async def test_localhost_to_127_origin_accepted(self, client, initialized_store):
        token = await _csrf(client)
        r = await client.post('/models/profile', data={
            'csrf_token': token, 'name':'default', 'role':'reasoner', 'model_id':'local-r'
        }, headers={'origin': 'http://127.0.0.1:8080', 'host': 'localhost:8080'}, follow_redirects=False)
        assert r.status_code == 303

    async def test_external_origin_rejected(self, client, initialized_store):
        token = await _csrf(client)
        r = await client.post('/models/profile', data={
            'csrf_token': token, 'name':'default', 'role':'reasoner', 'model_id':'x'
        }, headers={'origin': 'https://evil.example.com'}, follow_redirects=False)
        assert r.status_code == 403
        assert "evil.example.com" in r.json()["detail"]

    async def test_null_origin_with_ajax_header_accepted(self, client, initialized_store):
        token = await _csrf(client)
        r = await client.post('/models/profile', data={
            'csrf_token': token, 'name': 'default', 'role': 'reasoner', 'model_id': 'opaque-origin'
        }, headers={'origin': 'null', 'x-requested-with': 'fetch'}, follow_redirects=False)
        assert r.status_code == 303

    async def test_null_origin_without_ajax_header_rejected(self, client, initialized_store):
        token = await _csrf(client)
        r = await client.post('/models/profile', data={
            'csrf_token': token, 'name': 'default', 'role': 'reasoner', 'model_id': 'opaque-origin'
        }, headers={'origin': 'null'}, follow_redirects=False)
        assert r.status_code == 403

    async def test_external_origin_with_ajax_header_still_rejected(self, client, initialized_store):
        token = await _csrf(client)
        r = await client.post('/models/profile', data={
            'csrf_token': token, 'name': 'default', 'role': 'reasoner', 'model_id': 'x'
        }, headers={'origin': 'https://evil.example.com', 'x-requested-with': 'fetch'}, follow_redirects=False)
        assert r.status_code == 403

    async def test_sec_fetch_same_origin_accepted(self, client, initialized_store):
        token = await _csrf(client)
        r = await client.post('/models/profile', data={
            'csrf_token': token, 'name':'default', 'role':'coder', 'model_id':'local-c'
        }, headers={'origin': 'https://anything.example', 'sec-fetch-site': 'same-origin'}, follow_redirects=False)
        assert r.status_code == 303


class TestStructuredProjectIntake:
    async def test_new_project_dialog_has_large_brief_and_guardrails(self, client):
        response = await client.get("/")
        assert response.status_code == 200
        assert 'id="new-project-dialog"' in response.text
        assert 'data-open-dialog="new-project-dialog"' in response.text
        for field in (
            "brief", "language", "interface_type", "target_users", "core_workflows",
            "non_goals", "runtime_deployment", "stack_libraries",
            "accessibility_security", "testing_expectations",
        ):
            assert f'name="{field}"' in response.text
        assert 'name="spec_file"' in response.text
        assert 'enctype="multipart/form-data"' in response.text
        assert 'rows="8"' in response.text

    async def test_create_project_persists_intake_guardrails(self, client, initialized_store):
        token = await _csrf(client)
        response = await client.post("/projects/create", data={
            "csrf_token": token,
            "name": "Intake Project",
            "brief": "A detailed paragraph describing the product.",
            "language": "Python 3.12",
            "interface_type": "Web application",
            "target_users": "Small teams",
            "core_workflows": "Create, review, close tasks",
            "non_goals": "No billing",
            "runtime_deployment": "Local LAN",
            "stack_libraries": "FastAPI",
            "accessibility_security": "WCAG AA, local auth",
            "testing_expectations": "pytest + browser smoke",
        }, follow_redirects=False)
        assert response.status_code == 303
        pid = response.headers["location"].rsplit("/", 1)[-1]

        project = initialized_store.get_project(pid)
        settings = project["settings"]
        assert project["brief"] == "A detailed paragraph describing the product."
        assert settings["intake"]["language"] == "Python 3.12"
        assert settings["intake"]["interface_type"] == "Web application"
        assert settings["intake"]["target_users"] == "Small teams"
        assert settings["intake"]["testing_expectations"] == "pytest + browser smoke"
        assert settings["intake_complete"] is False
        assert settings["target_env"]["language"] == "Python 3.12"

    async def test_intake_answers_and_begin_design_enqueue_commands(self, client, initialized_store):
        token = await _csrf(client)
        pid = initialized_store.conn.execute("SELECT id FROM projects LIMIT 1").fetchone()[0]

        response = await client.post(f"/projects/{pid}/intake/answers", data={
            "csrf_token": token, "q_Q1": "developers", "q_Q2": "",
        }, follow_redirects=False)
        assert response.status_code == 303
        response = await client.post(f"/projects/{pid}/intake/begin", data={
            "csrf_token": token,
        }, follow_redirects=False)
        assert response.status_code == 303

        actions = [
            row["action"] for row in initialized_store.conn.execute(
                "SELECT action FROM commands WHERE project_id=? ORDER BY created_at, id", (pid,)
            ).fetchall()
        ]
        assert actions == ["answer_intake", "begin_design"]

    async def test_project_page_shows_info_gathering_panel(self, client, initialized_store):
        pid = initialized_store.conn.execute("SELECT id FROM projects LIMIT 1").fetchone()[0]
        settings = {
            "intake": {"description": "x", "answers": {}},
            "intake_complete": False,
            "intake_waiting": True,
            "intake_questions": [{"id": "Q1", "question": "Who will use it?", "why": "scope"}],
        }
        with Store(initialized_store.db_path, read_only=False) as writable:
            writable.update_project(pid, settings=settings)
        response = await client.get(f"/projects/{pid}")
        assert response.status_code == 200
        assert "Info Gathering" in response.text
        assert "Who will use it?" in response.text
        assert 'name="q_Q1"' in response.text
        assert "Begin design / spec-ing now" in response.text

    async def test_uploaded_spec_seeds_root_and_skips_info_gathering(self, client, initialized_store):
        token = await _csrf(client)
        spec_text = "# Existing Spec\n\nImported from a file.\n\n## Requirements\n- R1: Works\n"
        response = await client.post("/projects/create", data={
            "csrf_token": token,
            "name": "Imported Spec Project",
            "brief": "",
        }, files={"spec_file": ("SPEC.md", spec_text, "text/markdown")}, follow_redirects=False)
        assert response.status_code == 303, response.text
        pid = response.headers["location"].rsplit("/", 1)[-1]

        with Store(initialized_store.db_path, read_only=True) as store:
            project = store.get_project(pid)
            root = store.list_nodes(pid, parent_id=None)[0]
            artifacts = store.list_artifacts(pid, node_id=root["id"])
            events = store.list_events(pid, limit=100)
        assert project["settings"]["intake_complete"] is True
        assert project["settings"]["clarified"] is True
        assert project["settings"]["intake"]["source"] == "uploaded-spec"
        assert root["spec_text"] == spec_text
        assert any(a["kind"] == "spec" for a in artifacts)
        assert any(e["type"] == "project.spec_uploaded" for e in events)

    async def test_pause_controls_follow_project_status(self, client, initialized_store):
        pid = initialized_store.conn.execute("SELECT id FROM projects LIMIT 1").fetchone()[0]
        with Store(initialized_store.db_path, read_only=False) as writable:
            writable.update_project(pid, status="paused")

        response = await client.get(f"/projects/{pid}")
        assert response.status_code == 200
        assert 'disabled>Pause</button>' in response.text
        assert 'disabled>Resume</button>' not in response.text
        assert '>Resume</button>' in response.text


    async def test_submitted_answers_render_read_only(self, client, initialized_store):
        pid = initialized_store.conn.execute("SELECT id FROM projects LIMIT 1").fetchone()[0]
        settings = {
            "intake": {"description": "x", "answers": {"Q1": "Small teams"}},
            "intake_complete": False,
            "intake_waiting": False,
            "intake_questions": [{"id": "Q1", "question": "Who will use it?", "why": "scope"}],
        }
        with Store(initialized_store.db_path, read_only=False) as writable:
            writable.update_project(pid, settings=settings)

        response = await client.get(f"/projects/{pid}")
        assert response.status_code == 200
        assert "Questions and answers" in response.text
        assert "Small teams" in response.text
        assert 'name="q_Q1"' not in response.text

    async def test_completed_intake_shows_clear_next_step(self, client, initialized_store):
        pid = initialized_store.conn.execute("SELECT id FROM projects LIMIT 1").fetchone()[0]
        settings = {
            "intake": {"description": "x", "answers": {"Q1": "Small teams"}},
            "intake_complete": True,
            "intake_waiting": False,
        }
        with Store(initialized_store.db_path, read_only=False) as writable:
            writable.update_project(pid, settings=settings)

        response = await client.get(f"/projects/{pid}")
        assert response.status_code == 200
        assert "NEXT STEP" in response.text
        assert "Design is ready to continue" in response.text
        assert f'href="/projects/{pid}?phase=design"' in response.text




async def test_node_page_sections_have_scoped_scroll(client, initialized_store):
    pid = initialized_store.conn.execute("SELECT id FROM projects LIMIT 1").fetchone()[0]
    nid = initialized_store.conn.execute("SELECT id FROM nodes LIMIT 1", ()).fetchone()[0]
    response = await client.get(f"/projects/{pid}/nodes/{nid}")
    assert response.status_code == 200
    for marker in (
        "node-scroll-panel", "node-panel-spec", "node-panel-details",
        "node-panel-contract", "node-panel-traceability", "node-panel-jobs",
        "node-panel-artifacts", "node-panel-body",
    ):
        assert marker in response.text

