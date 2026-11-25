"""Language selection: migration 0007, Store defaults and pipeline wiring."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from nai.build import jobs
from nai.langs import registry
from nai.store import Store
from nai.store import store as store_module


def test_create_project_defaults_to_python(tmp_path: Path):
    store = Store(tmp_path / "nai.sqlite3")
    try:
        project_id = store.create_project("Default Lang", "brief")
        project = store.get_project(project_id)
        assert project["settings"]["language"] == "python"
        assert registry.language_for(project).id == "python"
    finally:
        store.close()


def test_migration_0007_backfills_language(tmp_path: Path):
    real = Path(store_module.__file__).parent / "migrations"
    legacy = tmp_path / "legacy-migrations"
    legacy.mkdir()
    for source in sorted(real.glob("000[1-6]_*.sql")):
        shutil.copy(source, legacy / source.name)

    db = tmp_path / "legacy.sqlite3"
    old = Store(db, migration_dir=legacy)
    try:
        assert old.schema_version == 6
        project_id = old.create_project("Legacy Lang", "brief")
        # Simulate a project written before 0007 added the language default.
        old.conn.execute(
            "UPDATE projects SET settings_json = '{}' WHERE id = ?", (project_id,)
        )
        old.conn.commit()
        assert "language" not in (old.get_project(project_id)["settings"] or {})
    finally:
        old.close()

    store = Store(db)
    try:
        assert store.schema_version == 7
        assert store.get_project(project_id)["settings"]["language"] == "python"
    finally:
        store.close()


def test_pipeline_resolves_the_selected_adapter():
    project = {"name": "Tiny Lib", "settings": {"language": "python"}}
    assert jobs._adapter_for(project).id == "python"
    with pytest.raises(registry.UnknownLanguageError):
        jobs._adapter_for({"name": "Tiny Lib", "settings": {"language": "cobol"}})


def test_env_overrides_come_from_the_adapter(tmp_path: Path):
    adapter = registry.get("python")
    project = {"name": "Tiny Lib", "settings": {"language": "python"}}
    # The adapter owns the mapping; the pipeline forwards it as an environment
    # override to the execution target rather than prefixing a shell command.
    overrides = adapter.env_overrides(project, tmp_path)
    assert overrides == {"PYTHONPATH": "src"}

    captured: dict[str, object] = {}

    class _WS:
        def run_argv(self, argv, **kwargs):
            captured["argv"] = list(argv)
            captured.update(kwargs)
            return 0, "ok"

    class _Ctx:
        ws = _WS()

    rc, out = jobs._run_argv(
        _Ctx(),
        ["pytest", "-q", "tests/test_text.py"],
        phase="run",
        env_overrides=overrides,
    )
    assert (rc, out) == (0, "ok")
    assert captured["env_overrides"] == {"PYTHONPATH": "src"}
    # No shell prefix: the command stays argv all the way to the target.
    assert "PYTHONPATH" not in " ".join(captured["argv"])


def test_python_adapter_declares_its_test_framework():
    assert registry.get("python").test_framework == "pytest"
