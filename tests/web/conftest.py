from pathlib import Path

import pytest

from nai.store import Store


@pytest.fixture(autouse=True)
def _web1_env(monkeypatch):
    """WP7 web tests expect no bearer token configured."""
    monkeypatch.delenv("NAI_WEB_TOKEN", raising=False)
    monkeypatch.delenv("NAI_DB_PATH", raising=False)


@pytest.fixture
def store(tmp_path: Path):
    db_path = tmp_path / "nai.sqlite3"
    with Store(db_path) as instance:
        yield instance


@pytest.fixture
def project(store: Store, tmp_path: Path) -> str:
    return store.create_project(
        "Test Project",
        "A project brief",
        workspace_path=tmp_path / "workspace",
        settings={"target_env": {"python": "3.12", "dependencies": []}},
    )
