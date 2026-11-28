"""Benchmark harness for WP9 (SS-EVAL)."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .holdout import HoldoutResult, discover_holdout, holdout_to_dict, run_holdout


@dataclass
class BenchmarkResult:
    """Result of a single benchmark run."""
    benchmark_id: str
    benchmark_name: str
    model_profile: str
    model_id: str
    quantization: str | None
    started_at: str
    completed_at: str | None = None
    duration_seconds: float | None = None
    success: bool = False
    error: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)
    holdout: HoldoutResult | None = None
    pipeline_mode: str = "v2"


@dataclass
class BenchmarkHarness:
    """Runs benchmarks against a model profile."""
    
    benchmarks_dir: Path
    output_dir: Path
    nai_binary: str = "nai"
    pipeline_mode: str = "v2"  # "v2" uses the store/scheduler pipeline; "v1" uses the legacy CLI
    holdout_timeout: int = 300
    
    def __post_init__(self):
        self.benchmarks_dir = Path(self.benchmarks_dir)
        self.output_dir = Path(self.output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
    
    def discover_benchmarks(self) -> list[Path]:
        """Discover benchmark spec files."""
        return sorted(self.benchmarks_dir.glob("*.md"))
    
    def _run_v2_pipeline(self, brief: str, workspace: Path, model_profile: str,
                         model_id: str | None, timeout: int) -> tuple[bool, str, dict[str, Any]]:
        """Run the v2 pipeline (spec -> build -> done) using the store and runner.

        Returns (success, error, metrics_dict). The model server is taken from the
        configured `LLAMA_SWAP_URL` env var (typically the scripted stub in tests).
        """
        import os

        from nai.build.plugin import register as register_build
        from nai.config import load_config
        from nai.llm import LLM
        from nai.sched import Runner, SchedConfig
        from nai.sched.readiness import walk_nodes
        from nai.spec.plugin import register as register_spec
        from nai.store import Store

        db = workspace / ".nai" / "nai.sqlite3"
        db.parent.mkdir(parents=True, exist_ok=True)
        store = Store(db, read_only=False)
        try:
            cfg = load_config()
            if model_profile in cfg.profiles:
                cfg.default_profile = model_profile
            pid = store.create_project(brief[:60], brief, workspace_path=workspace,
                                       settings={"model_profile": model_profile})
            store.create_node(pid, "root", "system", objective=brief)

            base_url = os.environ.get("LLAMA_SWAP_URL", "http://127.0.0.1:8080/v1")
            llm = LLM(base_url, os.environ.get("LLAMA_SWAP_KEY", "none"), stream=False,
                      show_thinking=False)
            runner = Runner(store, llm, app_config=cfg,
                            sched=SchedConfig(threaded=False, poll_seconds=0.05,
                                              plan_interval_seconds=0.05),
                            state_dir=workspace / ".nai")
            register_spec(runner)
            register_build(runner)
            runner.run(drain=True)
            runner.close()

            nodes = walk_nodes(store, pid)
            root = next((n for n in nodes if n.get("parent_id") is None), None)
            done = root is not None and root.get("status") == "done"
            errors = [e for e in store.list_events(pid, limit=10000)
                      if e.get("type") in ("planner.error", "job.failed")]
            metrics = {"nodes": len(nodes), "root_status": root.get("status") if root else "missing",
                       "errors": len(errors), "files_created": len(list(workspace.rglob("*")))}
            if not done:
                return False, f"root node ended as {metrics['root_status']}", metrics
            return True, "", metrics
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}", {}
        finally:
            store.close()

    def run_single(self, benchmark_path: Path, model_profile: str,
                   model_id: str | None = None, quantization: str | None = None,
                   timeout: int = 3600) -> BenchmarkResult:
        """Run a single benchmark."""
        benchmark_id = benchmark_path.stem
        started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        
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
            pipeline_mode=self.pipeline_mode,
        )
        
        try:
            if self.pipeline_mode == "v2":
                # The v2 pipeline is driven in-process: brief -> spec -> build -> done.
                brief = benchmark_path.read_text(encoding="utf-8")
                ok, error, metrics = self._run_v2_pipeline(brief, workspace, model_profile, model_id, timeout)
                result.success = ok
                result.error = error or None
                result.metrics = {**self._extract_metrics(workspace), **metrics}
            else:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
                result.success = proc.returncode == 0
                result.error = proc.stderr if proc.returncode != 0 else None
                result.metrics = self._extract_metrics(workspace)

            completed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            result.completed_at = completed_at
            result.duration_seconds = (
                datetime.fromisoformat(completed_at.replace("Z", "+00:00")) -
                datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            ).total_seconds()

            # Write metrics.json and collect artifacts for successful runs.
            if result.success:
                metrics_file = workspace / ".nai" / "metrics.json"
                metrics_file.parent.mkdir(parents=True, exist_ok=True)
                metrics_file.write_text(json.dumps(result.metrics, indent=2), encoding="utf-8")
                result.artifacts = [str(p.relative_to(self.output_dir)) for p in workspace.rglob("*") if p.is_file()]

            # Apply hold-out tests (EVAL-3): success includes their pass rate.
            holdout_path = discover_holdout(benchmark_path)
            if holdout_path:
                result.holdout = run_holdout(holdout_path, workspace, timeout=self.holdout_timeout)
                result.metrics["holdout"] = holdout_to_dict(result.holdout)
                if result.holdout.errors:
                    result.error = (result.error or "") + " | holdout: " + "; ".join(result.holdout.errors)
                    result.success = False
                elif result.holdout.total > 0 and result.holdout.pass_rate < 1.0:
                    result.success = False
                    result.error = (result.error or "") + f" | holdout pass rate {result.holdout.pass_rate:.1%}"

        except subprocess.TimeoutExpired:
            result.completed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            result.success = False
            result.error = f"Timeout after {timeout}s"
        except Exception as e:
            result.completed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
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