"""
Tests for the Tool Registry (WP6).
"""
from __future__ import annotations

import pytest

from nai.tools.registry import ToolRegistry, ToolSpec, get_registry


class TestToolRegistry:
    """Tests for the ToolRegistry class."""
    
    def test_singleton(self):
        """Registry should be a singleton."""
        r1 = ToolRegistry()
        r2 = ToolRegistry()
        assert r1 is r2
    
    def test_get_registry(self):
        """get_registry should return the singleton."""
        r = get_registry()
        assert isinstance(r, ToolRegistry)
    
    def test_register_and_get(self):
        """Register and retrieve a tool."""
        registry = ToolRegistry()
        registry._tools.clear()
        registry._families.clear()
        
        def dummy_fn(x: int) -> str:
            return str(x)
        
        spec = ToolSpec(
            name="test_tool",
            family="test",
            description="A test tool",
            fn=dummy_fn,
            params_schema={
                "type": "object",
                "properties": {"x": {"type": "integer"}},
                "required": ["x"],
            },
        )
        registry.register(spec)
        
        got = registry.get("test_tool")
        assert got is not None
        assert got.name == "test_tool"
        assert got.family == "test"
    
    def test_duplicate_registration_raises(self):
        """Registering duplicate tool name should raise."""
        registry = ToolRegistry()
        registry._tools.clear()
        registry._families.clear()
        
        def dummy_fn() -> str:
            return "x"
        
        spec = ToolSpec(
            name="dup",
            family="test",
            description="test",
            fn=dummy_fn,
            params_schema={"type": "object", "properties": {}, "required": []},
        )
        registry.register(spec)
        
        with pytest.raises(ValueError, match="already registered"):
            registry.register(spec)