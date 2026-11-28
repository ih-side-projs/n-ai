"""The dashboard's read-only ``/workers`` page."""

from __future__ import annotations

from fastapi.testclient import TestClient

from nai.web.app import create_app


def test_workers_page_lists_workers_and_tasks(store, project, worker) -> None:
    store.create_remote_task(project["id"], "echo", "a" * 64,
                             params={"params": {"message": "x"}}, required_caps=["macos"])
    client = TestClient(create_app(store.db_path))
    page = client.get("/workers")
    assert page.status_code == 200
    assert "Remote workers" in page.text
    assert "macbook" in page.text
    assert "online" in page.text
    assert "echo" in page.text
    assert 'href="/workers"' in page.text  # nav entry
    # Read-only: no form or control endpoints on this page.
    assert "<form" not in page.text.lower()


def test_workers_api_matches_the_page(store, project, worker) -> None:
    client = TestClient(create_app(store.db_path))
    payload = client.get("/api/workers").json()
    assert payload["workers"][0]["name"] == "macbook"
    assert payload["workers"][0]["online"] is True
    assert payload["workers"][0]["capabilities"] == ["macos", "python"]
    assert payload["tasks"] == []


def test_workers_page_is_empty_without_workers(store) -> None:
    client = TestClient(create_app(store.db_path))
    page = client.get("/workers")
    assert page.status_code == 200
    assert "No workers registered" in page.text
