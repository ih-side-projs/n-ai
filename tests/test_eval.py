"""Tests for WP9 evaluation harness."""

import tempfile
from pathlib import Path

from nai.eval.benchmarks import BenchmarkSpec, BenchmarkSuite, _parse_benchmark_spec, load_benchmark_suite
from nai.eval.harness import BenchmarkHarness, BenchmarkResult
from nai.eval.reports import generate_report
from nai.eval.stub_server import StubModelServer, StubServerConfig


class TestBenchmarkSpec:
    def test_parse_benchmark_spec(self):
        content = """# Test Benchmark
Description: A test benchmark
Archetype: cli
Size: small
"""
        spec = _parse_benchmark_spec(Path("test.md"), content)
        assert spec.id == "test"
        assert spec.name == "Test Benchmark"
        assert spec.archetype == "cli"
        assert spec.size_grade == "small"
    
    def test_complexity_score(self):
        content = "# Test\n\n" + "class Foo:\n    def bar(self):\n        pass\n" * 10
        spec = _parse_benchmark_spec(Path("test.md"), content)
        assert spec.complexity_score > 0


class TestBenchmarkSuite:
    def test_add_benchmark(self):
        suite = BenchmarkSuite("test", "Test suite")
        spec = BenchmarkSpec("test1", "Test 1", "desc", "cli", "small", Path("test.md"))
        suite.add_benchmark(spec)
        assert len(suite) == 1
    
    def test_filter(self):
        suite = BenchmarkSuite("test", "Test suite")
        suite.add_benchmark(BenchmarkSpec("a", "A", "d", "cli", "small", Path("a.md")))
        suite.add_benchmark(BenchmarkSpec("b", "B", "d", "web", "medium", Path("b.md")))
        
        filtered = suite.filter(archetype="cli")
        assert len(filtered) == 1
        assert filtered.benchmarks[0].id == "a"
        
        filtered = suite.filter(size_grade="medium")
        assert len(filtered) == 1
        assert filtered.benchmarks[0].id == "b"


class TestLoadBenchmarkSuite:
    def test_load_from_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            (tmpdir / "bench1.md").write_text("# Bench 1\nArchetype: cli\nSize: small\n")
            (tmpdir / "bench2.md").write_text("# Bench 2\nArchetype: web\nSize: large\n")
            
            suite = load_benchmark_suite(tmpdir)
            assert len(suite) == 2
            assert suite.benchmarks[0].id == "bench1"
            assert suite.benchmarks[1].id == "bench2"


class TestStubModelServer:
    def test_create_app(self):
        config = StubServerConfig(models=["test-model"])
        server = StubModelServer(config)
        app = server.create_app()
        assert app is not None
    
    def test_scripted_responses(self):
        config = StubServerConfig()
        server = StubModelServer(config)
        server.add_scripted_response({
            "id": "test",
            "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "scripted"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        })
        
        server.create_app()
        assert len(server.scripted_responses) == 1
    
    def test_call_history(self):
        config = StubServerConfig()
        server = StubModelServer(config)
        assert server.get_call_history() == []
        server.request_count = 1
        server.call_history.append({"id": 1, "model": "test"})
        assert len(server.get_call_history()) == 1
    
    def test_reset(self):
        config = StubServerConfig()
        server = StubModelServer(config)
        server.request_count = 5
        server.call_history.append({"id": 1})
        server.reset()
        assert server.request_count == 0
        assert server.call_history == []


class TestReports:
    def test_generate_report(self):
        results = [
            BenchmarkResult(
                benchmark_id="test1",
                benchmark_name="Test 1",
                model_profile="default",
                model_id="test-model",
                quantization="q4",
                started_at="2024-01-01T00:00:00Z",
                completed_at="2024-01-01T00:01:00Z",
                duration_seconds=60.0,
                success=True,
                error=None,
            ),
            BenchmarkResult(
                benchmark_id="test2",
                benchmark_name="Test 2",
                model_profile="default",
                model_id="test-model",
                quantization="q4",
                started_at="2024-01-01T00:01:00Z",
                completed_at="2024-01-01T00:02:00Z",
                duration_seconds=60.0,
                success=False,
                error="Timeout",
            ),
        ]
        
        report = generate_report(
            results=results,
            model_profile="default",
            model_id="test-model",
            quantization="q4",
            benchmark_suite="test",
            nai_version="1.0.0",
            git_commit="abc123"
        )
        
        assert report.summary["total"] == 2
        assert report.summary["passed"] == 1
        assert report.summary["failed"] == 1
        assert report.summary["success_rate"] == 0.5
    
    def test_report_json(self):
        results = [
            BenchmarkResult(
                benchmark_id="test1",
                benchmark_name="Test 1",
                model_profile="default",
                model_id="test-model",
                quantization="q4",
                started_at="2024-01-01T00:00:00Z",
                completed_at="2024-01-01T00:01:00Z",
                duration_seconds=60.0,
                success=True,
            ),
        ]
        
        report = generate_report(
            results=results,
            model_profile="default",
            model_id="test-model",
            quantization="q4",
            benchmark_suite="test",
            nai_version="1.0.0",
            git_commit="abc123"
        )
        
        json_str = report.to_json()
        assert "test1" in json_str
        assert "success" in json_str
    
    def test_report_markdown(self):
        results = [
            BenchmarkResult(
                benchmark_id="test1",
                benchmark_name="Test 1",
                model_profile="default",
                model_id="test-model",
                quantization="q4",
                started_at="2024-01-01T00:00:00Z",
                completed_at="2024-01-01T00:01:00Z",
                duration_seconds=60.0,
                success=True,
            ),
        ]
        
        report = generate_report(
            results=results,
            model_profile="default",
            model_id="test-model",
            quantization="q4",
            benchmark_suite="test",
            nai_version="1.0.0",
            git_commit="abc123"
        )
        
        md = report.to_markdown()
        assert "# Benchmark Report" in md
        assert "Test 1" in md
        assert "✅" in md


class TestBenchmarkHarness:
    def test_harness_creation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            benchmarks_dir = tmpdir / "benchmarks"
            benchmarks_dir.mkdir()
            output_dir = tmpdir / "output"
            
            harness = BenchmarkHarness(benchmarks_dir, output_dir)
            assert harness.benchmarks_dir == benchmarks_dir
            assert harness.output_dir == output_dir
    
    def test_discover_benchmarks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            benchmarks_dir = tmpdir / "benchmarks"
            benchmarks_dir.mkdir()
            output_dir = tmpdir / "output"
            
            (benchmarks_dir / "bench1.md").write_text("# Bench 1")
            (benchmarks_dir / "bench2.md").write_text("# Bench 2")
            (benchmarks_dir / "not_a_bench.txt").write_text("not a benchmark")
            
            harness = BenchmarkHarness(benchmarks_dir, output_dir)
            benchmarks = harness.discover_benchmarks()
            assert len(benchmarks) == 2
            assert all(b.suffix == ".md" for b in benchmarks)