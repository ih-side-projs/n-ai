"""
Tests for the Sandbox (WP6).
"""
from __future__ import annotations

import pytest

from nai.tools.sandbox import Sandbox, SandboxError


class TestSandbox:
    """Tests for the Sandbox class."""
    
    def test_sandbox_creation(self, tmp_path):
        """Create a sandbox."""
        sandbox = Sandbox(tmp_path)
        assert sandbox.workspace_root == tmp_path.resolve()
        assert (tmp_path / "outputs").exists()
    
    def test_validate_path_read(self, tmp_path):
        """Validate path for reading."""
        sandbox = Sandbox(tmp_path)
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello")
        
        validated = sandbox.validate_path("test.txt", write=False)
        assert validated == test_file.resolve()
    
    def test_validate_path_write(self, tmp_path):
        """Validate path for writing."""
        sandbox = Sandbox(tmp_path)
        validated = sandbox.validate_path("outputs/new.txt", write=True)
        assert validated == (tmp_path / "outputs" / "new.txt").resolve()
    
    def test_validate_path_write_outside_outputs_fails(self, tmp_path):
        """Writing outside allowed dirs should fail."""
        sandbox = Sandbox(tmp_path)
        with pytest.raises(SandboxError, match="not writable"):
            sandbox.validate_path("outside.txt", write=True)
    
    def test_validate_path_escape_fails(self, tmp_path):
        """Path escape should fail."""
        sandbox = Sandbox(tmp_path)
        with pytest.raises(SandboxError):
            sandbox.validate_path("../etc/passwd", write=False)
    
    def test_read_file(self, tmp_path):
        """Read a file."""
        sandbox = Sandbox(tmp_path)
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello world")
        
        content = sandbox.read_file("test.txt")
        assert content == "hello world"
    
    def test_read_file_not_found(self, tmp_path):
        """Read non-existent file returns ERROR."""
        sandbox = Sandbox(tmp_path)
        with pytest.raises(SandboxError, match="not found"):
            sandbox.read_file("nonexistent.txt")
    
    def test_write_file(self, tmp_path):
        """Write a file."""
        sandbox = Sandbox(tmp_path)
        result = sandbox.write_file("outputs/new.txt", "hello")
        assert "Written" in result
        assert (tmp_path / "outputs" / "new.txt").read_text() == "hello"
    
    def test_write_file_creates_dirs(self, tmp_path):
        """Write file creates parent directories."""
        sandbox = Sandbox(tmp_path)
        sandbox.write_file("outputs/subdir/file.txt", "content")
        assert (tmp_path / "outputs" / "subdir" / "file.txt").exists()
    
    def test_write_file_outside_fails(self, tmp_path):
        """Write outside allowed dirs fails."""
        sandbox = Sandbox(tmp_path)
        with pytest.raises(SandboxError, match="not writable"):
            sandbox.write_file("outside.txt", "content")
    
    def test_limit_output(self, tmp_path):
        """Limit output size."""
        sandbox = Sandbox(tmp_path, max_output_bytes=50)
        long_text = "x" * 100
        limited = sandbox.limit_output(long_text)
        assert len(limited.encode()) <= 50 + len(b"... [output truncated")
        assert "truncated" in limited
    
    def test_run_with_timeout(self, tmp_path):
        """Run function with timeout."""
        sandbox = Sandbox(tmp_path, default_timeout=1.0)
        
        def slow_fn():
            import time
            time.sleep(0.1)
            return "done"
        
        result = sandbox.run_with_timeout(slow_fn, timeout=0.5)
        assert result == "done"
    
    def test_run_with_timeout_expires(self, tmp_path):
        """Timeout should raise SandboxError."""
        sandbox = Sandbox(tmp_path, default_timeout=0.1)
        
        def slow_fn():
            import time
            time.sleep(1)
            return "done"
        
        with pytest.raises(SandboxError, match="timeout"):
            sandbox.run_with_timeout(slow_fn, timeout=0.1)