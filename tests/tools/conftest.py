"""Pytest configuration for tool tests."""
import pytest


@pytest.fixture(autouse=True)
def _restore_tool_registry():
    """Several registry tests clear the singleton; restore it for later modules."""
    from nai.tools.registry import get_registry

    registry = get_registry()
    saved_tools = dict(registry._tools)
    saved_families = {k: set(v) for k, v in registry._families.items()}
    yield
    registry._tools.clear()
    registry._tools.update(saved_tools)
    registry._families.clear()
    registry._families.update(saved_families)