"""LanguageAdapter protocol and shared result types.

The adapter is the only layer that knows a language's toolchain, layout,
stub syntax, command shape and result format.  The pipeline calls the adapter
instead of hard-coding Python assumptions.

Everything here is pure description: no filesystem I/O and no subprocesses.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


class Outcome(str, Enum):
    """Normalised result of a build/test command."""

    PASS = "pass"
    TEST_FAIL = "test_fail"
    BUILD_ERROR = "build_error"
    NO_TESTS = "no_tests"
    TOOLCHAIN_MISSING = "toolchain_missing"
    TIMEOUT = "timeout"


@dataclass(frozen=True)
class TestCase:
    """One test reported by a machine-readable result file."""

    name: str
    outcome: str  # "passed" | "failed" | "error" | "skipped"
    message: str = ""


@dataclass(frozen=True)
class ParseResult:
    """Normalised outcome plus the evidence the pipeline needs."""

    outcome: Outcome
    exit_code: int = 0
    tests: tuple[TestCase, ...] = ()
    failure_messages: tuple[str, ...] = ()
    raw_output: str = ""
    marker_present: bool = False
    missing_module: str | None = None
    timed_out: bool = False


@dataclass(frozen=True)
class StubFile:
    rel_path: str
    content: str


@dataclass(frozen=True)
class StubResult:
    files: tuple[StubFile, ...]
    module_import: str
    package_name: str


@dataclass(frozen=True)
class Command:
    """An argv command.

    ``shell`` is only set by an adapter that explicitly needs a shell.  The
    pipeline never turns a model-supplied string into a shell command.
    """

    argv: tuple[str, ...]
    shell: bool = False
    network: bool = True


@dataclass(frozen=True)
class ToolCheck:
    """One toolchain binary an adapter needs, and how to ask for its version.

    ``argv`` is the full command used for the probe (``("python3", "--version")``);
    ``argv[0]`` is resolved with ``shutil.which``.  ``fallback`` lists alternative
    binary names tried in order when ``argv[0]`` is absent (for example
    ``("python", "py")`` on Windows).  ``required`` tools make the probe fail when
    they are missing; optional ones are reported only.
    """

    name: str
    argv: tuple[str, ...]
    required: bool = True
    fallback: tuple[str, ...] = ()


@runtime_checkable
class LanguageAdapter(Protocol):
    """Everything the pipeline needs to know about one language."""

    id: str
    display_name: str
    test_framework: str
    extensions: tuple[str, ...]
    stub_marker: str
    stack_hint: str
    style_guide: str
    toolchain: tuple[ToolCheck, ...]

    # -- layout ------------------------------------------------------------
    def package_name(self, project: dict[str, Any]) -> str: ...

    def module_import(self, project: dict[str, Any], node: dict[str, Any]) -> str: ...

    def source_rel(self, project: dict[str, Any], node: dict[str, Any]) -> str: ...

    def test_rel(self, project: dict[str, Any], node: dict[str, Any]) -> str: ...

    def entities_rel(self, project: dict[str, Any]) -> str: ...

    def normalize_test_path(self, raw_path: str, node: dict[str, Any]) -> str: ...

    def check_syntax(self, code: str, filename: str) -> str | None: ...

    def protected_globs(self, project: dict[str, Any]) -> tuple[str, ...]: ...

    def writable_globs(
        self, project: dict[str, Any], node: dict[str, Any]
    ) -> tuple[str, ...]: ...

    def snapshot_ignore(self) -> tuple[str, ...]: ...

    def cache_dirs(self) -> tuple[str, ...]: ...

    # -- stubs -------------------------------------------------------------
    def render_stub(
        self,
        project: dict[str, Any],
        node: dict[str, Any],
        contract: dict[str, Any] | None,
    ) -> StubResult: ...

    def validate_stub(
        self,
        workspace_root: Path,
        module_import: str,
        run_cmd: Any,
        *,
        executable: str | None = None,
    ) -> tuple[bool, str]: ...

    def extract_signatures(self, source: str) -> str: ...

    # -- commands ----------------------------------------------------------
    def setup_commands(
        self, project: dict[str, Any], workspace_root: Path
    ) -> tuple[Command, ...]: ...

    def build_commands(
        self, project: dict[str, Any], workspace_root: Path
    ) -> tuple[Command, ...]: ...

    def test_command(
        self,
        project: dict[str, Any],
        node: dict[str, Any],
        *,
        scope: str | None = None,
        paths: tuple[str, ...] = (),
    ) -> Command: ...

    def lint_command(
        self,
        project: dict[str, Any],
        workspace_root: Path,
        targets: tuple[str, ...],
    ) -> Command | None: ...

    def env_overrides(
        self, project: dict[str, Any], workspace_root: Path
    ) -> dict[str, str]: ...

    # -- environment and workspace -----------------------------------------
    def needs_target_env(self) -> bool: ...

    def prepare_workspace(
        self, project: dict[str, Any], workspace_root: Path
    ) -> tuple[str, ...]: ...

    def environment_block(
        self, project: dict[str, Any], workspace_root: Path
    ) -> str: ...

    def extract_test_functions(
        self, test_sources: dict[str, str]
    ) -> tuple[list[str], list[str]]: ...

    def platform_lint(
        self,
        project: dict[str, Any],
        node: dict[str, Any],
        workspace_root: Path,
        *,
        platform: str = "any",
    ) -> tuple[str, ...]: ...

    # -- remote execution ---------------------------------------------------
    def remote_recipe(
        self, project: dict[str, Any], node: dict[str, Any], job_kind: str, *, platform: str = "any"
    ) -> str: ...

    def remote_params(
        self,
        project: dict[str, Any],
        node: dict[str, Any],
        job_kind: str,
        *,
        targets: tuple[str, ...] = (),
        platform: str = "any",
    ) -> dict[str, Any]: ...

    # -- results -----------------------------------------------------------
    def parse_result(
        self,
        raw_output: str,
        exit_code: int,
        kind: str,
        *,
        timed_out: bool = False,
    ) -> ParseResult: ...

    def stub_failure_is_valid(
        self,
        result: ParseResult,
        *,
        integration: bool = False,
        expected_module: str | None = None,
    ) -> bool: ...
