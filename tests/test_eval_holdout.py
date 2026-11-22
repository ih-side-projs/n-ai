"""Tests for Pass 6 evaluation harness: hold-out tests and v2 pipeline mode."""
from __future__ import annotations

import json

from nai.eval.harness import BenchmarkHarness, BenchmarkResult
from nai.eval.holdout import HoldoutResult, discover_holdout, holdout_to_dict, run_holdout


def test_discover_holdout(tmp_path):
    spec = tmp_path / "bench.md"
    (tmp_path / "holdouts").mkdir()
    spec.write_text("# Benchmark\nHoldout: holdouts/\n")
    assert discover_holdout(spec) == (tmp_path / "holdouts").resolve()
    spec.write_text("# Benchmark\n")
    assert discover_holdout(spec) is None


def test_run_holdout_pass(tmp_path):
    holdout = tmp_path / "holdout"
    holdout.mkdir()
    (holdout / "test_output.py").write_text(
        "from pathlib import Path\n\ndef test_file_exists():\n"
        "    assert (Path.cwd() / 'output.txt').exists()\n"
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "output.txt").write_text("hello")
    result = run_holdout(holdout, workspace, timeout=60)
    assert result.applied
    assert result.total == 1
    assert result.passed == 1
    assert result.pass_rate == 1.0
    assert not result.errors


def test_run_holdout_fail(tmp_path):
    holdout = tmp_path / "holdout"
    holdout.mkdir()
    (holdout / "test_fail.py").write_text("def test_fail():\n    assert False\n")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    result = run_holdout(holdout, workspace, timeout=60)
    assert result.passed == 0 and result.failed == 1
    assert result.pass_rate == 0.0


def test_harness_v2_default(tmp_path):
    h = BenchmarkHarness(tmp_path, tmp_path / "out")
    assert h.pipeline_mode == "v2"
    r = BenchmarkResult("id", "name", "prof", "model", None, "2026-01-01T00:00:00Z")
    assert r.pipeline_mode == "v2"


def test_holdout_serialization():
    r = HoldoutResult(applied=True, total=2, passed=1, failed=1, errors=["x"])
    d = holdout_to_dict(r)
    assert d["pass_rate"] == 0.5
    assert d["errors"] == ["x"]
    assert isinstance(json.dumps(d), str)


def test_cli_mode_flag():
    import sys

    from nai.eval.cli import bench_main

    old = sys.argv
    try:
        sys.argv = ["nai", "bench"]
        assert bench_main(["--list", "--mode", "v1"]) == 0
    finally:
        sys.argv = old
