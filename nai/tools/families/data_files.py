"""
Data and Files tool family (stdlib only).

Tools for CSV/JSON read/validate/transform, zip/unzip, and diff.
All tools return human-readable strings; errors start with "ERROR:".
"""
from __future__ import annotations

import csv
import difflib
import json
import zipfile
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
    name="read_csv",
    family="data_files",
    description="Read a CSV file and return its contents as JSON. Optionally limit rows.",
    side_effects=False,
    timeout_seconds=10.0,
    returns_description="JSON array of objects (one per row) or error message",
)
def read_csv(path: str, max_rows: int = 1000, delimiter: str = ",") -> str:
    """
    Read a CSV file.
    
    Args:
        path: Relative path to the CSV file
        max_rows: Maximum number of rows to read (default 1000)
        delimiter: CSV delimiter (default ",")
    """
    sandbox = _get_sandbox()
    try:
        file_path = sandbox.validate_path(path, write=False)
        
        if not file_path.exists():
            return f"ERROR: file not found: {path}"
        
        rows = []
        with file_path.open("r", encoding="utf-8") as f:
            reader = csv.DictReader(f, delimiter=delimiter)
            for i, row in enumerate(reader):
                if i >= max_rows:
                    break
                rows.append(row)
        
        result = {
            "columns": reader.fieldnames or [],
            "row_count": len(rows),
            "truncated": i >= max_rows if rows else False,
            "data": rows,
        }
        return json.dumps(result, indent=2)
        
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"


@register_tool(
    name="read_json",
    family="data_files",
    description="Read a JSON file and return its contents.",
    side_effects=False,
    timeout_seconds=10.0,
    returns_description="Parsed JSON content or error message",
)
def read_json(path: str) -> str:
    """
    Read a JSON file.
    
    Args:
        path: Relative path to the JSON file
    """
    sandbox = _get_sandbox()
    try:
        file_path = sandbox.validate_path(path, write=False)
        
        if not file_path.exists():
            return f"ERROR: file not found: {path}"
        
        content = file_path.read_text(encoding="utf-8")
        data = json.loads(content)
        return json.dumps(data, indent=2)
        
    except json.JSONDecodeError as e:
        return f"ERROR: invalid JSON: {e}"
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"


@register_tool(
    name="write_json",
    family="data_files",
    description="Write data as JSON to a file.",
    side_effects=True,
    timeout_seconds=10.0,
    returns_description="Success message with path and byte count or error message",
)
def write_json(path: str, data: Any, indent: int = 2) -> str:
    """
    Write data as JSON to a file.
    
    Args:
        path: Relative path to write the JSON file
        data: Data to serialize as JSON (dict, list, etc.)
        indent: JSON indentation (default 2)
    """
    sandbox = _get_sandbox()
    try:
        content = json.dumps(data, indent=indent, ensure_ascii=False)
        return sandbox.write_file(path, content)
        
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"


@register_tool(
    name="diff_text",
    family="data_files",
    description="Compute a unified diff between two text files.",
    side_effects=False,
    timeout_seconds=10.0,
    returns_description="Unified diff output or error message",
)
def diff_text(path1: str, path2: str, context_lines: int = 3) -> str:
    """
    Compute a unified diff between two text files.
    
    Args:
        path1: Relative path to the first file
        path2: Relative path to the second file
        context_lines: Number of context lines (default 3)
    """
    sandbox = _get_sandbox()
    try:
        file1 = sandbox.validate_path(path1, write=False)
        file2 = sandbox.validate_path(path2, write=False)
        
        if not file1.exists():
            return f"ERROR: file not found: {path1}"
        if not file2.exists():
            return f"ERROR: file not found: {path2}"
        
        text1 = file1.read_text(encoding="utf-8").splitlines(keepends=True)
        text2 = file2.read_text(encoding="utf-8").splitlines(keepends=True)
        
        diff = difflib.unified_diff(text1, text2, fromfile=str(path1), tofile=str(path2), n=context_lines)
        result = "".join(diff)
        
        if not result:
            return "Files are identical."
        
        return sandbox.limit_output(result)
        
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"


@register_tool(
    name="zip_files",
    family="data_files",
    description="Create a ZIP archive from a list of files.",
    side_effects=True,
    timeout_seconds=30.0,
    returns_description="Success message with archive path and size or error message",
)
def zip_files(output_path: str, file_paths: list[str]) -> str:
    """
    Create a ZIP archive from a list of files.
    
    Args:
        output_path: Relative path for the output ZIP file
        file_paths: List of relative paths to include in the archive
    """
    sandbox = _get_sandbox()
    try:
        output = sandbox.validate_path(output_path, write=True)
        
        total_size = 0
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
            for file_path in file_paths:
                src = sandbox.validate_path(file_path, write=False)
                if not src.exists():
                    return f"ERROR: file not found: {file_path}"
                
                # Use relative path within archive
                arcname = src.relative_to(sandbox.workspace_root)
                zf.write(src, arcname)
                total_size += src.stat().st_size
        
        return f"Created ZIP archive: {output.relative_to(sandbox.workspace_root)} ({total_size} bytes, {len(file_paths)} files)"
        
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"


@register_tool(
    name="unzip_files",
    family="data_files",
    description="Extract a ZIP archive to a directory.",
    side_effects=True,
    timeout_seconds=30.0,
    returns_description="Success message with extraction path and file count or error message",
)
def unzip_files(zip_path: str, extract_dir: str) -> str:
    """
    Extract a ZIP archive to a directory.
    
    Args:
        zip_path: Relative path to the ZIP file
        extract_dir: Relative directory to extract to
    """
    sandbox = _get_sandbox()
    try:
        zip_file = sandbox.validate_path(zip_path, write=False)
        extract_to = sandbox.validate_path(extract_dir, write=True)
        
        if not zip_file.exists():
            return f"ERROR: zip file not found: {zip_path}"
        
        extracted = 0
        with zipfile.ZipFile(zip_file, "r") as zf:
            # Validate all paths are safe before extracting
            for member in zf.namelist():
                member_path = (extract_to / member).resolve()
                try:
                    member_path.relative_to(extract_to.resolve())
                except ValueError:
                    return f"ERROR: zip contains path traversal attempt: {member}"
            
            zf.extractall(extract_to)
            extracted = len(zf.namelist())
        
        return f"Extracted {extracted} files to {extract_to.relative_to(sandbox.workspace_root)}"
        
    except zipfile.BadZipFile:
        return "ERROR: invalid or corrupted ZIP file"
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"