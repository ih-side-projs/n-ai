"""Tests for the deterministic project layout, migration and manifest (prompt 1)."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from nai import layout, manifest, paths
from nai.layout_cli import relocate_main, where_main
from nai.store import Store


def _tree(root: Path) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())


def _make_project(tmp_path: Path, name: str = "My Project", output: str = "out") -> tuple[Store, str, Path]:
    store = Store(tmp_path / ".nai" / "nai.sqlite3")
    pid = store.create_project(name, "brief", output_root=tmp_path / output)
    root_id = store.create_node(pid, "root", "system", spec_text="# Root spec\n")
    return store, pid, root_id


def test_layout_functions_are_deterministic_and_pure(tmp_path: Path):
    assert layout.slugify("  My Project!  ") == "my-project"
    assert layout.slugify("") == "project"
    assert layout.unique_slug("alpha", ["alpha", "alpha-2"]) == "alpha-3"
    assert layout.node_path(["billing", "invoice"]) == "billing/invoice"
    root = tmp_path / "out" / "demo"
    assert layout.project_root(tmp_path / "out", "demo") == root
    assert layout.spec_path(root) == root / "SPEC.md"
    assert layout.spec_path(root, "billing/invoice") == root / "specs" / "billing" / "invoice" / "SPEC.md"
    assert layout.src_dir(root, "billing") == root / "src" / "billing"
    assert layout.test_path(root, "billing", "test_x.py") == root / "tests" / "billing" / "test_x.py"
    assert layout.manifest_path(root) == root / ".nai-manifest.json"
    assert layout.marker_path(root) == root / ".nai-project"
    assert layout.python_source_rel({"name": "Tiny Library"}, {"name": "text-normalizer"}) == (
        "src/tiny_library/text_normalizer.py"
    )
    assert layout.python_test_rel({"name": "text-normalizer"}) == "tests/test_text_normalizer.py"


def test_two_runs_produce_identical_tree(tmp_path: Path):
    trees: list[list[str]] = []
    for index in (1, 2):
        store, pid, root_id = _make_project(tmp_path / f"run{index}")
        try:
            alpha = store.create_node(pid, "alpha", "component", parent_id=root_id, spec_text="# Alpha\n")
            store.create_node(pid, "beta", "component", parent_id=root_id, spec_text="# Beta\n")
            store.update_node_contract(alpha, {"components": []})
            project = store.get_project(pid)
        finally:
            store.close()
        trees.append(_tree(Path(project["workspace_path"])))
    assert trees[0] == trees[1]
    assert "SPEC.md" in trees[0]
    assert "specs/alpha/SPEC.md" in trees[0]
    assert "specs/beta/SPEC.md" in trees[0]
    assert ".nai-project" in trees[0]
    assert ".nai-manifest.json" in trees[0]


def test_project_slug_collisions_are_suffixed_once(tmp_path: Path):
    store = Store(tmp_path / ".nai" / "nai.sqlite3")
    try:
        first = store.create_project("Same Name", output_root=tmp_path / "out")
        second = store.create_project("Same Name", output_root=tmp_path / "out")
        third = store.create_project("Same Name", output_root=tmp_path / "out")
        slugs = [store.get_project(pid)["slug"] for pid in (first, second, third)]
        assert slugs == ["same-name", "same-name-2", "same-name-3"]
        assert len({store.get_project(pid)["workspace_path"] for pid in (first, second, third)}) == 3
    finally:
        store.close()


def test_rename_never_moves_files_or_changes_slug(tmp_path: Path):
    store, pid, root_id = _make_project(tmp_path)
    try:
        child = store.create_node(pid, "alpha", "component", parent_id=root_id, spec_text="# Alpha\n")
        project = store.get_project(pid)
        before = store.get_node(child)
        source = layout.python_source_path(project["workspace_path"], project, before)
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("value = 1\n", encoding="utf-8")
        manifest.record(project["workspace_path"], layout.relative_to_root(project["workspace_path"], source),
                        node_id=child, stage="implement")
        store.update_node_name(child, "renamed-component")
        after = store.get_node(child)
        assert after["name"] == "renamed-component"
        assert after["slug"] == before["slug"] == "alpha"
        assert after["path"] == before["path"] == "alpha"
        assert source.is_file()
        assert layout.spec_path(project["workspace_path"], after["path"]).is_file()
    finally:
        store.close()


def test_traversal_and_symlink_escape_are_rejected(tmp_path: Path):
    for bad in ("../outside", "/etc/passwd", "a/../../b", "", "a\x00b"):
        with pytest.raises(ValueError):
            layout.relative_parts(bad)
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    link = root / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are not available on this platform")
    with pytest.raises(paths.LayoutError):
        paths.resolve_relative(root, "link/secret.txt")
    assert paths.resolve_relative(root, "safe.txt").name == "safe.txt"


def test_atomic_write_refuses_symlinked_parent_escape(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (root / "specs").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are not available on this platform")
    with pytest.raises(paths.LayoutError):
        paths.write_atomic(root / "specs" / "SPEC.md", "secret", root=root)
    assert not (outside / "SPEC.md").exists()


def test_dangerous_output_roots_are_refused():
    for bad in (Path("/"), Path.home(), Path("/etc"), Path("/usr")):
        with pytest.raises(paths.LayoutError):
            paths.validate_output_root(bad, create=False)


def test_marker_mismatch_and_empty_directory_rules(tmp_path: Path):
    out = tmp_path / "out"
    empty = out / "empty-project"
    empty.mkdir(parents=True)
    prepared = paths.prepare_project_dir(out, "empty-project", project_id="p1", name="Empty")
    assert prepared == empty
    assert paths.read_marker(prepared)["project_id"] == "p1"

    occupied = out / "occupied"
    occupied.mkdir()
    (occupied / "existing.txt").write_text("x", encoding="utf-8")
    with pytest.raises(paths.LayoutError):
        paths.prepare_project_dir(out, "occupied", project_id="p2")

    paths.write_marker(occupied, project_id="other", slug="other")
    with pytest.raises(paths.LayoutError):
        paths.prepare_project_dir(out, "occupied", project_id="p2")


def test_hand_edit_is_detected_and_marks_node_stale(tmp_path: Path):
    store, pid, root_id = _make_project(tmp_path)
    try:
        project = store.get_project(pid)
        workspace = Path(project["workspace_path"])
        target = workspace / "src" / "app.py"
        target.parent.mkdir(parents=True)
        target.write_text("value = 1\n", encoding="utf-8")
        manifest.record(workspace, "src/app.py", node_id=root_id, stage="implement")
        target.write_text("value = 2\n", encoding="utf-8")
        mismatches = manifest.verify(workspace)
        assert [(m.relpath, m.reason) for m in mismatches] == [("src/app.py", "modified")]
        detected = manifest.detect_hand_edits(store, store.get_project(pid))
        assert len(detected) == 1
        assert any(e["type"] == "project.hand_edit" for e in store.list_events(pid))
        assert store.get_node(root_id)["status"] == "stale"
    finally:
        store.close()


def test_relocation_moves_tree_and_verifies_manifest(tmp_path: Path):
    store, pid, root_id = _make_project(tmp_path, name="Move Me")
    try:
        project = store.get_project(pid)
        workspace = Path(project["workspace_path"])
        (workspace / "src").mkdir()
        (workspace / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
        manifest.record(workspace, "src/app.py", node_id=root_id, stage="implement")
        target = store.relocate_project(pid, tmp_path / "new-root")
        assert target == tmp_path / "new-root" / "move-me"
        assert (target / "src" / "app.py").read_text(encoding="utf-8") == "x = 1\n"
        assert not workspace.exists()
        moved = store.get_project(pid)
        assert Path(moved["workspace_path"]) == target
        assert Path(moved["output_root"]) == tmp_path / "new-root"
        assert manifest.verify(target) == []
    finally:
        store.close()


def test_relocation_refuses_unverified_hand_edits(tmp_path: Path):
    store, pid, root_id = _make_project(tmp_path, name="Dirty")
    try:
        project = store.get_project(pid)
        workspace = Path(project["workspace_path"])
        target = workspace / "src" / "app.py"
        target.parent.mkdir()
        target.write_text("a\n", encoding="utf-8")
        manifest.record(workspace, "src/app.py", node_id=root_id, stage="implement")
        target.write_text("b\n", encoding="utf-8")
        with pytest.raises(Exception):
            store.relocate_project(pid, tmp_path / "new-root")
        assert workspace.exists()
        forced = store.relocate_project(pid, tmp_path / "new-root", force=True)
        assert forced.is_dir()
    finally:
        store.close()


def test_migration_backfills_slugs_and_layout(tmp_path: Path):
    from nai.store import store as store_module

    real = Path(store_module.__file__).parent / "migrations"
    legacy = tmp_path / "legacy-migrations"
    legacy.mkdir()
    for source in sorted(real.glob("000[1-5]_*.sql")):
        shutil.copy(source, legacy / source.name)

    db = tmp_path / ".nai" / "nai.sqlite3"
    db.parent.mkdir()
    old = Store(db, migration_dir=legacy)
    try:
        pid = old.create_project("Legacy Project", "brief", workspace_path=tmp_path / "Legacy Project")
        root_id = old.create_node(pid, "root", "system", spec_text="# Legacy\n")
        child_id = old.create_node(pid, "child-component", "component", parent_id=root_id)
        assert old.schema_version == 5
    finally:
        old.close()

    store = Store(db)
    try:
        assert store.schema_version == 7
        project = store.get_project(pid)
        assert project["slug"] == "legacy-project"
        assert project["settings"]["language"] == "python"
        assert project["output_root"] == str(tmp_path)
        root = store.get_node(root_id)
        child = store.get_node(child_id)
        assert root["slug"] == "root" and root["path"] == ""
        assert child["slug"] == "child-component" and child["path"] == "child-component"
        workspace = Path(project["workspace_path"])
        assert (workspace / ".nai-project").is_file()
        assert (workspace / ".nai-manifest.json").is_file()
    finally:
        store.close()


def test_where_and_relocate_cli(tmp_path: Path, capsys):
    store, pid, _root_id = _make_project(tmp_path, name="CLI Project")
    db = store.db_path
    try:
        assert where_main([pid, "--db", str(db), "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["id"] == pid
        assert payload["slug"] == "cli-project"
        assert payload["manifest_mismatches"] == []

        target = tmp_path / "moved"
        assert relocate_main([pid, str(target), "--db", str(db)]) == 0
        assert (target / "cli-project" / "SPEC.md").is_file()
        assert store.get_project(pid)["output_root"] == str(target)
    finally:
        store.close()


def test_output_root_precedence_env_and_settings(tmp_path: Path, monkeypatch):
    store = Store(tmp_path / ".nai" / "nai.sqlite3")
    try:
        monkeypatch.setenv("NAI_OUTPUT_ROOT", str(tmp_path / "env-root"))
        pid = store.create_project("Env Root Project")
        project = store.get_project(pid)
        assert Path(project["output_root"]) == tmp_path / "env-root"
        assert Path(project["workspace_path"]).parent == tmp_path / "env-root"

        monkeypatch.delenv("NAI_OUTPUT_ROOT")
        settings_root = tmp_path / "settings-root"
        pid2 = store.create_project("Settings Root", settings={"output_root": str(settings_root)})
        project2 = store.get_project(pid2)
        assert Path(project2["output_root"]) == settings_root
    finally:
        store.close()


def test_web_create_rejects_roots_outside_allowlist(tmp_path: Path):
    from fastapi.testclient import TestClient

    from nai.web.app import WebSettings, create_app

    db = tmp_path / ".nai" / "nai.sqlite3"
    Store(db).close()
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    client = TestClient(create_app(settings=WebSettings(database_path=db, output_roots=(allowed,))))
    page = client.get("/projects")
    token = client.cookies.get("nai_csrf")
    assert page.status_code == 200 and token

    denied = client.post(
        "/projects/create",
        data={"name": "Denied", "brief": "x", "output_root": str(outside), "csrf_token": token},
        follow_redirects=False,
    )
    assert denied.status_code == 400

    allowed_response = client.post(
        "/projects/create",
        data={"name": "Allowed", "brief": "x", "output_root": str(allowed), "csrf_token": token},
        follow_redirects=False,
    )
    assert allowed_response.status_code == 303
    with Store(db, read_only=True) as store:
        project = store.list_projects()[0]
    assert Path(project["output_root"]) == allowed
    assert Path(project["workspace_path"]) == allowed / "allowed"


def test_git_checkpoint_initializes_and_commits_done(tmp_path: Path):
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    from nai.gitcp import GitCheckpoint

    root = tmp_path / "project"
    root.mkdir()
    (root / "file.txt").write_text("hello\n", encoding="utf-8")
    checkpoint = GitCheckpoint(root)
    checkpoint.checkpoint_job("node-1", "done", 1)
    assert (root / ".git").is_dir()
    assert ".nai/" in (root / ".gitignore").read_text(encoding="utf-8")
    log = subprocess.run(
        ["git", "log", "--format=%s"], cwd=root, capture_output=True, text=True,
    ).stdout
    assert "node-1: done attempt 1" in log


def test_cli_registers_where_and_relocate():
    import nai.cli

    assert "where" in nai.cli.SUBCOMMANDS
    assert "relocate" in nai.cli.SUBCOMMANDS
    assert "project" in nai.cli.SUBCOMMANDS


def test_project_create_cli(tmp_path: Path, capsys):
    """Answer 4: ``nai project create`` uses the Store-backed layout."""
    from nai.project_cli import project_main

    db = tmp_path / "nai.sqlite3"
    out = tmp_path / "out"
    rc = project_main(["create", "CLI Project", "--db", str(db), "--output-dir", str(out), "--brief", "hello"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "created CLI Project" in captured.out

    with Store(db, read_only=True) as store:
        project = store.find_project("CLI Project")
        assert project is not None
        workspace = Path(project["workspace_path"])
        assert workspace == out / "cli-project"
        assert workspace.is_dir()
        assert (workspace / ".nai-project").is_file()
        assert (workspace / ".nai-manifest.json").is_file()

    assert project_main([]) == 2
    assert project_main(["bogus"]) == 2


def test_wheel_contains_layout_module_and_migration(tmp_path: Path):
    pytest.importorskip("build")
    repo = Path(__file__).resolve().parents[1]
    out = tmp_path / "dist"
    proc = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--no-isolation", "--outdir", str(out)],
        cwd=repo, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    wheel = next(out.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
    assert "nai/layout.py" in names
    assert "nai/paths.py" in names
    assert "nai/manifest.py" in names
    assert "nai/project_cli.py" in names
    assert "nai/langs/__init__.py" in names
    assert "nai/langs/base.py" in names
    assert "nai/langs/registry.py" in names
    assert "nai/langs/python.py" in names
    assert "nai/store/migrations/0006_project_layout.sql" in names
    assert "nai/store/migrations/0007_project_language.sql" in names
