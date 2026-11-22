"""Hold-out test support for SS-EVAL (EVAL-3).

Hold-out tests live outside the project workspace and store. After a benchmark
run they are applied to the generated output; their pass rate is the primary
quality metric (not merely "the CLI exited 0").
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class HoldoutResult:
    """Result of applying hold-out tests to a benchmark output."""
    applied: bool = False
    total: int = 0
    passed: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)
    output: str = ""

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0


def discover_holdout(spec_path: Path) -> Path | None:
    """Find hold-out tests referenced by a benchmark spec.

    Supported metadata lines:
      Holdout: relative/or/absolute/path
      Holdout dir: path
    """
    for line in spec_path.read_text(encoding="utf-8").splitlines():
        low = line.strip().lower()
        if low.startswith("holdout:") or low.startswith("holdout dir:"):
            value = line.split(":", 1)[1].strip()
            if not value:
                return None
            path = Path(value).expanduser()
            if not path.is_absolute():
                path = (spec_path.parent / path).resolve()
            return path if path.exists() else None
    return None


def run_holdout(holdout_path: Path, workspace: Path, timeout: int = 300) -> HoldoutResult:
    """Copy hold-out tests into a temp copy of the workspace and run pytest.

    Tests are never copied into the original workspace permanently; a temp dir
    keeps the benchmark's generated output pristine for later inspection.
    """
    result = HoldoutResult(applied=True)
    if not holdout_path.exists():
        result.errors.append(f"hold-out path not found: {holdout_path}")
        return result

    with tempfile.TemporaryDirectory(prefix="nai-holdout-") as tmp:
        target = Path(tmp) / "workspace"
        if workspace.exists():
            shutil.copytree(workspace, target, ignore=shutil.ignore_patterns(
                "__pycache__", "*.pyc", ".git", "venv", ".venv"))
        else:
            target.mkdir(parents=True)

        # Copy hold-out tests under a dedicated directory to avoid name clashes.
        tests_dir = target / "holdout_tests"
        if holdout_path.is_dir():
            shutil.copytree(holdout_path, tests_dir, dirs_exist_ok=True)
        else:
            tests_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(holdout_path, tests_dir / holdout_path.name)

        try:
            proc = subprocess.run(
                ["python", "-m", "pytest", "-q", str(tests_dir)],
                cwd=target, capture_output=True, text=True, timeout=timeout,
            )
            result.output = (proc.stdout or "") + (proc.stderr or "")
            # Parse pytest summary line: "N passed, M failed" / "N passed"
            import re

            m_pass = re.search(r"(\d+) passed", result.output)
            m_fail = re.search(r"(\d+) failed", result.output)
            m_err = re.search(r"(\d+) error", result.output)
            result.passed = int(m_pass.group(1)) if m_pass else 0
            result.failed = int(m_fail.group(1)) if m_fail else 0
            if m_err:
                result.failed += int(m_err.group(1))
            result.total = result.passed + result.failed
            if proc.returncode not in (0, 1) and not result.total:
                result.errors.append(f"pytest exit {proc.returncode}")
        except subprocess.TimeoutExpired:
            result.errors.append(f"hold-out tests timed out after {timeout}s")
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"{type(exc).__name__}: {exc}")
    return result


def holdout_to_dict(result: HoldoutResult) -> dict[str, Any]:
    return {
        "applied": result.applied,
        "total": result.total,
        "passed": result.passed,
        "failed": result.failed,
        "pass_rate": result.pass_rate,
        "errors": result.errors,
        "output": result.output[-4000:],
    }
