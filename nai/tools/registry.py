"""
Tool Registry for SS-TOOLS (WP6).

Tools are ordinary Python functions with type hints and docstrings, registered with a
decorator giving name, family, side effects and timeout. The registry generates:
(a) OpenAI-style function schemas for the orchestration code
(b) MCP tool definitions
One definition, two consumers. (TLS-1)
"""
from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from functools import wraps
from typing import Any, get_type_hints

from .sandbox import Sandbox


@dataclass(frozen=True)
class ToolSpec:
    """Complete specification of a registered tool."""
    name: str
    family: str
    description: str
    fn: Callable
    params_schema: dict[str, Any]  # JSON Schema for parameters
    side_effects: bool = False
    timeout_seconds: float = 30.0
    returns_description: str = ""

    def to_openai_schema(self) -> dict:
        """Generate OpenAI-style function schema (for orchestration)."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.params_schema,
            },
        }

    def to_mcp_schema(self) -> dict:
        """Generate MCP tool definition."""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.params_schema,
        }


class ToolRegistry:
    """
    Central registry for all tools.
    
    Tools are registered with @register_tool decorator. The registry provides:
    - OpenAI-style function schemas for model calls
    - MCP tool definitions for MCP server
    - Allowlist filtering per job
    - Meta-tools for discovery (list_tools, describe_tool)
    """
    
    _instance: ToolRegistry | None = None
    
    def __new__(cls) -> ToolRegistry:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._tools: dict[str, ToolSpec] = {}
            cls._instance._families: dict[str, set[str]] = {}
        return cls._instance
    
    def __init__(self):
        # Ensure meta-tools are registered
        if not self._tools:
            self._register_meta_tools()
    
    def _register_meta_tools(self) -> None:
        """Register the built-in meta-tools for tool discovery (TLS-4)."""
        # Meta-tool: list_tools
        def list_tools(family: str | None = None) -> str:
            """List all available tools, optionally filtered by family."""
            if family:
                tools = self._families.get(family, set())
                if not tools:
                    return f"ERROR: Unknown family: {family}"
                tool_list = sorted(tools)
            else:
                tool_list = sorted(self._tools.keys())
            
            if not tool_list:
                return "No tools registered."
            
            lines = ["Available tools:"]
            for name in tool_list:
                spec = self._tools[name]
                lines.append(f"  {name} ({spec.family}): {spec.description.split('.')[0]}.")
            return "\n".join(lines)
        
        # Meta-tool: describe_tool
        def describe_tool(name: str) -> str:
            """Get detailed information about a specific tool."""
            spec = self._tools.get(name)
            if not spec:
                return f"ERROR: Unknown tool: {name}"
            
            # Format parameters nicely
            props = spec.params_schema.get("properties", {})
            required = set(spec.params_schema.get("required", []))
            
            lines = [
                f"Tool: {spec.name}",
                f"Family: {spec.family}",
                f"Description: {spec.description}",
                f"Side effects: {'yes' if spec.side_effects else 'no'}",
                f"Timeout: {spec.timeout_seconds}s",
                "",
                "Parameters:",
            ]
            for param_name, param_info in props.items():
                req = " (required)" if param_name in required else " (optional)"
                param_type = param_info.get("type", "any")
                param_desc = param_info.get("description", "")
                lines.append(f"  {param_name}: {param_type}{req} - {param_desc}")
            
            if spec.returns_description:
                lines.append(f"\nReturns: {spec.returns_description}")
            
            return "\n".join(lines)
        
        # Register meta-tools directly
        list_spec = ToolSpec(
            name="list_tools",
            family="meta",
            description="List all available tools, optionally filtered by family.",
            fn=list_tools,
            params_schema={
                "type": "object",
                "properties": {
                    "family": {"type": "string", "description": "Optional family name to filter by"}
                },
                "required": [],
            },
            side_effects=False,
            timeout_seconds=5.0,
            returns_description="Formatted list of tool names and brief descriptions",
        )
        describe_spec = ToolSpec(
            name="describe_tool",
            family="meta",
            description="Get detailed information about a specific tool including parameters.",
            fn=describe_tool,
            params_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Name of the tool to describe"}
                },
                "required": ["name"],
            },
            side_effects=False,
            timeout_seconds=5.0,
            returns_description="Detailed tool specification including parameters and return format",
        )
        
        self._tools["list_tools"] = list_spec
        self._tools["describe_tool"] = describe_spec
        self._families.setdefault("meta", set()).update(["list_tools", "describe_tool"])
    
    def register(self, spec: ToolSpec) -> None:
        """Register a tool specification."""
        if spec.name in self._tools:
            raise ValueError(f"Tool already registered: {spec.name}")
        self._tools[spec.name] = spec
        self._families.setdefault(spec.family, set()).add(spec.name)
    
    def get(self, name: str) -> ToolSpec | None:
        """Get a tool by name."""
        return self._tools.get(name)
    
    def get_all(self) -> dict[str, ToolSpec]:
        """Get all registered tools."""
        return dict(self._tools)
    
    def get_by_family(self, family: str) -> dict[str, ToolSpec]:
        """Get all tools in a family."""
        names = self._families.get(family, set())
        return {name: self._tools[name] for name in names if name in self._tools}
    
    def get_families(self) -> list[str]:
        """Get list of all families."""
        return sorted(self._families.keys())
    
    def get_openai_schemas(self, allowlist: list[str] | None = None) -> list[dict]:
        """
        Get OpenAI-style function schemas for tools in the allowlist.
        If allowlist is None, return all tools.
        (TLS-3: Allowlists)
        """
        tools = self._tools
        if allowlist is not None:
            tools = {k: v for k, v in self._tools.items() if k in allowlist}
        return [spec.to_openai_schema() for spec in tools.values()]
    
    def get_mcp_schemas(self, allowlist: list[str] | None = None) -> list[dict]:
        """
        Get MCP tool definitions for tools in the allowlist.
        If allowlist is None, return all tools.
        """
        tools = self._tools
        if allowlist is not None:
            tools = {k: v for k, v in self._tools.items() if k in allowlist}
        return [spec.to_mcp_schema() for spec in tools.values()]
    
    def call_tool(self, name: str, args: dict, sandbox: Sandbox | None = None) -> str:
        """Call a tool with per-call sandbox, timeout, output limit and validation.

        Returns a human-readable string; errors start with "ERROR:" (TLS-7).
        """
        spec = self._tools.get(name)
        if not spec:
            return f"ERROR: Unknown tool: {name}"

        try:
            from pathlib import Path as _Path

            from .sandbox import _current_sandbox, get_current_sandbox

            # Validate arguments against the JSON Schema generated from type hints.
            props = spec.params_schema.get("properties", {})
            for req in spec.params_schema.get("required", []):
                if req not in args:
                    return f"ERROR: Missing required parameter: {req}"
            for key in args:
                if key not in props:
                    return f"ERROR: Unexpected parameter: {key}"
            for key, schema in props.items():
                if key not in args:
                    continue
                value = args[key]
                typ = schema.get("type")
                if typ == "string" and not isinstance(value, str):
                    return f"ERROR: Parameter {key} must be a string"
                if typ == "integer" and (isinstance(value, bool) or not isinstance(value, int)):
                    return f"ERROR: Parameter {key} must be an integer"
                if typ == "number" and (isinstance(value, bool) or not isinstance(value, (int, float))):
                    return f"ERROR: Parameter {key} must be a number"
                if typ == "boolean" and not isinstance(value, bool):
                    return f"ERROR: Parameter {key} must be a boolean"
                if typ == "array" and not isinstance(value, list):
                    return f"ERROR: Parameter {key} must be an array"
                if "enum" in schema and value not in schema["enum"]:
                    return f"ERROR: Parameter {key} must be one of {schema['enum']}"

            sb = sandbox or get_current_sandbox() or Sandbox(_Path.cwd())
            token = _current_sandbox.set(sb)
            try:
                if spec.timeout_seconds and spec.timeout_seconds > 0:
                    result = sb.run_with_timeout(spec.fn, args=(), kwargs=args, timeout=spec.timeout_seconds)
                else:
                    result = spec.fn(**args)
            finally:
                _current_sandbox.reset(token)
            if not isinstance(result, str):
                result = str(result)
            return sb.limit_output(result)
        except Exception as e:
            return f"ERROR: {type(e).__name__}: {e}"


# Global registry instance
_registry = ToolRegistry()


def get_registry() -> ToolRegistry:
    """Get the global tool registry instance."""
    return _registry


def register_tool(
    name: str,
    family: str,
    description: str,
    side_effects: bool = False,
    timeout_seconds: float = 30.0,
    returns_description: str = "",
) -> Callable[[Callable], Callable]:
    """
    Decorator to register a tool.
    
    Args:
        name: Unique tool name (e.g., "read_csv")
        family: Tool family (e.g., "data_files", "markdown", "excel")
        description: Human-readable description for the model
        side_effects: Whether the tool modifies state (files, network, etc.)
        timeout_seconds: Maximum execution time
        returns_description: Description of what the tool returns
    
    The decorated function must have type hints for all parameters.
    The docstring is used as the parameter descriptions in the schema.
    """
    def decorator(fn: Callable) -> Callable:
        # Extract parameter schema from function signature and type hints
        sig = inspect.signature(fn)
        type_hints = get_type_hints(fn)
        
        properties = {}
        required = []
        
        for param_name, param in sig.parameters.items():
            if param_name in ("self", "cls"):
                continue
            
            param_type = type_hints.get(param_name, Any)
            param_schema = _python_type_to_json_schema(param_type)
            
            # Get description from docstring if available
            param_desc = _extract_param_docstring(fn, param_name)
            if param_desc:
                param_schema["description"] = param_desc
            
            properties[param_name] = param_schema
            
            if param.default == inspect.Parameter.empty:
                required.append(param_name)
        
        params_schema = {
            "type": "object",
            "properties": properties,
            "required": required,
        }
        
        spec = ToolSpec(
            name=name,
            family=family,
            description=description,
            fn=fn,
            params_schema=params_schema,
            side_effects=side_effects,
            timeout_seconds=timeout_seconds,
            returns_description=returns_description or (fn.__doc__ or "").strip(),
        )
        
        _registry.register(spec)
        
        @wraps(fn)
        def wrapper(*args, **kwargs):
            return fn(*args, **kwargs)
        
        return wrapper
    
    return decorator


def _python_type_to_json_schema(py_type: Any) -> dict:
    """Convert a Python type hint to a JSON Schema fragment."""
    # Handle basic types
    if py_type is str:
        return {"type": "string"}
    if py_type is int:
        return {"type": "integer"}
    if py_type is float:
        return {"type": "number"}
    if py_type is bool:
        return {"type": "boolean"}
    if py_type is type(None):
        return {"type": "null"}
    
    # Handle Optional[X] -> X | null
    from typing import Union, get_args, get_origin
    origin = get_origin(py_type)
    args = get_args(py_type)
    
    if origin is Union and type(None) in args:
        # Optional[X]
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            schema = _python_type_to_json_schema(non_none[0])
            schema["nullable"] = True
            return schema
    
    if origin is list or (origin and str(origin).startswith("typing.List")):
        item_schema = _python_type_to_json_schema(args[0]) if args else {"type": "string"}
        return {"type": "array", "items": item_schema}
    
    if origin is dict or (origin and str(origin).startswith("typing.Dict")):
        return {"type": "object", "additionalProperties": True}
    
    # Default to string for unknown types
    return {"type": "string"}


def _extract_param_docstring(fn: Callable, param_name: str) -> str | None:
    """Extract parameter description from function docstring (NumPy/Google style)."""
    doc = fn.__doc__
    if not doc:
        return None
    
    # Simple extraction for "param_name: description" patterns
    lines = doc.split("\n")
    in_params = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("Args:") or stripped.startswith("Parameters:") or stripped.startswith("Arguments:"):
            in_params = True
            continue
        if in_params and stripped and not stripped[0].isspace():
            # New section
            break
        if in_params and ":" in stripped:
            # Check if this line documents our parameter
            parts = stripped.split(":", 1)
            if parts[0].strip() == param_name:
                return parts[1].strip()
    
    return None


# Convenience: register a tool directly without decorator
def register_tool_direct(spec: ToolSpec) -> None:
    """Register a ToolSpec directly (for meta-tools and built-ins)."""
    _registry.register(spec)