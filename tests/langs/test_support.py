"""Support status is derived from the registry, and unsupported ids are rejected."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from nai.langs import probe, registry, support
from nai.langs.base import ToolCheck

FIXTURES = Path(__file__).resolve().parents[1] / "e2e" / "fixtures" / "langs"


@pytest.fixture
def partial_language():
    """Register a throwaway language, then remove it again."""
    created: list[str] = []

    def _add(language_id: str, *, toolchain: tuple[ToolCheck, ...] = ()) -> None:
        adapter = SimpleNamespace(
            id=language_id,
            display_name=language_id.title(),
            test_framework=f"{language_id}-test",
            stack_hint=f"{language_id} 1.0",
            toolchain=toolchain,
        )
        registry.register(adapter)  # type: ignore[arg-type]
        created.append(language_id)

    yield _add
    for language_id in created:
        registry.unregister(language_id)


def test_python_meets_every_support_criterion():
    status = support.support_status("python", fixture_root=FIXTURES)
    assert status.adapter and status.knowledge and status.probe_defined
    assert status.e2e_fixture is True
    assert status.complete


def test_supported_languages_come_from_the_registry():
    assert support.supported_languages() == registry.supported()
    assert support.is_supported("python")
    assert not support.is_supported("cobol")
    assert not support.is_supported(None)


def test_adapter_without_a_knowledge_pack_is_not_supported(partial_language):
    partial_language("partial", toolchain=(ToolCheck("partial", ("partial",)),))
    status = support.support_status("partial")
    assert status.adapter is True
    assert status.knowledge is False
    assert status.missing_knowledge  # every section is reported missing
    assert status.supported is False
    assert "partial" not in support.supported_languages()
    with pytest.raises(registry.UnknownLanguageError):
        support.require_supported("partial")


def test_adapter_without_a_probe_is_not_supported(partial_language):
    partial_language("noprobe")
    status = support.support_status("noprobe")
    assert status.probe_defined is False
    assert status.supported is False


def test_require_supported_normalises_case_and_whitespace():
    assert support.require_supported("  Python ") == "python"
    with pytest.raises(registry.UnknownLanguageError):
        support.require_supported("")
    with pytest.raises(registry.UnknownLanguageError):
        support.require_supported("cobol")


def test_missing_e2e_fixture_marks_the_language_incomplete(tmp_path: Path):
    status = support.support_status("python", fixture_root=tmp_path)
    assert status.e2e_fixture is False
    assert status.supported is True  # runtime criteria still hold
    assert status.complete is False


def test_fixture_is_absent_outside_a_checkout(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("NAI_LANG_FIXTURES", str(tmp_path / "nowhere"))
    assert support.e2e_fixture_root() is None
    assert support.support_status("python").e2e_fixture is None


def test_unsupported_languages_in_flags_only_unbuildable_languages():
    # Rust and TypeScript are supported adapters now, so a brief naming them is
    # buildable; only genuinely unbuildable languages are flagged.
    assert support.unsupported_languages_in("Build a Rust CLI with TypeScript bindings") == []
    assert support.unsupported_languages_in("A Python 3.12 library") == []
    assert support.unsupported_languages_in("A Java service with Kotlin bindings") == [
        "Java",
        "Kotlin",
    ]
    assert support.unsupported_languages_in("Use javascript for the frontend") == ["JavaScript"]
    # Word boundaries: 'trust' and 'gopher' are not languages.
    assert support.unsupported_languages_in("trust the gopher") == []


def test_supported_languages_block_lists_registry_ids():
    block = support.supported_languages_block()
    assert "## Supported Languages" in block
    for language_id in registry.supported():
        assert f"`{language_id}`" in block
    assert "Never substitute" in block


def test_probe_reports_missing_required_tools():
    result = probe.probe("python", which=lambda name: None)
    assert result.ok is False
    assert result.missing == ("python",)
    assert {tool.name for tool in result.tools} == {"python", "pytest", "ruff"}


def test_probe_uses_fallbacks_and_reports_versions():
    seen: list[list[str]] = []

    def _which(name: str) -> str | None:
        return "/usr/bin/python" if name == "python" else None

    def _run(argv: list[str], cwd: Path) -> tuple[int, str]:
        seen.append(argv)
        return 0, "Python 3.12.4\n"

    result = probe.probe("python", which=_which, run=_run)
    assert result.ok is True
    assert result.tools[0].version == "Python 3.12.4"
    assert seen and seen[0] == ["/usr/bin/python", "--version"]


def test_nai_languages_reports_supported_list_and_probes(capsys):
    from nai.langs_cli import languages_main

    rc = languages_main(["--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["supported"] == support.supported_languages()
    assert payload["registered"] == registry.supported()
    # The exit code reflects whether every declared toolchain is installed on
    # this host; CI installs each language's tools in its own job.
    assert rc == (0 if payload["ok"] else 1)
    entry = next(item for item in payload["languages"] if item["language"] == "python")
    assert entry["status"]["supported"] is True
    assert entry["probes"]["local"]["ok"] is True

    human_rc = languages_main([])
    human = capsys.readouterr().out
    assert human_rc == (0 if payload["ok"] else 1)
    assert "supported languages:" in human
    assert "python" in human


def test_nai_languages_probes_named_workers(capsys, tmp_path: Path):
    from nai.langs_cli import languages_main

    rc = languages_main(["--json", "--worker", f"edge={tmp_path}"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == (0 if payload["ok"] else 1)
    entry = next(item for item in payload["languages"] if item["language"] == "python")
    assert "edge" in entry["probes"]


def test_nai_languages_rejects_malformed_workers(capsys):
    from nai.langs_cli import languages_main

    assert languages_main(["--worker", "no-equals-sign"]) == 2
    assert "NAME=PATH" in capsys.readouterr().err
