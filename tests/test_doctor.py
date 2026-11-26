"""Pass 8: integration self-check command."""
from __future__ import annotations

import json

import pytest

from nai import cli, doctor
from nai.store import store as store_mod


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """Keep doctor's database/output-root checks off the developer's real state."""
    monkeypatch.setenv(doctor.DB_ENV, str(tmp_path / ".nai" / "nai.sqlite3"))
    monkeypatch.setenv(doctor.OUTPUT_ENV, str(tmp_path / "outputs"))
    monkeypatch.delenv(doctor.DOCKER_ENV, raising=False)
    doctor._MODEL_PROBE_CACHE.clear()


def test_all_checks_pass():
    results = doctor.run_checks()
    failed = [f"{r.name}: {r.detail}" for r in results if not r.ok]
    assert not failed, failed
    assert {r.name for r in results} == {name for name, _ in doctor.CHECKS}


def test_report_and_json_output(capsys):
    assert doctor.main([]) == 0
    out = capsys.readouterr().out
    assert "nai doctor" in out
    total = len(doctor.CHECKS)
    assert f"{total}/{total} checks passed" in out

    assert doctor.main(["--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload) == len(doctor.CHECKS)
    assert all(item["ok"] for item in payload)


def test_cli_subcommand_and_flag(capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["doctor"])
    assert exit_info.value.code == 0
    assert "nai doctor" in capsys.readouterr().out

    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--doctor"])
    assert exit_info.value.code == 0
    assert "checks passed" in capsys.readouterr().out


def test_planner_probe_detects_missing_context_api():
    def bad_planner(pc):
        return pc.active_projects()

    result = doctor._planner_probe("bad", bad_planner)
    assert not result.ok
    assert "active_projects" in result.detail


def test_artifact_kind_check_detects_invalid_literal(monkeypatch):
    monkeypatch.setattr(store_mod, "ARTIFACT_KINDS", {"contract"})
    result = doctor.check_artifact_kinds()
    assert not result.ok
    assert "invalid" not in result.detail.lower() or result.detail
    assert "spec" in result.detail or "code" in result.detail or "test" in result.detail


# ---------------------------------------------------------------------------
# operability checks: database, output root, models, toolchains
# ---------------------------------------------------------------------------


def test_store_path_accepts_a_migrated_database(tmp_path, monkeypatch):
    from nai.store import Store

    db = tmp_path / "nai.sqlite3"
    Store(db, read_only=False).close()
    monkeypatch.setenv(doctor.DB_ENV, str(db))
    result = doctor.check_store_path()
    assert result.ok, result.detail
    assert "migrations applied" in result.detail


def test_store_path_reports_a_stale_database(tmp_path, monkeypatch):
    import sqlite3

    db = tmp_path / "nai.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE schema_migrations (version TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()
    monkeypatch.setenv(doctor.DB_ENV, str(db))
    result = doctor.check_store_path()
    assert not result.ok
    assert "0/" in result.detail and "migrations applied" in result.detail


def test_store_path_accepts_a_missing_database_in_a_writable_parent(tmp_path, monkeypatch):
    monkeypatch.setenv(doctor.DB_ENV, str(tmp_path / "nested" / "nai.sqlite3"))
    result = doctor.check_store_path()
    assert result.ok, result.detail
    assert "not created yet" in result.detail


def test_store_path_rejects_a_directory(tmp_path, monkeypatch):
    target = tmp_path / "nai.sqlite3"
    target.mkdir()
    monkeypatch.setenv(doctor.DB_ENV, str(target))
    assert not doctor.check_store_path().ok


def test_output_root_accepts_a_configured_directory(tmp_path, monkeypatch):
    monkeypatch.setenv(doctor.OUTPUT_ENV, str(tmp_path / "outputs"))
    result = doctor.check_output_root()
    assert result.ok, result.detail
    assert "will be created" in result.detail or "writable" in result.detail


def test_output_root_rejects_the_filesystem_root(monkeypatch):
    monkeypatch.setenv(doctor.OUTPUT_ENV, "/")
    result = doctor.check_output_root()
    assert not result.ok
    assert "filesystem root" in result.detail


def test_output_root_defaults_next_to_the_state_directory(tmp_path, monkeypatch):
    monkeypatch.delenv(doctor.OUTPUT_ENV, raising=False)
    monkeypatch.setenv(doctor.DB_ENV, str(tmp_path / ".nai" / "nai.sqlite3"))
    result = doctor.check_output_root()
    assert result.ok, result.detail
    assert str(tmp_path / "outputs") in result.detail



def _reachable_models(monkeypatch, *, reachable: bool):
    monkeypatch.setattr(
        doctor, "_probe_models",
        lambda base, key: (
            reachable,
            "HTTP 200, 2 model(s)" if reachable else "unreachable (ConnectError)",
        ),
    )
    doctor._MODEL_PROBE_CACHE.clear()


def test_models_reports_a_reachable_server(monkeypatch):
    _reachable_models(monkeypatch, reachable=True)
    result = doctor.check_models()
    assert result.ok and not result.warning
    assert "2 model(s)" in result.detail


def test_models_is_advisory_when_the_server_is_down(monkeypatch):
    _reachable_models(monkeypatch, reachable=False)
    result = doctor.check_models()
    assert result.ok and result.warning
    assert "llama-swap" in result.detail
    strict = doctor.check_models(strict=True)
    assert not strict.ok and not strict.warning


def test_models_offline_skips_the_probe(monkeypatch):
    def _boom(base, key):  # pragma: no cover - must not be called
        raise AssertionError("probe must not run with --offline")

    monkeypatch.setattr(doctor, "_probe_models", _boom)
    result = doctor.check_models(offline=True)
    assert result.ok and "not probed" in result.detail


def test_models_reports_a_broken_config(monkeypatch):
    import nai.config as config_mod

    def _boom(*args, **kwargs):
        raise config_mod.ConfigError("bad nai.toml")

    monkeypatch.setattr(config_mod, "load_config", _boom)
    result = doctor.check_models()
    assert not result.ok
    assert "ConfigError" in result.detail


def test_language_toolchains_is_advisory_by_default(monkeypatch):
    from nai.langs import probe as probe_mod

    monkeypatch.setattr(
        probe_mod, "probe",
        lambda language_id, root=None: probe_mod.ProbeResult(
            language=language_id,
            root=str(root or "."),
            tools=(probe_mod.ToolStatus(name="missing", required=True, found=False),),
        ),
    )
    result = doctor.check_language_toolchains()
    assert result.ok and result.warning
    assert "missing" in result.detail
    assert not doctor.check_language_toolchains(strict=True).ok


def test_language_toolchains_are_ready_when_every_probe_passes(monkeypatch):
    from nai.langs import probe as probe_mod

    monkeypatch.setattr(
        probe_mod, "probe",
        lambda language_id, root=None: probe_mod.ProbeResult(
            language=language_id,
            root=str(root or "."),
            tools=(probe_mod.ToolStatus(name="ok", required=True, found=True),),
        ),
    )
    result = doctor.check_language_toolchains()
    assert result.ok and not result.warning
    assert "local" in result.detail


def test_language_toolchains_notes_the_docker_image(monkeypatch):
    monkeypatch.setenv(doctor.DOCKER_ENV, "nai-sandbox:latest")
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(doctor, "_docker_image_present", lambda image: True)
    result = doctor.check_language_toolchains()
    assert result.ok
    assert "docker image nai-sandbox:latest" in result.detail


def test_doctor_rejects_a_malformed_worker(capsys):
    assert doctor.main(["--worker", "no-equals-sign"]) == 2
    assert "NAME=PATH" in capsys.readouterr().err


def test_doctor_reports_warnings_in_text_and_json(capsys, monkeypatch):
    _reachable_models(monkeypatch, reachable=False)
    assert doctor.main(["--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    models = next(item for item in payload if item["name"] == "models")
    assert models["ok"] is True and models["warning"] is True

    assert doctor.main([]) == 0
    out = capsys.readouterr().out
    assert "[WARN] models:" in out
    assert "warning(s)" in out


def test_package_data_includes_the_vendored_headers():
    assert "langs/vendor/*.h" in doctor.PACKAGE_GLOBS
    result = doctor.check_package_data()
    assert result.ok, result.detail

