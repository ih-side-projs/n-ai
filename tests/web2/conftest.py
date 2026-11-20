"""Pytest configuration for WP8 tests."""
import os
import sys
import tempfile
from pathlib import Path

import pytest

# Add the WP8 package to the path
wp8_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(wp8_root))

@pytest.fixture(autouse=True)
def _web2_env(monkeypatch):
    """Isolate WP8 web test environment from other test suites."""
    monkeypatch.delenv("NAI_DB_PATH", raising=False)
    monkeypatch.setenv("NAI_WEB_TOKEN", "test-token-123")


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
    node2 = store.create_node(project_id, "node2", "component", objective="Test node 2", parent_id=node1)
    
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