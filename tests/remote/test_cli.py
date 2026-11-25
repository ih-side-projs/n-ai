"""``nai worker add|list|revoke`` and ``nai remote serve|status``."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from nai.remote.cli import remote_cli, worker_cli
from nai.store import Store


def _token_from(output: str) -> str:
    match = re.search(r"token \(shown once; store it now\):\n(\S+)", output)
    assert match, output
    return match.group(1)


def test_worker_add_prints_the_token_once_and_stores_a_hash(store: Store, capsys) -> None:
    assert worker_cli(["add", "macbook", "--capabilities", "macos,python", "--db", str(store.db_path)]) == 0
    output = capsys.readouterr().out
    token = _token_from(output)
    assert "worker macbook added" in output
    record = store.worker_for_token(token)
    assert record is not None and record["name"] == "macbook"
    stored = store.conn.execute("SELECT token_hash FROM workers").fetchone()["token_hash"]
    assert token not in stored and len(stored) == 64


def test_worker_add_rejects_a_duplicate_name(store: Store, capsys) -> None:
    assert worker_cli(["add", "macbook", "--db", str(store.db_path)]) == 0
    capsys.readouterr()
    assert worker_cli(["add", "macbook", "--db", str(store.db_path)]) == 1
    assert "ERROR" in capsys.readouterr().err


def test_worker_list_and_revoke(store: Store, worker, capsys) -> None:
    assert worker_cli(["list", "--db", str(store.db_path)]) == 0
    output = capsys.readouterr().out
    assert "macbook" in output and "online" in output

    assert worker_cli(["list", "--json", "--db", str(store.db_path)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["name"] == "macbook" and payload[0]["online"] is True

    assert worker_cli(["revoke", "macbook", "--db", str(store.db_path)]) == 0
    assert "revoked" in capsys.readouterr().out
    assert store.get_worker("macbook")["revoked"] is True
    assert worker_cli(["revoke", "ghost", "--db", str(store.db_path)]) == 1


def test_worker_cli_requires_an_existing_database(tmp_path: Path, capsys) -> None:
    missing = tmp_path / "nope.sqlite3"
    assert worker_cli(["list", "--db", str(missing)]) == 1
    assert "database not found" in capsys.readouterr().err


def test_remote_status_summarises_workers_and_tasks(store: Store, project, worker, capsys) -> None:
    store.create_remote_task(project["id"], "echo", "a" * 64,
                             params={"params": {"message": "x"}}, required_caps=["macos"])
    assert remote_cli(["status", "--db", str(store.db_path)]) == 0
    output = capsys.readouterr().out
    assert "workers: 1" in output and "recent tasks: 1" in output and "echo" in output

    assert remote_cli(["status", "--json", "--db", str(store.db_path)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["workers"][0]["name"] == "macbook"
    assert payload["tasks"][0]["recipe"] == "echo"


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.20"])
def test_remote_serve_refuses_a_plain_lan_bind(store: Store, host: str, capsys) -> None:
    assert remote_cli(["serve", "--host", host, "--db", str(store.db_path)]) == 2
    assert "refusing to bind" in capsys.readouterr().err
