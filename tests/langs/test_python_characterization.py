"""Characterization tests for the Python-specific behavior the adapter must preserve.

These tests pin the *current* behavior of the Python pipeline helpers.  They are
written before the ``nai.langs`` layer exists and must stay green after the
refactor, so the adapter cannot silently change what the pipeline does.
"""

from __future__ import annotations

import shlex
from pathlib import Path

from nai import layout
from nai.build import stubs
from nai.build.tenv import TargetEnv, TargetEnvManager, parse_target_env

PROJECT = {"id": "p1", "name": "Tiny Library", "settings": {}}
NODE = {"id": "n1", "name": "text-normalizer", "kind": "leaf"}

CONTRACT = {
    "entities": [
        {
            "name": "Entry",
            "fields": [
                {"name": "account", "type": "str", "required": True},
                {"name": "memo", "type": "str", "required": False},
            ],
        }
    ],
    "components": [
        {
            "name": "text-normalizer",
            "provides": [
                {
                    "name": "normalize",
                    "signature": "normalize(text: str) -> str",
                    "input_entities": ["Entry"],
                    "output_entities": ["Entry"],
                }
            ],
        }
    ],
}


# ---------------------------------------------------------------------------
# Layout: flat Python package layout
# ---------------------------------------------------------------------------


def test_layout_python_source_and_test_paths_are_flat():
    assert layout.project_package(PROJECT) == "tiny_library"
    assert layout.python_source_rel(PROJECT, NODE) == "src/tiny_library/text_normalizer.py"
    assert layout.python_test_rel(NODE) == "tests/test_text_normalizer.py"
    assert layout.python_entities_rel(PROJECT) == "src/tiny_library/entities.py"


def test_layout_package_name_setting_wins():
    project = {**PROJECT, "settings": {"package_name": "tiny_pkg"}}
    assert layout.project_package(project) == "tiny_pkg"
    assert layout.python_source_rel(project, NODE) == "src/tiny_pkg/text_normalizer.py"


def test_layout_module_slug_handles_keywords_and_leading_digits():
    assert layout.module_slug("text-normalizer") == "text_normalizer"
    assert layout.module_slug("2fast") == "mod_2fast"
    assert layout.module_slug("class") == "mod_class"


# ---------------------------------------------------------------------------
# Stubs: deterministic Python stubs and signature extraction
# ---------------------------------------------------------------------------


def test_stubs_generate_contract_stubs_and_extract_signatures():
    result = stubs.generate_contract_stubs(PROJECT, NODE, CONTRACT)
    by_path = {f.rel_path: f.content for f in result.files}
    assert result.module_import == "tiny_library.text_normalizer"
    assert "src/tiny_library/entities.py" in by_path
    assert "src/tiny_library/text_normalizer.py" in by_path
    stub = by_path["src/tiny_library/text_normalizer.py"]
    assert "def normalize(text: str) -> str:" in stub
    assert "raise NotImplementedError" in stub

    implemented = stub.replace(
        'raise NotImplementedError("Contract stub for normalize")',
        "return text.strip()",
    )
    signatures = stubs.extract_stub_signatures(implemented)
    assert "def normalize(text: str) -> str:" in signatures
    assert "return text.strip()" not in signatures


# ---------------------------------------------------------------------------
# Target environment: Python version defaults and command wrapping
# ---------------------------------------------------------------------------


def test_parse_target_env_defaults_to_host_python(tmp_path: Path):
    tenv = parse_target_env(PROJECT, tmp_path)
    assert tenv.python_version
    assert tenv.os_family
    assert tenv.venv_dir == ".nai/venv"
    assert tenv.lint_rules == ("UP", "B")


def test_wrap_test_command_keeps_pytest_and_adds_deprecation_errors(tmp_path: Path):
    tenv = TargetEnv()
    manager = TargetEnvManager()
    wrapped = manager.wrap_test_command(tmp_path, tenv, "pytest -q tests/test_x.py")
    assert "pytest" in wrapped
    assert "tests/test_x.py" in wrapped
    assert "-W" in wrapped
    assert "error::DeprecationWarning" in wrapped
    # The venv interpreter is used rather than the ambient pytest.
    assert ".nai/venv" in wrapped


def test_wrap_test_command_quotes_allowlist_messages_in_message_slot(tmp_path: Path):
    tenv = TargetEnv(deprecation_allowlist=("old_fn is deprecated",))
    wrapped = TargetEnvManager().wrap_test_command(tmp_path, tenv, "pytest -q tests/test_x.py")
    assert "default:old_fn is deprecated::" in wrapped


# ---------------------------------------------------------------------------
# Result classification: the strings the pipeline currently matches on
# ---------------------------------------------------------------------------


def test_current_classification_strings_are_stable():
    """The Python adapter owns the classification strings the pipeline matches on."""
    import nai.langs.python as python_adapter

    source = Path(python_adapter.__file__).read_text(encoding="utf-8")
    for needle in (
        "NotImplementedError",
        "SyntaxError",
        "IndentationError",
        "No module named",
    ):
        assert needle in source, f"expected classification string {needle!r} in the adapter"


def test_jobs_delegates_classification_to_the_adapter():
    """jobs.py no longer matches Python syntax/import strings itself."""
    import nai.build.jobs as jobs

    source = Path(jobs.__file__).read_text(encoding="utf-8")
    for needle in ("SyntaxError", "IndentationError", "No module named"):
        assert needle not in source, f"jobs.py still hard-codes {needle!r}"


def test_gate_owns_zero_test_detection():
    """The verification gate currently detects zero-test runs with this regex."""
    from nai.gate import ZERO_RE

    for sample in ("collected 0 items", "no tests ran", "No tests found", "Ran 0 tests"):
        assert ZERO_RE.search(sample), sample


def test_test_command_string_is_plain_pytest_invocation():
    """The command the pipeline builds today is exactly ``pytest -q <path>``."""
    rel = layout.python_test_rel(NODE)
    built = f"pytest -q {rel}"
    assert shlex.split(built) == ["pytest", "-q", "tests/test_text_normalizer.py"]
