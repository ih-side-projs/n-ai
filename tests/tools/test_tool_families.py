"""
Tests for the Tool Families (WP6).
"""
from __future__ import annotations

import json
import zipfile

import pytest

from nai.tools.families.data_files import (
    diff_text,
    read_csv,
    read_json,
    unzip_files,
    write_json,
    zip_files,
)
from nai.tools.families.project_introspection import (
    list_spec_nodes,
    read_contract_stub,
    read_requirements,
)
from nai.tools.sandbox import Sandbox


@pytest.fixture
def sandbox(tmp_path):
    """Create a sandbox for testing via the per-call contextvar."""
    from nai.tools.sandbox import _current_sandbox

    s = Sandbox(tmp_path)
    token = _current_sandbox.set(s)
    yield s
    _current_sandbox.reset(token)


class TestDataFiles:
    """Tests for data_files tool family."""
    
    def test_read_csv(self, sandbox, tmp_path):
        """Read a CSV file."""
        csv_file = tmp_path / "test.csv"
        csv_file.write_text("name,age\nAlice,30\nBob,25\n")
        
        result = read_csv("test.csv")
        data = json.loads(result)
        
        assert data["columns"] == ["name", "age"]
        assert data["row_count"] == 2
        assert data["data"][0]["name"] == "Alice"
        assert data["data"][1]["age"] == "25"
    
    def test_read_csv_with_delimiter(self, sandbox, tmp_path):
        """Read CSV with custom delimiter."""
        csv_file = tmp_path / "test.tsv"
        csv_file.write_text("name\tage\nAlice\t30\n")
        
        result = read_csv("test.tsv", delimiter="\t")
        data = json.loads(result)
        
        assert data["columns"] == ["name", "age"]
        assert data["row_count"] == 1
    
    def test_read_csv_not_found(self, sandbox):
        """Read non-existent CSV returns ERROR."""
        result = read_csv("nonexistent.csv")
        assert result.startswith("ERROR:")
    
    def test_read_json(self, sandbox, tmp_path):
        """Read a JSON file."""
        json_file = tmp_path / "test.json"
        json_file.write_text('{"key": "value", "num": 42}')
        
        result = read_json("test.json")
        data = json.loads(result)
        
        assert data["key"] == "value"
        assert data["num"] == 42
    
    def test_read_json_invalid(self, sandbox, tmp_path):
        """Read invalid JSON returns ERROR."""
        json_file = tmp_path / "bad.json"
        json_file.write_text("{ invalid }")
        
        result = read_json("bad.json")
        assert result.startswith("ERROR:")
    
    def test_write_json(self, sandbox, tmp_path):
        """Write JSON to a file."""
        result = write_json("outputs/data.json", {"key": "value", "list": [1, 2, 3]})
        
        assert "Written" in result
        content = (tmp_path / "outputs" / "data.json").read_text()
        data = json.loads(content)
        assert data["key"] == "value"
        assert data["list"] == [1, 2, 3]
    
    def test_diff_text(self, sandbox, tmp_path):
        """Diff two text files."""
        file1 = tmp_path / "file1.txt"
        file2 = tmp_path / "file2.txt"
        file1.write_text("line 1\nline 2\nline 3\n")
        file2.write_text("line 1\nline 2 modified\nline 3\n")
        
        result = diff_text("file1.txt", "file2.txt")
        
        assert "line 2" in result
        assert "modified" in result
    
    def test_diff_text_identical(self, sandbox, tmp_path):
        """Diff identical files."""
        file1 = tmp_path / "file1.txt"
        file2 = tmp_path / "file2.txt"
        file1.write_text("same\n")
        file2.write_text("same\n")
        
        result = diff_text("file1.txt", "file2.txt")
        assert result == "Files are identical."
    
    def test_zip_files(self, sandbox, tmp_path):
        """Create a ZIP archive."""
        file1 = tmp_path / "file1.txt"
        file2 = tmp_path / "file2.txt"
        file1.write_text("content 1")
        file2.write_text("content 2")
        
        result = zip_files("outputs/archive.zip", ["file1.txt", "file2.txt"])
        
        assert "Created ZIP archive" in result
        assert (tmp_path / "outputs" / "archive.zip").exists()
        
        # Verify contents
        with zipfile.ZipFile(tmp_path / "outputs" / "archive.zip", "r") as zf:
            assert set(zf.namelist()) == {"file1.txt", "file2.txt"}
    
    def test_zip_files_missing(self, sandbox):
        """Zip with missing file returns ERROR."""
        result = zip_files("outputs/archive.zip", ["missing.txt"])
        assert result.startswith("ERROR:")
    
    def test_unzip_files(self, sandbox, tmp_path):
        """Extract a ZIP archive."""
        # Create a zip first
        zip_path = tmp_path / "test.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("extracted1.txt", "content 1")
            zf.writestr("extracted2.txt", "content 2")
        
        result = unzip_files("test.zip", "outputs/extracted")
        
        assert "Extracted 2 files" in result
        assert (tmp_path / "outputs" / "extracted" / "extracted1.txt").read_text() == "content 1"
        assert (tmp_path / "outputs" / "extracted" / "extracted2.txt").read_text() == "content 2"
    
    def test_unzip_files_bad_zip(self, sandbox, tmp_path):
        """Extract bad ZIP returns ERROR."""
        bad_zip = tmp_path / "bad.zip"
        bad_zip.write_text("not a zip")
        
        result = unzip_files("bad.zip", "outputs/extracted")
        assert result.startswith("ERROR:")


class TestProjectIntrospection:
    """Tests for project_introspection tool family."""
    
    def test_list_spec_nodes(self, sandbox, tmp_path):
        """List spec nodes."""
        spec_file = tmp_path / "SPEC.md"
        spec_file.write_text("""# Spec

## Node: node1
Status: approved

## Node: node2
Status: pending
""")
        
        result = list_spec_nodes(".")
        data = json.loads(result)
        
        assert len(data) == 2
        assert data[0]["id"] == "node1"
        assert data[0]["status"] == "approved"
        assert data[1]["id"] == "node2"
        assert data[1]["status"] == "pending"
    
    def test_list_spec_nodes_filter(self, sandbox, tmp_path):
        """List spec nodes with status filter."""
        spec_file = tmp_path / "SPEC.md"
        spec_file.write_text("""# Spec

## Node: node1
Status: approved

## Node: node2
Status: pending
""")
        
        result = list_spec_nodes(".", status_filter="approved")
        data = json.loads(result)
        
        assert len(data) == 1
        assert data[0]["id"] == "node1"
    
    def test_read_contract_stub(self, sandbox, tmp_path):
        """Read contract stub."""
        contracts_dir = tmp_path / "contracts"
        contracts_dir.mkdir()
        (contracts_dir / "node1.json").write_text('{"interface": "test"}')
        
        result = read_contract_stub(".", "node1")
        data = json.loads(result)
        
        assert data["interface"] == "test"
    
    def test_read_contract_stub_not_found(self, sandbox):
        """Read non-existent contract returns ERROR."""
        result = read_contract_stub(".", "nonexistent")
        assert result.startswith("ERROR:")
    
    def test_read_contract_stub_missing_id(self, sandbox):
        """Read contract without node_id returns ERROR."""
        result = read_contract_stub(".", "")
        assert result.startswith("ERROR:")
    
    def test_read_requirements(self, sandbox, tmp_path):
        """Read requirements."""
        req_dir = tmp_path / "requirements"
        req_dir.mkdir()
        (req_dir / "node1.json").write_text('["req1", "req2"]')
        
        result = read_requirements(".", "node1")
        data = json.loads(result)
        
        assert data == ["req1", "req2"]
    
    def test_read_requirements_not_found(self, sandbox):
        """Read non-existent requirements returns ERROR."""
        result = read_requirements(".", "nonexistent")
        assert result.startswith("ERROR:")