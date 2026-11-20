"""Tests for WP8 web interface."""

import pytest
from httpx import ASGITransport, AsyncClient

from nai.web import create_app


@pytest.fixture
def app(initialized_store):
    """Create the FastAPI app for testing."""
    import os
    os.environ["NAI_DB_PATH"] = str(initialized_store.conn.execute("PRAGMA database_list").fetchone()[2])
    os.environ["NAI_WEB_TOKEN"] = "test-token-123"
    return create_app()


@pytest.fixture
async def client(app):
    """Create an async test client."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest.fixture
def auth_cookie():
    """Return the auth cookie for tests."""
    return {"nai_auth": "test-token-123"}


class TestHealthAndAuth:
    """Test health and authentication endpoints."""

    async def test_healthz(self, client):
        response = await client.get("/healthz")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"


class TestProjects:
    """Test project-related endpoints."""

    async def test_projects_page(self, client, auth_cookie):
        response = await client.get("/", cookies=auth_cookie)
        assert response.status_code == 200
        assert "Test Project" in response.text

    async def test_projects_json_api(self, client, auth_cookie):
        response = await client.get("/api/projects", cookies=auth_cookie)
        assert response.status_code == 200
        data = response.json()
        assert "projects" in data
        assert len(data["projects"]) == 1
        assert data["projects"][0]["name"] == "Test Project"


class TestNodes:
    """Test node-related endpoints."""

    async def test_node_page(self, client, auth_cookie, initialized_store):
        nodes = initialized_store.list_nodes(initialized_store.conn.execute("SELECT id FROM projects").fetchone()[0])
        if nodes:
            node_id = nodes[0]["id"]
            project_id = initialized_store.conn.execute("SELECT id FROM projects").fetchone()[0]
            response = await client.get(f"/projects/{project_id}/nodes/{node_id}", cookies=auth_cookie)
            assert response.status_code == 200

    async def test_node_edit_page(self, client, auth_cookie, initialized_store):
        nodes = initialized_store.list_nodes(initialized_store.conn.execute("SELECT id FROM projects").fetchone()[0])
        if nodes:
            node_id = nodes[0]["id"]
            project_id = initialized_store.conn.execute("SELECT id FROM projects").fetchone()[0]
            response = await client.get(f"/projects/{project_id}/nodes/{node_id}/edit", cookies=auth_cookie)
            assert response.status_code == 200
            assert "Edit Specification" in response.text


class TestApprovals:
    """Test approvals inbox."""

    async def test_approvals_page(self, client, auth_cookie):
        response = await client.get("/approvals", cookies=auth_cookie)
        assert response.status_code == 200
        assert "awaiting approval" in response.text.lower() or "Pending approvals" in response.text

    async def test_approvals_json_api(self, client, auth_cookie):
        response = await client.get("/api/approvals", cookies=auth_cookie)
        assert response.status_code == 200
        data = response.json()
        assert "approvals" in data


class TestEscalations:
    """Test escalations inbox."""

    async def test_escalations_page(self, client, auth_cookie):
        response = await client.get("/escalations", cookies=auth_cookie)
        assert response.status_code == 200

    async def test_escalations_json_api(self, client, auth_cookie):
        response = await client.get("/api/escalations", cookies=auth_cookie)
        assert response.status_code == 200
        data = response.json()
        assert "escalations" in data


class TestTools:
    """Test tools page."""

    async def test_tools_page(self, client, auth_cookie):
        response = await client.get("/tools", cookies=auth_cookie)
        assert response.status_code == 200
        assert "TOOL REGISTRY" in response.text
        assert "read_csv" in response.text

    async def test_tools_json_api(self, client, auth_cookie):
        response = await client.get("/api/tools", cookies=auth_cookie)
        assert response.status_code == 200
        data = response.json()
        assert "tools" in data
        assert len(data["tools"]) >= 1


class TestControls:
    """Test project control endpoints."""

    async def test_control_pause(self, client, auth_cookie, initialized_store):
        project_id = initialized_store.conn.execute("SELECT id FROM projects").fetchone()[0]
        response = await client.post(
            f"/projects/{project_id}/control/pause",
            data={"csrf_token": "test"},
            cookies=auth_cookie
        )
        assert response.status_code != 404

    async def test_control_resume(self, client, auth_cookie, initialized_store):
        project_id = initialized_store.conn.execute("SELECT id FROM projects").fetchone()[0]
        response = await client.post(
            f"/projects/{project_id}/control/resume",
            data={"csrf_token": "test"},
            cookies=auth_cookie
        )
        assert response.status_code != 404


class TestLogs:
    """Test logs endpoints."""

    async def test_logs_page(self, client, auth_cookie, initialized_store):
        project_id = initialized_store.conn.execute("SELECT id FROM projects").fetchone()[0]
        response = await client.get(f"/projects/{project_id}/logs", cookies=auth_cookie)
        assert response.status_code == 200
        assert "LIVE LOGS" in response.text

    async def test_logs_json_api(self, client, auth_cookie, initialized_store):
        project_id = initialized_store.conn.execute("SELECT id FROM projects").fetchone()[0]
        response = await client.get(f"/api/projects/{project_id}/logs", cookies=auth_cookie)
        assert response.status_code == 200
        data = response.json()
        assert "events" in data


class TestMetrics:
    """Test metrics endpoints."""

    async def test_metrics_page(self, client, auth_cookie, initialized_store):
        project_id = initialized_store.conn.execute("SELECT id FROM projects").fetchone()[0]
        response = await client.get(f"/projects/{project_id}/metrics", cookies=auth_cookie)
        assert response.status_code == 200
        assert "METRICS" in response.text

    async def test_metrics_json_api(self, client, auth_cookie, initialized_store):
        project_id = initialized_store.conn.execute("SELECT id FROM projects").fetchone()[0]
        response = await client.get(f"/api/projects/{project_id}/metrics", cookies=auth_cookie)
        assert response.status_code == 200
        data = response.json()
        assert "total_tokens" in data


class TestModels:
    """Test models endpoints."""

    async def test_models_page(self, client, auth_cookie):
        response = await client.get("/models", cookies=auth_cookie)
        assert response.status_code == 200
        assert "MODELS" in response.text

    async def test_models_json_api(self, client, auth_cookie):
        response = await client.get("/api/models", cookies=auth_cookie)
        assert response.status_code == 200
        data = response.json()
        assert "profiles" in data
        assert "server" in data


class TestArtifacts:
    """Test artifacts endpoints."""

    async def test_artifacts_page(self, client, auth_cookie, initialized_store):
        project_id = initialized_store.conn.execute("SELECT id FROM projects").fetchone()[0]
        response = await client.get(f"/projects/{project_id}/artifacts", cookies=auth_cookie)
        assert response.status_code == 200
        assert "ARTIFACTS" in response.text

    async def test_artifacts_json_api(self, client, auth_cookie, initialized_store):
        project_id = initialized_store.conn.execute("SELECT id FROM projects").fetchone()[0]
        response = await client.get(f"/api/projects/{project_id}/artifacts", cookies=auth_cookie)
        assert response.status_code == 200
        data = response.json()
        assert "artifacts" in data
        assert "files" in data


class TestSecurity:
    """Test security features."""

    async def test_csrf_protection(self, client, auth_cookie, initialized_store):
        project_id = initialized_store.conn.execute("SELECT id FROM projects").fetchone()[0]
        node_id = initialized_store.list_nodes(project_id)[0]["id"]
        response = await client.post(
            f"/projects/{project_id}/nodes/{node_id}/approve",
            data={"note": "test"},
            cookies=auth_cookie
        )
        assert response.status_code in (400, 403)

    async def test_content_security_policy(self, client, auth_cookie):
        response = await client.get("/", cookies=auth_cookie)
        assert response.status_code == 200
        csp = response.headers.get("content-security-policy")
        assert csp is not None
        assert "default-src 'self'" in csp


class TestNavigation:
    """Test navigation and UI elements."""

    async def test_navigation_links(self, client, auth_cookie):
        response = await client.get("/", cookies=auth_cookie)
        assert response.status_code == 200
        assert 'href="/"' in response.text
        assert 'href="/approvals"' in response.text
        assert 'href="/escalations"' in response.text
        assert 'href="/tools"' in response.text
        assert 'href="/models"' in response.text