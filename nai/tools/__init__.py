"""nai.tools: Tool registry, MCP server, and tool families for SS-TOOLS (WP6)."""
from .registry import ToolRegistry, register_tool, get_registry, ToolSpec
from .sandbox import Sandbox, SandboxError
from .mcp_server import MCPServer, run_mcp_server
from .cli import register_mcp_subcommand

# Import tool families to register them
from .families import data_files, project_introspection

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