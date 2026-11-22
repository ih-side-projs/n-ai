import tempfile
from pathlib import Path

import pytest

from nai.store import Store


@pytest.fixture(autouse=True)
def _web_env(monkeypatch):
    """Web tests control env explicitly in their own fixtures."""
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


@pytest.fixture
def temp_db():
    """Create a temporary database for testing."""
    with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as f:
        db_path = Path(f.name)
    yield db_path
    if db_path.exists():
        db_path.unlink()


@pytest.fixture
def initialized_store(temp_db):
    """Create an initialized store with test data."""
    from nai.store import Store
    store = Store(temp_db, read_only=False)
    # Run migrations
    store.close()
    
    # Reopen and add test data
    store = Store(temp_db, read_only=False)
    
    # Create a test project
    project_id = store.create_project("Test Project", "A test project", workspace_path="/tmp/test")
    
    # Create test nodes
    node1 = store.create_node(project_id, "node1", "component", objective="Test node 1")
    store.create_node(project_id, "node2", "component", objective="Test node 2", parent_id=node1)
    
    # Add some approvals
    store.add_approval(node1, "component-spec", "approved", note="Looks good")
    
    # Add a tool definition
    store.conn.execute("""
        INSERT INTO tool_defs (name, description, parameters_json, family, side_effects, timeout_seconds)
        VALUES (?, ?, ?, ?, ?, ?)
    """, ("read_csv", "Read a CSV file", '{"type": "object", "properties": {}}', "data_files", "none", 10))
    store.conn.commit()
    
    store.close()
    
    # Return read-only store for tests
    yield Store(temp_db, read_only=True)