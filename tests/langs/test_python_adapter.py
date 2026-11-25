"""Equivalence tests for the Python adapter.

Every assertion here pins the adapter to the behavior the characterization
tests lock in for the current Python helpers.  If the adapter drifts, these
tests fail.
"""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from nai import layout
from nai.build import stubs
from nai.langs import registry
from nai.langs.base import Outcome
from nai.langs.python import PythonAdapter

PROJECT = {"name": "Tiny Lib", "settings": {}}
NODE = {"name": "text-normalizer", "kind": "leaf"}
CONTRACT = {
    "components": [
        {
            "name": "text-normalizer",
            "provides": [
                {
                    "name": "normalize",
                    "signature": "def normalize(text: str) -> str",
                    "description": "Lowercase and collapse whitespace.",
                }
            ],
        }
    ],
    "entities": [],
}


@pytest.fixture()
def adapter() -> PythonAdapter:
    return PythonAdapter()


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_has_python_by_default():
    assert registry.supported() == ["python", "swift"]
    assert registry.get("python").id == "python"
    assert registry.get(None).id == "python"
    assert registry.get("PYTHON").id == "python"


def test_registry_rejects_unknown_language():
    with pytest.raises(registry.UnknownLanguageError):
        registry.get("cobol")


def test_language_for_reads_project_settings():
    assert registry.language_for({"settings": {}}).id == "python"
    assert registry.language_for({"settings": {"language": "python"}}).id == "python"
    with pytest.raises(registry.UnknownLanguageError):
        registry.language_for({"settings": {"language": "cobol"}})


# ---------------------------------------------------------------------------
# Layout equivalence
# ---------------------------------------------------------------------------


def test_layout_helpers_match_the_current_python_helpers(adapter):
    assert adapter.package_name(PROJECT) == layout.project_package(PROJECT)
    assert adapter.source_rel(PROJECT, NODE) == layout.python_source_rel(PROJECT, NODE)
    assert adapter.test_rel(PROJECT, NODE) == layout.python_test_rel(NODE)
    assert adapter.entities_rel(PROJECT) == layout.python_entities_rel(PROJECT)
    assert adapter.module_import(PROJECT, NODE) == "tiny_lib.text_normalizer"


def test_protected_globs_match_the_verify_policy(adapter):
    globs = adapter.protected_globs(PROJECT)
    for expected in (
        "tests/**",
        ".nai/**",
        "conftest.py",
        "pytest.ini",
        "pyproject.toml",
        "setup.cfg",
        "tox.ini",
        "src/tiny_lib/entities.py",
    ):
        assert expected in globs


def test_writable_globs_match_the_implement_policy(adapter):
    writable = adapter.writable_globs(PROJECT, NODE)
    assert writable[0] == "src/tiny_lib/text_normalizer.py"
    # Only the node's own subpackage is writable, matching the pre-refactor policy.
    assert "src/tiny_lib/text_normalizer/**" in writable


# ---------------------------------------------------------------------------
# Stubs and signatures
# ---------------------------------------------------------------------------


def test_render_stub_matches_generate_contract_stubs(adapter):
    expected = stubs.generate_contract_stubs(PROJECT, NODE, CONTRACT)
    actual = adapter.render_stub(PROJECT, NODE, CONTRACT)
    assert actual.module_import == expected.module_import
    assert actual.package_name == expected.package_name
    assert [(f.rel_path, f.content) for f in actual.files] == [
        (f.rel_path, f.content) for f in expected.files
    ]
    module = next(f for f in actual.files if f.rel_path.endswith("text_normalizer.py"))
    assert "NotImplementedError" in module.content


def test_extract_signatures_matches_the_stub_helper(adapter):
    source = (
        "def normalize(text: str) -> str:\n"
        '    """Lowercase text."""\n'
        "    return text.lower()\n"
    )
    assert adapter.extract_signatures(source) == stubs.extract_stub_signatures(source)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def test_test_command_matches_the_current_pytest_invocation(adapter):
    command = adapter.test_command(PROJECT, NODE)
    assert shlex.join(command.argv) == "pytest -q tests/test_text_normalizer.py"


def test_test_command_accepts_explicit_paths(adapter):
    command = adapter.test_command(
        PROJECT, NODE, paths=("tests/test_a.py", "tests/test_b.py")
    )
    assert command.argv == ("pytest", "-q", "tests/test_a.py", "tests/test_b.py")


def test_env_overrides_match_pythonpath_src(adapter):
    assert adapter.env_overrides(PROJECT, Path(".")) == {"PYTHONPATH": "src"}


def test_lint_command_matches_ruff_invocation(adapter):
    command = adapter.lint_command(
        PROJECT, Path("."), ("src/tiny_lib/text_normalizer.py",)
    )
    assert command is not None
    assert command.argv == (
        "ruff",
        "check",
        "--select",
        "UP,B",
        "src/tiny_lib/text_normalizer.py",
    )


def test_lint_command_is_none_without_targets(adapter):
    assert adapter.lint_command(PROJECT, Path("."), ()) is None


def test_setup_commands_include_venv_and_pip(adapter):
    project = {
        "name": "Tiny Lib",
        "settings": {"target_env": {"packages": ["requests==2.31.0"]}},
    }
    commands = adapter.setup_commands(project, Path("."))
    assert commands[0].argv[:2] == ("python3", "-m")
    assert commands[0].network is True
    assert commands[1].argv[-1] == "requests==2.31.0"
    assert commands[1].network is True


def test_setup_commands_offline_when_configured(adapter):
    project = {
        "name": "Tiny Lib",
        "settings": {
            "target_env": {"packages": ["requests==2.31.0"], "offline": True}
        },
    }
    commands = adapter.setup_commands(project, Path("."))
    assert commands[1].network is False


# ---------------------------------------------------------------------------
# Result classification
# ---------------------------------------------------------------------------


def test_parse_result_pass(adapter):
    result = adapter.parse_result("1 passed in 0.01s", 0, "test")
    assert result.outcome is Outcome.PASS
    assert result.marker_present is False


def test_parse_result_stub_failure(adapter):
    result = adapter.parse_result(
        "FAILED tests/test_x.py::test_R1 - NotImplementedError: Contract stub for normalize",
        1,
        "test",
    )
    assert result.outcome is Outcome.TEST_FAIL
    assert result.marker_present is True
    assert result.failure_messages


def test_parse_result_syntax_error_is_build_error(adapter):
    result = adapter.parse_result("SyntaxError: invalid syntax", 1, "test")
    assert result.outcome is Outcome.BUILD_ERROR


def test_parse_result_missing_module(adapter):
    result = adapter.parse_result(
        "ModuleNotFoundError: No module named 'tiny_lib.root_integration'", 1, "test"
    )
    assert result.outcome is Outcome.TEST_FAIL
    assert result.missing_module == "tiny_lib.root_integration"


def test_parse_result_zero_tests(adapter):
    result = adapter.parse_result("collected 0 items", 0, "test")
    assert result.outcome is Outcome.NO_TESTS


def test_parse_result_timeout(adapter):
    result = adapter.parse_result("", -9, "test", timed_out=True)
    assert result.outcome is Outcome.TIMEOUT


def test_parse_result_build_kind(adapter):
    result = adapter.parse_result("error: something", 1, "build")
    assert result.outcome is Outcome.BUILD_ERROR


# ---------------------------------------------------------------------------
# Stub failure validity
# ---------------------------------------------------------------------------


def test_stub_failure_is_valid_for_marker(adapter):
    result = adapter.parse_result("NotImplementedError: stub", 1, "test")
    assert adapter.stub_failure_is_valid(result) is True


def test_stub_failure_is_valid_for_integration_missing_module(adapter):
    result = adapter.parse_result(
        "ModuleNotFoundError: No module named 'tiny_lib.root_integration'", 1, "test"
    )
    assert (
        adapter.stub_failure_is_valid(
            result, integration=True, expected_module="tiny_lib.root_integration"
        )
        is True
    )


def test_stub_failure_is_invalid_for_wrong_module(adapter):
    result = adapter.parse_result(
        "ModuleNotFoundError: No module named 'other_module'", 1, "test"
    )
    assert (
        adapter.stub_failure_is_valid(
            result, integration=True, expected_module="tiny_lib.root_integration"
        )
        is False
    )


def test_stub_failure_is_invalid_for_pass_and_build_error(adapter):
    assert (
        adapter.stub_failure_is_valid(adapter.parse_result("1 passed", 0, "test"))
        is False
    )
    assert (
        adapter.stub_failure_is_valid(adapter.parse_result("SyntaxError", 1, "test"))
        is False
    )


def test_stub_failure_is_invalid_without_marker(adapter):
    result = adapter.parse_result("AssertionError: expected 1", 1, "test")
    assert adapter.stub_failure_is_valid(result) is False


# ---------------------------------------------------------------------------
# Snapshot / cache metadata
# ---------------------------------------------------------------------------


def test_snapshot_ignore_and_cache_dirs(adapter):
    ignored = adapter.snapshot_ignore()
    for expected in (".nai", ".git", "__pycache__", ".venv", ".pytest_cache"):
        assert expected in ignored
    caches = adapter.cache_dirs()
    for expected in (".nai/venv", ".pytest_cache", ".mypy_cache", ".ruff_cache"):
        assert expected in caches
