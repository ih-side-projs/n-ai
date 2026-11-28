"""Benchmark report generation for WP9 (EVAL-6, EVAL-7)."""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .harness import BenchmarkResult


@dataclass
class BenchmarkReport:
    """Complete benchmark report (EVAL-7)."""
    generated_at: str
    nai_version: str
    git_commit: str
    model_profile: str
    model_id: str
    quantization: str
    benchmark_suite: str
    results: list[BenchmarkResult]
    summary: dict[str, Any] = field(default_factory=dict)
    
    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, default=str)
    
    def to_markdown(self) -> str:
        lines = [
            "# Benchmark Report",
            "",
            f"Generated: {self.generated_at}",
            f"nai version: {self.nai_version}",
            f"Git commit: {self.git_commit}",
            f"Model profile: {self.model_profile}",
            f"Model: {self.model_id}",
            f"Quantization: {self.quantization}",
            f"Benchmark suite: {self.benchmark_suite}",
            "",
            "## Summary",
            "",
            f"- Total benchmarks: {self.summary.get('total', 0)}",
            f"- Passed: {self.summary.get('passed', 0)}",
            f"- Failed: {self.summary.get('failed', 0)}",
            f"- Success rate: {self.summary.get('success_rate', 0):.1%}",
            f"- Total duration: {self.summary.get('total_duration', 0):.1f}s",
            "",
            "## Results",
            "",
        ]
        
        for result in self.results:
            status = "✅" if result.success else "❌"
            lines.append(f"### {status} {result.benchmark_name}")
            lines.append(f"- ID: {result.benchmark_id}")
            lines.append(f"- Pipeline: {getattr(result, 'pipeline_mode', 'v2')}")
            lines.append(f"- Duration: {result.duration_seconds:.1f}s" if result.duration_seconds else "- Duration: N/A")
            holdout = getattr(result, "holdout", None)
            if holdout and holdout.applied:
                lines.append(f"- Hold-out: {holdout.passed}/{holdout.total} passed ({holdout.pass_rate:.1%})")
            if result.error:
                lines.append(f"- Error: {result.error}")
            lines.append("")
        
        return "\n".join(lines)


def generate_report(results: list[BenchmarkResult], 
                    model_profile: str,
                    model_id: str,
                    quantization: str,
                    benchmark_suite: str,
                    nai_version: str = "unknown",
                    git_commit: str = "unknown") -> BenchmarkReport:
    """Generate a benchmark report from results."""
    
    total = len(results)
    passed = sum(1 for r in results if r.success)
    failed = total - passed
    total_duration = sum(r.duration_seconds or 0 for r in results)
    
    summary = {
        "total": total,
        "passed": passed,
        "failed": failed,
        "success_rate": passed / total if total > 0 else 0,
        "total_duration": total_duration,
    }
    
    return BenchmarkReport(
        generated_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        nai_version=nai_version,
        git_commit=git_commit,
        model_profile=model_profile,
        model_id=model_id,
        quantization=quantization,
        benchmark_suite=benchmark_suite,
        results=results,
        summary=summary,
    )


def get_git_commit() -> str:
    """Get the current git commit hash."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, cwd=Path(__file__).parent.parent.parent
        )
        return result.stdout.strip()[:8] if result.returncode == 0 else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def write_report(report: BenchmarkReport, output_dir: Path, format: str = "both") -> tuple[Path, Path]:
    """Write report to files."""
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    
    json_path = output_dir / f"benchmark_report_{timestamp}.json"
    md_path = output_dir / f"benchmark_report_{timestamp}.md"
    
    if format in ("json", "both"):
        json_path.write_text(report.to_json())
    if format in ("markdown", "both"):
        md_path.write_text(report.to_markdown())
    
    return json_path, md_path