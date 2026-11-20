"""Pytest configuration for WP6 tests."""
import sys
from pathlib import Path

# Add the WP6 package to the path
wp6_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(wp6_root))

# Also add WP1 for nai.core dependencies
wp1_root = wp6_root.parent / "WP1" / "extracted" / "nai-wp1"
if wp1_root.exists():
    sys.path.insert(0, str(wp1_root))


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