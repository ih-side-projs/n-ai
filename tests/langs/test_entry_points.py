"""Every entry point rejects unsupported languages and offers only supported ones."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nai.langs import registry, support
from nai.project_cli import project_main
from nai.spec import schemas as spec_schemas
from nai.store import Store, StoreError
from nai.web.app import WebSettings, create_app

SCHEMA_DIR = Path(spec_schemas.__file__).resolve().parent / "schemas"


# ---------------------------------------------------------------------------
# Store: one check that covers CLI, web, eval and every internal caller
# ---------------------------------------------------------------------------


def test_store_rejects_unsupported_language(tmp_path: Path):
    with Store(tmp_path / "db.sqlite3") as store:
        with pytest.raises(StoreError, match="unsupported language"):
            store.create_project("Bad", "brief", settings={"language": "cobol"})
        assert store.list_projects() == []


def test_store_rejects_a_language_list(tmp_path: Path):
    with Store(tmp_path / "db.sqlite3") as store:
        with pytest.raises(StoreError, match="one language per project"):
            store.create_project("Bad", "brief", settings={"language": ["python", "rust"]})


def test_store_defaults_to_the_registry_default(tmp_path: Path):
    with Store(tmp_path / "db.sqlite3") as store:
        project_id = store.create_project("Good", "brief")
        project = store.get_project(project_id)
    assert project["settings"]["language"] == registry.DEFAULT_LANGUAGE
    assert support.is_supported(project["settings"]["language"])


def test_store_validates_language_updates(tmp_path: Path):
    with Store(tmp_path / "db.sqlite3") as store:
        project_id = store.create_project("Good", "brief")
        with pytest.raises(StoreError, match="unsupported language"):
            store.update_project(project_id, settings={"language": "cobol"})
        assert store.get_project(project_id)["settings"]["language"] == "python"

        store.update_project(project_id, settings={"language": "Python"})
        assert store.get_project(project_id)["settings"]["language"] == "python"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_project_create_cli_rejects_unsupported_language(tmp_path: Path, capsys):
    with pytest.raises(SystemExit) as exc:
        project_main([
            "create", "Bad", "--db", str(tmp_path / "db.sqlite3"),
            "--output-dir", str(tmp_path / "out"), "--language", "cobol",
        ])
    assert exc.value.code == 2
    assert "python" in capsys.readouterr().err


def test_project_create_cli_accepts_supported_language(tmp_path: Path):
    db = tmp_path / "db.sqlite3"
    assert project_main([
        "create", "Good", "--db", str(db),
        "--output-dir", str(tmp_path / "out"), "--language", "python",
    ]) == 0
    with Store(db, read_only=True) as store:
        project = store.list_projects()[0]
    assert project["settings"]["language"] == "python"


# ---------------------------------------------------------------------------
# Web form
# ---------------------------------------------------------------------------


def _client(tmp_path: Path) -> tuple[TestClient, Path]:
    db = tmp_path / "nai.sqlite3"
    with Store(db) as store:
        store.list_projects()
    client = TestClient(create_app(settings=WebSettings(database_path=db)))
    return client, db


def _csrf(client: TestClient) -> str:
    assert client.get("/projects").status_code == 200
    token = client.cookies.get("nai_csrf")
    assert token
    return token


def test_web_form_offers_only_supported_languages(tmp_path: Path):
    client, _ = _client(tmp_path)
    page = client.get("/projects")
    assert page.status_code == 200
    assert '<select name="language">' in page.text
    for language_id in support.supported_languages():
        assert f'<option value="{language_id}"' in page.text


def test_web_create_rejects_unsupported_language(tmp_path: Path):
    client, db = _client(tmp_path)
    response = client.post(
        "/projects/create",
        data={"name": "Bad", "brief": "x", "language": "cobol", "csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "unsupported language" in response.text
    with Store(db, read_only=True) as store:
        assert store.list_projects() == []


def test_web_create_records_the_selected_language(tmp_path: Path):
    client, db = _client(tmp_path)
    response = client.post(
        "/projects/create",
        data={"name": "Good", "brief": "x", "language": "python", "csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with Store(db, read_only=True) as store:
        project = store.list_projects()[0]
    assert project["settings"]["language"] == "python"
    assert project["settings"]["intake"]["language"] == "Python 3.12"
    assert project["settings"]["target_env"]["language"] == "Python 3.12"


def test_web_settings_rejects_unsupported_language(tmp_path: Path):
    client, db = _client(tmp_path)
    token = _csrf(client)
    assert client.post(
        "/projects/create",
        data={"name": "Good", "brief": "x", "language": "python", "csrf_token": token},
        follow_redirects=False,
    ).status_code == 303
    with Store(db, read_only=True) as store:
        project_id = store.list_projects()[0]["id"]

    rejected = client.post(
        f"/projects/{project_id}/settings",
        data={"target_language": "cobol", "csrf_token": token},
        follow_redirects=False,
    )
    assert rejected.status_code == 400
    with Store(db, read_only=True) as store:
        assert store.get_project(project_id)["settings"]["language"] == "python"

    accepted = client.post(
        f"/projects/{project_id}/settings",
        data={"target_language": "python", "csrf_token": token},
        follow_redirects=False,
    )
    assert accepted.status_code == 303


def test_import_rejects_a_bundle_with_an_unsupported_language(tmp_path: Path):
    from nai.store import ExportImportError

    source_db = tmp_path / "source.sqlite3"
    bundle = tmp_path / "bundle"
    with Store(source_db) as source:
        project_id = source.create_project("Exportable", "brief", output_root=tmp_path / "out")
        source.create_node(project_id, "root", "system", spec_text="# Root\n")
        source.export_project(project_id, bundle)

    manifest_file = bundle / "manifest.json"
    payload = json.loads(manifest_file.read_text(encoding="utf-8"))
    payload["tables"]["projects"][0]["settings_json"] = json.dumps({"language": "cobol"})
    manifest_file.write_text(json.dumps(payload), encoding="utf-8")

    with Store(tmp_path / "target.sqlite3") as target:
        with pytest.raises(ExportImportError, match="unsupported language"):
            target.import_project(bundle, workspace_path=tmp_path / "imported")
        assert target.list_projects() == []


def test_import_defaults_the_language_for_older_bundles(tmp_path: Path):
    source_db = tmp_path / "source.sqlite3"
    bundle = tmp_path / "bundle"
    with Store(source_db) as source:
        project_id = source.create_project("Legacy Bundle", "brief", output_root=tmp_path / "out")
        source.create_node(project_id, "root", "system", spec_text="# Root\n")
        source.export_project(project_id, bundle)

    manifest_file = bundle / "manifest.json"
    payload = json.loads(manifest_file.read_text(encoding="utf-8"))
    payload["tables"]["projects"][0]["settings_json"] = json.dumps({})
    manifest_file.write_text(json.dumps(payload), encoding="utf-8")

    with Store(tmp_path / "target.sqlite3") as target:
        imported = target.import_project(bundle, workspace_path=tmp_path / "imported")
        assert target.get_project(imported)["settings"]["language"] == "python"


# ---------------------------------------------------------------------------
# Contract schema
# ---------------------------------------------------------------------------


def test_contract_schema_language_enum_is_generated_from_the_registry():
    contract = spec_schemas.INTERFACES_SCHEMA["properties"]["contract"]["properties"]
    assert contract["language"]["enum"] == support.supported_languages()
    assert contract["components"]["items"]["properties"]["platform"]["enum"] == [
        "any", "linux", "macos",
    ]


def test_contract_schema_on_disk_matches_the_generated_enum():
    on_disk = json.loads((SCHEMA_DIR / "contract.json").read_text(encoding="utf-8"))
    assert on_disk["properties"]["language"]["enum"] == spec_schemas.SUPPORTED_LANGUAGES
    assert on_disk["properties"]["components"]["items"]["properties"]["platform"]["enum"] == [
        "any", "linux", "macos",
    ]
