"""Benchmark harness for WP9 (SS-EVAL)."""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from nai.cli import main as nai_main


@dataclass
class BenchmarkResult:
    """Result of a single benchmark run."""
    benchmark_id: str
    benchmark_name: str
    model_profile: str
    model_id: str
    quantization: str | None
    started_at: str
    completed_at: str | None
    duration_seconds: float | None
    success: bool
    error: str | None
    metrics: dict[str, Any] = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)


@dataclass
class BenchmarkHarness:
    """Runs benchmarks against a model profile."""
    
    benchmarks_dir: Path
    output_dir: Path
    nai_binary: str = "nai"
    
    def __post_init__(self):
        self.benchmarks_dir = Path(self.benchmarks_dir)
        self.output_dir = Path(self.output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
    
    def discover_benchmarks(self) -> list[Path]:
        """Discover benchmark spec files."""
        return sorted(self.benchmarks_dir.glob("*.md"))
    
    def run_single(self, benchmark_path: Path, model_profile: str, 
                   model_id: str | None = None, quantization: str | None = None,
                   timeout: int = 3600) -> BenchmarkResult:
        """Run a single benchmark."""
        benchmark_id = benchmark_path.stem
        started_at = datetime.utcnow().isoformat() + "Z"
        
        # Prepare workspace
        workspace = self.output_dir / benchmark_id
        workspace.mkdir(parents=True, exist_ok=True)
        
        # Build command
        cmd = [
            self.nai_binary,
            str(benchmark_path),
            "-w", str(workspace),
            "--profile", model_profile,
        ]
        if model_id:
            cmd.extend(["--reasoner", model_id, "--coder", model_id])
        
        result = BenchmarkResult(
            benchmark_id=benchmark_id,
            benchmark_name=benchmark_path.name,
            model_profile=model_profile,
            model_id=model_id or "auto",
            quantization=quantization or "unknown",
            started_at=started_at,
        )
        
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            completed_at = datetime.utcnow().isoformat() + "Z"
            result.completed_at = completed_at
            result.duration_seconds = (
                datetime.fromisoformat(completed_at.replace("Z", "+00:00")) -
                datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            ).total_seconds()
            result.success = proc.returncode == 0
            result.error = proc.stderr if proc.returncode != 0 else None
            
            # Collect metrics from output
            if proc.returncode == 0:
                result.metrics = self._extract_metrics(workspace)
                result.artifacts = [str(p.relative_to(self.output_dir)) for p in workspace.rglob("*") if p.is_file()]
                
        except subprocess.TimeoutExpired:
            result.completed_at = datetime.utcnow().isoformat() + "Z"
            result.success = False
            result.error = f"Timeout after {timeout}s"
        except Exception as e:
            result.completed_at = datetime.utcnow().isoformat() + "Z"
            result.success = False
            result.error = str(e)
        
        return result
    
    def _extract_metrics(self, workspace: Path) -> dict[str, Any]:
        """Extract metrics from a completed benchmark run."""
        metrics = {}
        
        # Look for metrics files
        metrics_file = workspace / ".nai" / "metrics.json"
        if metrics_file.exists():
            try:
                metrics.update(json.loads(metrics_file.read_text()))
            except Exception:
                pass
        
        # Count files created
        metrics["files_created"] = len(list(workspace.rglob("*")))
        
        return metrics
    
    def run_suite(self, benchmark_suite: list[Path], model_profile: str,
                  model_id: str | None = None, quantization: str | None = None,
                  timeout: int = 3600) -> list[BenchmarkResult]:
        """Run a suite of benchmarks."""
        results = []
        for bench_path in benchmark_suite:
            result = self.run_single(bench_path, model_profile, model_id, quantization, timeout)
            results.append(result)
        return results


def run_benchmark(benchmark_path: str | Path, model_profile: str,
                  output_dir: str | Path, model_id: str | None = None,
                  quantization: str | None = None, timeout: int = 3600) -> BenchmarkResult:
    """Convenience function to run a single benchmark."""
    harness = BenchmarkHarness(Path(benchmark_path).parent, Path(output_dir))
    return harness.run_single(Path(benchmark_path), model_profile, model_id, quantization, timeout)