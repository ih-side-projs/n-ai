"""
Project Introspection tool family (stdlib only).

Read-only tools for listing spec nodes, reading contract stubs, and reading requirements.
All tools return human-readable strings; errors start with "ERROR:".
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..registry import register_tool
from ..sandbox import Sandbox


# Global sandbox instance (set by the MCP server or CLI)
_sandbox: Sandbox | None = None


def set_sandbox(sandbox: Sandbox) -> None:
    """Set the global sandbox for tool execution."""
    global _sandbox
    _sandbox = sandbox


def _get_sandbox() -> Sandbox:
    """Get the sandbox, creating a default if needed."""
    global _sandbox
    if _sandbox is None:
        _sandbox = Sandbox(Path.cwd())
    return _sandbox


@register_tool(
    name="list_spec_nodes",
    family="project_introspection",
    description="List all specification nodes in the project.",
    side_effects=False,
    timeout_seconds=10.0,
    returns_description="JSON array of node summaries or error message",
)
def list_spec_nodes(project_root: str = ".", status_filter: str | None = None) -> str:
    """
    List all specification nodes in the project.
    
    Args:
        project_root: Relative path to the project root (default ".")
        status_filter: Optional filter by status (e.g., "approved", "pending", "implemented")
    """
    sandbox = _get_sandbox()
    try:
        root = sandbox.validate_path(project_root, write=False)
        
        # Look for spec files
        spec_files = list(root.glob("SPEC*.md"))
        if not spec_files:
            return "ERROR: no SPEC*.md files found in project root"
        
        nodes = []
        for spec_file in spec_files:
            # Parse spec file for nodes (simplified - looks for ## Node: markers)
            content = spec_file.read_text(encoding="utf-8")
            lines = content.split("\n")
            
            current_node = None
            for line in lines:
                if line.startswith("## Node:") or line.startswith("### Node:"):
                    if current_node:
                        nodes.append(current_node)
                    parts = line.split(":", 1)
                    if len(parts) > 1:
                        current_node = {
                            "id": parts[1].strip(),
                            "file": str(spec_file.relative_to(root)),
                            "status": "unknown",
                        }
                elif current_node and line.startswith("Status:"):
                    current_node["status"] = line.split(":", 1)[1].strip()
            
            if current_node:
                nodes.append(current_node)
        
        # Filter by status if provided
        if status_filter:
            nodes = [n for n in nodes if n.get("status", "").lower() == status_filter.lower()]
        
        return json.dumps(nodes, indent=2)
        
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"


@register_tool(
    name="read_contract_stub",
    family="project_introspection",
    description="Read a contract stub for a specification node.",
    side_effects=False,
    timeout_seconds=10.0,
    returns_description="Contract stub content or error message",
)
def read_contract_stub(project_root: str = ".", node_id: str = "") -> str:
    """
    Read a contract stub for a specification node.
    
    Args:
        project_root: Relative path to the project root (default ".")
        node_id: The node ID to read the contract for (required)
    """
    sandbox = _get_sandbox()
    try:
        if not node_id:
            return "ERROR: node_id is required"
        
        root = sandbox.validate_path(project_root, write=False)
        
        # Look for contract stub files
        # Convention: contracts/{node_id}.json or .agent/contracts/{node_id}.json
        contract_paths = [
            root / "contracts" / f"{node_id}.json",
            root / ".agent" / "contracts" / f"{node_id}.json",
        ]
        
        for contract_path in contract_paths:
            if contract_path.exists():
                content = contract_path.read_text(encoding="utf-8")
                return content
        
        return f"ERROR: contract stub not found for node: {node_id}"
        
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"


@register_tool(
    name="read_requirements",
    family="project_introspection",
    description="Read the requirements for a specification node.",
    side_effects=False,
    timeout_seconds=10.0,
    returns_description="Requirements list as JSON or error message",
)
def read_requirements(project_root: str = ".", node_id: str = "") -> str:
    """
    Read the requirements for a specification node.
    
    Args:
        project_root: Relative path to the project root (default ".")
        node_id: The node ID to read requirements for (required)
    """
    sandbox = _get_sandbox()
    try:
        if not node_id:
            return "ERROR: node_id is required"
        
        root = sandbox.validate_path(project_root, write=False)
        
        # Look for requirements in spec files or separate requirements files
        req_paths = [
            root / "requirements" / f"{node_id}.json",
            root / ".agent" / "requirements" / f"{node_id}.json",
        ]
        
        for req_path in req_paths:
            if req_path.exists():
                content = req_path.read_text(encoding="utf-8")
                return content
        
        # Fallback: try to extract from spec file
        spec_files = list(root.glob("SPEC*.md"))
        for spec_file in spec_files:
            content = spec_file.read_text(encoding="utf-8")
            if f"## Node: {node_id}" in content or f"### Node: {node_id}" in content:
                # Extract requirements section
                lines = content.split("\n")
                in_req = False
                reqs = []
                for line in lines:
                    if "## Requirements" in line or "### Requirements" in line:
                        in_req = True
                        continue
                    if in_req and line.startswith("## ") and "Requirements" not in line:
                        break
                    if in_req and line.strip().startswith("- [ ]") or line.strip().startswith("- [x]"):
                        reqs.append(line.strip())
                
                if reqs:
                    return json.dumps({"node_id": node_id, "requirements": reqs}, indent=2)
        
        return f"ERROR: requirements not found for node: {node_id}"
        
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"