"""Pass 8: integration self-check command."""
from __future__ import annotations

import json

import pytest

from nai import cli, doctor
from nai.store import store as store_mod


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
