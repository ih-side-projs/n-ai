"""
CLI entry point for `nai mcp` subcommand.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable
from pathlib import Path

from .mcp_server import run_mcp_server


def mcp_main(argv: list[str] | None = None) -> int:
    """Entry point for `nai mcp` command."""
    parser = argparse.ArgumentParser(prog="nai mcp", description="Run the MCP server for tool access")
    parser.add_argument("-w", "--workspace", default=".", help="Workspace root directory")
    parser.add_argument("--http", action="store_true", help="Run HTTP server instead of stdio (not yet implemented)")
    parser.add_argument("--port", type=int, default=8081, help="HTTP port (if --http)")
    
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    
    workspace = Path(args.workspace).resolve()
    
    if args.http:
        print("ERROR: HTTP mode not yet implemented", file=sys.stderr)
        return 1
    
    try:
        asyncio.run(run_mcp_server(workspace))
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    
    return 0


# Register the subcommand
def register_mcp_subcommand(register_fn: Callable[[str, Callable], None]) -> None:
    """Register the mcp subcommand with the main CLI."""
    register_fn("mcp", mcp_main)