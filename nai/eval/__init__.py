"""nai.eval: Evaluation harness and benchmark specs (WP9 / SS-EVAL)."""

from .benchmarks import BenchmarkSpec, BenchmarkSuite, load_benchmark_suite
from .harness import BenchmarkHarness, BenchmarkResult, run_benchmark
from .reports import BenchmarkReport, generate_report
from .stub_server import StubModelServer, StubServerConfig

__all__ = [
    "BenchmarkHarness",
    "BenchmarkResult", 
    "run_benchmark",
    "BenchmarkSpec",
    "BenchmarkSuite",
    "load_benchmark_suite",
    "StubModelServer",
    "StubServerConfig",
    "BenchmarkReport",
    "generate_report",
]