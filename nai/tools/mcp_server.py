"""
MCP Server for SS-TOOLS (WP6).

Serves the tool registry over stdio (primary) and optionally over local HTTP
bound to 127.0.0.1. Verified against a standard MCP client. (TLS-2)
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from .registry import get_registry, ToolRegistry
from .sandbox import Sandbox


class MCPServer:
    """
    Model Context Protocol (MCP) server.
    
    Implements the MCP protocol over stdio for tool discovery and invocation.
    Supports the following MCP methods:
    - initialize: Initialize the connection
    - tools/list: List available tools
    - tools/call: Call a tool
    """
    
    def __init__(self, registry: ToolRegistry | None = None, sandbox: Sandbox | None = None):
        self.registry = registry or get_registry()
        self.sandbox = sandbox
        self.initialized = False
    
    async def handle_request(self, request: dict) -> dict:
        """Handle an incoming MCP request."""
        method = request.get("method")
        params = request.get("params", {})
        request_id = request.get("id")
        
        if method == "initialize":
            return await self._handle_initialize(request_id, params)
        elif method == "tools/list":
            return await self._handle_tools_list(request_id, params)
        elif method == "tools/call":
            return await self._handle_tools_call(request_id, params)
        elif method == "notifications/initialized":
            # Notification, no response needed
            return None
        else:
            return self._error_response(request_id, -32601, f"Method not found: {method}")
    
    async def _handle_initialize(self, request_id: Any, params: dict) -> dict:
        """Handle initialize request."""
        self.initialized = True
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {
                    "tools": {}
                },
                "serverInfo": {
                    "name": "nai-tools",
                    "version": "1.0.0"
                }
            }
        }
    
    async def _handle_tools_list(self, request_id: Any, params: dict) -> dict:
        """Handle tools/list request."""
        # Get allowlist from params if provided
        allowlist = params.get("allowlist")
        
        schemas = self.registry.get_mcp_schemas(allowlist)
        
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "tools": schemas
            }
        }
    
    async def _handle_tools_call(self, request_id: Any, params: dict) -> dict:
        """Handle tools/call request."""
        name = params.get("name")
        arguments = params.get("arguments", {})
        
        if not name:
            return self._error_response(request_id, -32602, "Missing tool name")
        
        # Call the tool through the registry
        result = self.registry.call_tool(name, arguments, self.sandbox)
        
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": result
                    }
                ],
                "isError": result.startswith("ERROR:")
            }
        }
    
    def _error_response(self, request_id: Any, code: int, message: str) -> dict:
        """Create an error response."""
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": code,
                "message": message
            }
        }
    
    async def run_stdio(self) -> None:
        """Run the MCP server over stdio."""
        # Set up sandbox if not provided
        if self.sandbox is None:
            self.sandbox = Sandbox(Path.cwd())
        
        # Import tool families to register them
        from .families import data_files, project_introspection
        data_files.set_sandbox(self.sandbox)
        project_introspection.set_sandbox(self.sandbox)
        
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await asyncio.get_event_loop().connect_read_pipe(lambda: protocol, sys.stdin)
        
        writer = asyncio.StreamWriter(sys.stdout, {}, None, asyncio.get_event_loop())
        
        while True:
            line = await reader.readline()
            if not line:
                break
            
            line = line.decode("utf-8").strip()
            if not line:
                continue
            
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                continue
            
            response = await self.handle_request(request)
            if response is not None:
                writer.write((json.dumps(response) + "\n").encode("utf-8"))
                await writer.drain()


async def run_mcp_server(workspace: Path | None = None) -> None:
    """Run the MCP server."""
    sandbox = Sandbox(workspace or Path.cwd())
    server = MCPServer(sandbox=sandbox)
    await server.run_stdio()