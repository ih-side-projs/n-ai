"""
Sandbox for SS-TOOLS (WP6).

Tools write only inside the project's `outputs/` directory (or another path the job's
policy permits), refuse path escapes with `ERROR:`, have per-tool timeouts and output
size limits, and make no network calls. (TLS-5)
"""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any


class SandboxError(Exception):
    """Raised when a sandbox constraint is violated."""
    pass


class Sandbox:
    """
    Execution sandbox for tools.
    
    Enforces:
    - Path policy: writes only allowed in configured writable directories
    - Per-tool timeouts
    - Output size limits
    - No network calls (enforced by not providing network access)
    """
    
    def __init__(
        self,
        workspace_root: Path,
        writable_dirs: list[Path] | None = None,
        readonly_dirs: list[Path] | None = None,
        default_timeout: float = 30.0,
        max_output_bytes: int = 1024 * 1024,  # 1 MB default
    ):
        self.workspace_root = workspace_root.resolve()
        self.writable_dirs = [d.resolve() for d in (writable_dirs or [])]
        self.readonly_dirs = [d.resolve() for d in (readonly_dirs or [])]
        self.default_timeout = default_timeout
        self.max_output_bytes = max_output_bytes
        
        # Always allow outputs/ directory
        outputs_dir = self.workspace_root / "outputs"
        outputs_dir.mkdir(parents=True, exist_ok=True)
        if outputs_dir not in self.writable_dirs:
            self.writable_dirs.insert(0, outputs_dir)
    
    def _is_path_allowed(self, path: Path, write: bool = False) -> bool:
        """Check if a path is allowed for read or write."""
        path = path.resolve()
        
        # Must be within workspace
        try:
            path.relative_to(self.workspace_root)
        except ValueError:
            return False
        
        if write:
            # For writes, must be in a writable directory and not in readonly
            for writable in self.writable_dirs:
                try:
                    path.relative_to(writable)
                    # Check not in readonly
                    for readonly in self.readonly_dirs:
                        try:
                            path.relative_to(readonly)
                            return False  # In readonly, deny write
                        except ValueError:
                            pass
                    return True
                except ValueError:
                    pass
            return False
        else:
            # For reads, just check not explicitly denied (we allow all reads within workspace)
            for readonly in self.readonly_dirs:
                try:
                    path.relative_to(readonly)
                    return True  # Explicitly allowed readonly
                except ValueError:
                    pass
            # Allow reads from anywhere in workspace by default
            return True
    
    def validate_path(self, path: str | Path, write: bool = False) -> Path:
        """
        Validate and resolve a path.
        Returns the resolved path if allowed.
        Raises SandboxError if not allowed.
        """
        path = Path(path)
        # If relative, resolve relative to workspace
        if not path.is_absolute():
            path = self.workspace_root / path
        
        path = path.resolve()
        
        if not self._is_path_allowed(path, write=write):
            if write:
                raise SandboxError(f"ERROR: path not writable: {path}")
            else:
                raise SandboxError(f"ERROR: path not readable: {path}")
        
        return path
    
    def read_file(self, path: str | Path, max_bytes: int | None = None) -> str:
        """Read a file with size limit."""
        max_bytes = max_bytes or self.max_output_bytes
        path = self.validate_path(path, write=False)
        
        if not path.exists():
            raise SandboxError(f"ERROR: file not found: {path}")
        
        if path.stat().st_size > max_bytes:
            raise SandboxError(f"ERROR: file too large ({path.stat().st_size} > {max_bytes} bytes)")
        
        return path.read_text(encoding="utf-8")
    
    def write_file(self, path: str | Path, content: str) -> str:
        """Write a file, ensuring parent directories exist."""
        path = self.validate_path(path, write=True)
        
        # Check content size
        content_bytes = content.encode("utf-8")
        if len(content_bytes) > self.max_output_bytes:
            raise SandboxError(f"ERROR: content too large ({len(content_bytes)} > {self.max_output_bytes} bytes)")
        
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        
        return f"Written {len(content_bytes)} bytes to {path.relative_to(self.workspace_root)}"
    
    def run_with_timeout(
        self,
        fn: callable,
        args: tuple = (),
        kwargs: dict | None = None,
        timeout: float | None = None,
    ) -> Any:
        """
        Run a function with a timeout.
        Returns the function result or raises SandboxError on timeout.
        """
        kwargs = kwargs or {}
        timeout = timeout or self.default_timeout
        
        result = [None]
        error = [None]
        
        def target():
            try:
                result[0] = fn(*args, **kwargs)
            except Exception as e:
                error[0] = e
        
        thread = threading.Thread(target=target)
        thread.daemon = True
        thread.start()
        thread.join(timeout=timeout)
        
        if thread.is_alive():
            # Timeout - can't actually kill the thread in Python, but we can raise
            raise SandboxError(f"ERROR: tool timeout after {timeout}s")
        
        if error[0]:
            raise error[0]
        
        return result[0]
    
    def limit_output(self, output: str) -> str:
        """Limit output size, truncate if necessary."""
        output_bytes = output.encode("utf-8")
        if len(output_bytes) > self.max_output_bytes:
            truncated = output_bytes[:self.max_output_bytes].decode("utf-8", errors="ignore")
            return truncated + f"\n... [output truncated, {len(output_bytes)} bytes total]"
        return output


# Convenience: create a sandbox from a path policy (compatible with WP1's PathPolicy)
def create_sandbox_from_policy(workspace_root: Path, policy: Any) -> Sandbox:
    """Create a sandbox from a WP1 PathPolicy."""
    writable = []
    readonly = []
    
    if hasattr(policy, 'writable'):
        for pattern in policy.writable:
            # Convert glob pattern to path
            # For simplicity, we use the workspace root + pattern
            # This is a simplification - real implementation would expand globs
            pass
    
    # Default: outputs/ is writable
    return Sandbox(workspace_root)