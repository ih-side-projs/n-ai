"""Tests for the newly wired web features: create, tools, settings, service, runner health."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from nai.web import create_app


@pytest.fixture
def web_app(initialized_store, monkeypatch):
    db = initialized_store.conn.execute("PRAGMA database_list").fetchone()[2]
    monkeypatch.setenv("NAI_DB_PATH", db)
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
        (tmp_path / "x.json").write_text('{"ok": true}', encoding="utf-8")
        r = await client.post("/tools/call", data={
            "csrf_token": token, "tool_name": "read_json",
            "arguments": json.dumps({"path": "x.json"}), "workspace": str(tmp_path),
        })
        assert r.status_code == 200
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
