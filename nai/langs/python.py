"""Python language adapter.

This adapter delegates to the existing Python helpers (``nai.layout``,
``nai.build.stubs`` and ``nai.build.tenv``) so the refactor does not change
behavior.  It is the seam other languages plug into.

Imports of ``nai.build`` are deliberately lazy: ``nai.build`` imports its
plugin, which imports ``nai.build.jobs``, which imports this package.  A
module-level import would create a cycle.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from .. import layout
from .base import Command, Outcome, ParseResult, StubFile, StubResult, ToolCheck

_MARKER = "NotImplementedError"
_SYNTAX_RE = re.compile(r"SyntaxError|IndentationError")
_MISSING_MODULE_RE = re.compile(r"No module named '([^']+)'")
_ZERO_TESTS_RE = re.compile(
    r"collected 0 items|Ran 0 tests|running 0 tests|no tests ran|No tests found",
    re.I,
)
_FAILURE_LINE_RE = re.compile(r"^(?:FAILED|ERROR)\s+\S+.*$", re.M)


class PythonAdapter:
    """Adapter for Python 3 projects (pytest, venv/pip, ruff)."""

    id = "python"
    display_name = "Python"
    test_framework = "pytest"
    extensions: tuple[str, ...] = (".py",)
    stub_marker = _MARKER
    stack_hint = "Python 3.12"
    style_guide = (
        "- Follow PEP 8 and PEP 484 type annotations.\n"
        "- Write clean docstrings, avoid deprecated stdlib/library APIs (ruff UP, B).\n"
        "- Keep functions focused, deterministic where possible, and handle edge cases explicitly."
    )
    #: Probe only: pytest/ruff are optional because the project venv installs
    #: pinned versions from ``target_env.packages``.
    toolchain: tuple[ToolCheck, ...] = (
        ToolCheck("python", ("python3", "--version"), fallback=("python", "py")),
        ToolCheck("pytest", ("pytest", "--version"), required=False),
        ToolCheck("ruff", ("ruff", "--version"), required=False),
    )

    # -- layout ------------------------------------------------------------
    def package_name(self, project: dict[str, Any]) -> str:
        return layout.project_package(project)

    def module_import(self, project: dict[str, Any], node: dict[str, Any]) -> str:
        return f"{self.package_name(project)}.{layout.module_slug(node['name'])}"

    def source_rel(self, project: dict[str, Any], node: dict[str, Any]) -> str:
        return layout.python_source_rel(project, node)

    def test_rel(self, project: dict[str, Any], node: dict[str, Any]) -> str:
        return layout.python_test_rel(node)

    def entities_rel(self, project: dict[str, Any]) -> str:
        return layout.python_entities_rel(project)

    def normalize_test_path(self, raw_path: str, node: dict[str, Any]) -> str:
        """Normalise a model-supplied test path into ``tests/<name>.py``."""
        cleaned = raw_path.replace("\\", "/").lstrip("/")
        parts = [p for p in cleaned.split("/") if p and p != "."]
        if not parts or ".." in parts:
            return layout.python_test_rel(node)
        if parts[0] != "tests":
            parts = ["tests", *parts]
        if not parts[-1].endswith(".py"):
            parts[-1] += ".py"
        return "/".join(parts)

    def check_syntax(self, code: str, filename: str) -> str | None:
        import ast

        try:
            ast.parse(code, filename=filename)
        except SyntaxError as exc:
            return f"Python syntax error at line {exc.lineno}: {exc.msg}"
        return None

    def protected_globs(self, project: dict[str, Any]) -> tuple[str, ...]:
        return (
            "tests/**",
            ".nai/**",
            "conftest.py",
            "pytest.ini",
            "pyproject.toml",
            "setup.cfg",
            "tox.ini",
            self.entities_rel(project),
        )

    def writable_globs(
        self, project: dict[str, Any], node: dict[str, Any]
    ) -> tuple[str, ...]:
        """Match the implement policy: the module file plus its own subpackage."""
        pkg = self.package_name(project)
        module = layout.module_slug(str(node["name"]))
        return (self.source_rel(project, node), f"src/{pkg}/{module}/**")

    def snapshot_ignore(self) -> tuple[str, ...]:
        return (
            ".nai",
            ".git",
            "__pycache__",
            ".venv",
            "venv",
            ".pytest_cache",
            ".mypy_cache",
            ".ruff_cache",
            "*.pyc",
            "*.pyo",
        )

    def cache_dirs(self) -> tuple[str, ...]:
        return (
            ".nai/venv",
            ".pytest_cache",
            ".mypy_cache",
            ".ruff_cache",
            "__pycache__",
        )

    # -- stubs -------------------------------------------------------------
    def render_stub(
        self,
        project: dict[str, Any],
        node: dict[str, Any],
        contract: dict[str, Any] | None,
    ) -> StubResult:
        from ..build import stubs

        result = stubs.generate_contract_stubs(project, node, contract)
        return StubResult(
            files=tuple(StubFile(f.rel_path, f.content) for f in result.files),
            module_import=result.module_import,
            package_name=result.package_name,
        )

    def validate_stub(
        self,
        workspace_root: Path,
        module_import: str,
        run_cmd: Any,
        *,
        executable: str | None = None,
        run_setup_cmd: Any | None = None,
    ) -> tuple[bool, str]:
        from ..build import stubs

        return stubs.validate_stub_imports(
            workspace_root, module_import, run_cmd, python_exe=executable
        )

    def extract_signatures(self, source: str) -> str:
        from ..build import stubs

        return stubs.extract_stub_signatures(source)

    # -- commands ----------------------------------------------------------
    def setup_commands(
        self, project: dict[str, Any], workspace_root: Path
    ) -> tuple[Command, ...]:
        from ..build.tenv import parse_target_env

        tenv = parse_target_env(project, workspace_root)
        venv = Path(tenv.venv_dir)
        python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        commands = [Command(argv=("python3", "-m", "venv", str(venv)), network=True)]
        if tenv.packages:
            commands.append(
                Command(
                    argv=(str(python), "-m", "pip", "install", *tenv.packages),
                    network=not tenv.offline,
                )
            )
        return tuple(commands)

    def build_commands(
        self, project: dict[str, Any], workspace_root: Path
    ) -> tuple[Command, ...]:
        # Python has no separate build step; byte-compilation is a cheap check.
        return (Command(argv=("python3", "-m", "compileall", "-q", "src"), network=False),)

    def test_command(
        self,
        project: dict[str, Any],
        node: dict[str, Any],
        *,
        scope: str | None = None,
        paths: tuple[str, ...] = (),
    ) -> Command:
        targets = tuple(paths) or (self.test_rel(project, node),)
        return Command(argv=("pytest", "-q", *targets), network=False)

    def lint_command(
        self,
        project: dict[str, Any],
        workspace_root: Path,
        targets: tuple[str, ...],
    ) -> Command | None:
        if not targets:
            return None
        from ..build.tenv import parse_target_env

        tenv = parse_target_env(project, workspace_root)
        rules = ",".join(tenv.lint_rules)
        return Command(argv=("ruff", "check", "--select", rules, *targets), network=False)

    def env_overrides(
        self, project: dict[str, Any], workspace_root: Path
    ) -> dict[str, str]:
        return {"PYTHONPATH": "src"}

    # -- environment and workspace -----------------------------------------
    def needs_target_env(self) -> bool:
        """Python builds inside a pinned virtualenv."""
        return True

    def prepare_workspace(self, project: dict[str, Any], workspace_root: Path) -> tuple[str, ...]:
        """Python's layout needs no generated manifest."""
        return ()

    def environment_block(self, project: dict[str, Any], workspace_root: Path) -> str:
        from ..build.tenv import format_env_block, parse_target_env

        return format_env_block(parse_target_env(project, workspace_root))

    def extract_test_functions(self, test_sources: dict[str, str]) -> tuple[list[str], list[str]]:
        from ..build.traceability import extract_test_functions

        return extract_test_functions(test_sources)

    def platform_lint(
        self,
        project: dict[str, Any],
        node: dict[str, Any],
        workspace_root: Path,
        *,
        platform: str = "any",
    ) -> tuple[str, ...]:
        """Python has no platform-restricted imports to enforce."""
        return ()

    # -- remote execution ---------------------------------------------------
    def remote_recipe(
        self, project: dict[str, Any], node: dict[str, Any], job_kind: str, *, platform: str = "any"
    ) -> str:
        return "verify" if job_kind in ("verify", "author_tests", "integrate") else job_kind

    def remote_params(
        self,
        project: dict[str, Any],
        node: dict[str, Any],
        job_kind: str,
        *,
        targets: tuple[str, ...] = (),
        platform: str = "any",
    ) -> dict[str, Any]:
        return {"targets": list(targets)}

    # -- results -----------------------------------------------------------
    def parse_result(
        self,
        raw_output: str,
        exit_code: int,
        kind: str,
        *,
        timed_out: bool = False,
    ) -> ParseResult:
        text = raw_output or ""
        if timed_out:
            return ParseResult(
                Outcome.TIMEOUT, exit_code=exit_code, raw_output=text, timed_out=True
            )
        marker = _MARKER in text
        missing = _MISSING_MODULE_RE.search(text)
        syntax = bool(_SYNTAX_RE.search(text))
        zero = bool(_ZERO_TESTS_RE.search(text))
        if exit_code == 0:
            outcome = Outcome.NO_TESTS if zero else Outcome.PASS
        elif kind == "build":
            outcome = Outcome.BUILD_ERROR
        elif syntax:
            outcome = Outcome.BUILD_ERROR
        else:
            outcome = Outcome.TEST_FAIL
        failures = tuple(m.group(0).strip() for m in _FAILURE_LINE_RE.finditer(text))
        return ParseResult(
            outcome=outcome,
            exit_code=exit_code,
            tests=(),
            failure_messages=failures,
            raw_output=text,
            marker_present=marker,
            missing_module=missing.group(1) if missing else None,
        )

    def stub_failure_is_valid(
        self,
        result: ParseResult,
        *,
        integration: bool = False,
        expected_module: str | None = None,
    ) -> bool:
        """True only when the failure is the *expected* pre-implementation one."""
        if result.outcome in (Outcome.BUILD_ERROR, Outcome.PASS, Outcome.NO_TESTS):
            return False
        if result.marker_present:
            return True
        if integration and expected_module and result.missing_module == expected_module:
            return True
        return False
