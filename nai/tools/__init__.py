"""nai.tools: Tool registry, MCP server, and tool families for SS-TOOLS (WP6)."""
from .cli import register_mcp_subcommand

# Import tool families to register them
from .families import data_files, project_introspection  # noqa: F401
from .mcp_server import MCPServer, run_mcp_server
from .registry import ToolRegistry, ToolSpec, get_registry, register_tool
from .sandbox import Sandbox, SandboxError

__all__ = [
    "ToolRegistry",
    "register_tool",
    "get_registry",
    "ToolSpec",
    "Sandbox",
    "SandboxError",
    "MCPServer",
    "run_mcp_server",
    "register_mcp_subcommand",
]