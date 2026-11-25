"""CI enforcement of the language support criteria.

A language is supported only when it has an adapter, a complete knowledge pack,
a toolchain probe and an e2e fixture project that CI runs.  These tests run for
every registered adapter, so registering a new adapter without the rest fails
the suite.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nai.langs import knowledge, probe, registry, support

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "langs"


@pytest.mark.parametrize("language_id", registry.supported())
def test_every_registered_language_meets_the_runtime_criteria(language_id: str):
    status = support.support_status(language_id, fixture_root=FIXTURES)
    assert status.adapter, f"{language_id} has no adapter"
    assert status.knowledge, f"{language_id} knowledge pack missing: {status.missing_knowledge}"
    assert status.probe_defined, f"{language_id} declares no toolchain probe"


@pytest.mark.parametrize("language_id", registry.supported())
def test_every_registered_language_has_an_e2e_fixture(language_id: str):
    status = support.support_status(language_id, fixture_root=FIXTURES)
    assert status.e2e_fixture is True, f"missing e2e fixture: {FIXTURES / language_id / 'project.json'}"
    assert status.complete


@pytest.mark.parametrize("language_id", registry.supported())
def test_e2e_fixture_is_wired_to_a_ci_test_module(language_id: str):
    fixture_file = FIXTURES / language_id / "project.json"
    fixture = json.loads(fixture_file.read_text(encoding="utf-8"))
    assert fixture["language"] == language_id
    assert fixture["brief"].strip(), "fixture brief must be non-empty"

    test_path = Path(__file__).resolve().parents[2] / fixture["e2e_test"]
    assert test_path.is_file(), f"e2e test for {language_id} not found: {test_path}"
    source = test_path.read_text(encoding="utf-8")
    # The fixture must be the single source of truth for the e2e run.
    assert "project.json" in source, f"{test_path} does not load its fixture"
    assert "def test_" in source


@pytest.mark.parametrize("language_id", registry.supported())
def test_e2e_fixture_settings_are_accepted_by_the_store(language_id: str, tmp_path: Path):
    from nai.store import Store

    fixture = json.loads((FIXTURES / language_id / "project.json").read_text(encoding="utf-8"))
    with Store(tmp_path / "fixture.sqlite3") as store:
        project_id = store.create_project(
            fixture["name"], fixture["brief"], settings=dict(fixture["settings"]),
        )
        project = store.get_project(project_id)
    assert project["settings"]["language"] == language_id


@pytest.mark.parametrize("language_id", registry.supported())
def test_probe_finds_the_declared_toolchain(language_id: str):
    result = probe.probe(language_id)
    if not result.ok and language_id != registry.DEFAULT_LANGUAGE:
        # Host-dependent: the default language's tools must be present, but a
        # language whose CI job installs its toolchain (Swift) is only probed
        # where that toolchain exists.  ``nai languages`` reports the gap.
        pytest.skip(f"{language_id} toolchain not installed on this host: {', '.join(result.missing)}")
    assert result.ok, f"required tools missing for {language_id}: {', '.join(result.missing)}"


def test_support_criteria_are_derived_from_the_registry():
    assert support.supported_languages() == registry.supported()
    assert set(knowledge.SECTIONS) == {
        "spec", "stub", "test", "impl", "pitfalls", "compile_errors",
    }
