"""Worker config, argv rendering, process control and work-directory handling."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

from nai.exec.target import scrub_env
from nai.remote.errors import RecipeError
from nai.remote.snapshots import snapshot_tree
from nai.remote.worker import (
    WorkerAgent,
    WorkerConfig,
    prepare_workdir,
    render_argv,
    run_process,
    write_config,
)

BASE = """
[worker]
name = "macbook"
server = "http://127.0.0.1:8765"
token = "token-1"
work_dir = "{work_dir}"
capabilities = ["macos", "python"]
python = "{python}"
heartbeat_seconds = 5
claim_wait = 1

[recipes.echo]
argv = ["echo", "{{message}}"]

[recipes.verify]
argv = ["{{python}}", "-m", "pytest", "{{targets}}", "-q"]
timeout = 60
"""


def _base_text(tmp_path: Path) -> str:
    """The template with placeholders expanded, so tests can patch real text."""
    return BASE.format(work_dir=tmp_path / "work", python=sys.executable)


def _write(tmp_path: Path, text: str | None = None) -> Path:
    path = tmp_path / "worker.toml"
    path.write_text(text or _base_text(tmp_path), encoding="utf-8")
    return path


def test_config_parses_recipes_and_capabilities(tmp_path: Path) -> None:
    config = WorkerConfig.from_toml(_write(tmp_path))
    assert config.name == "macbook" and config.server_url == "http://127.0.0.1:8765"
    assert config.capabilities == ("macos", "python")
    assert sorted(config.recipes) == ["echo", "verify"]
    assert config.recipe("verify").timeout == 60
    assert config.variables["python"] == sys.executable


def test_token_can_come_from_a_file(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("file-token\n", encoding="utf-8")
    text = _base_text(tmp_path).replace('token = "token-1"', f'token_file = "{token_file}"')
    config = WorkerConfig.from_toml(_write(tmp_path, text))
    assert config.token == "file-token"


@pytest.mark.parametrize(
    "replacement, message",
    [
        ('[recipes.echo]\nargv = ["echo", "{message}"]\n\n[recipes.nope]\nargv = ["true"]', "unknown recipe"),
        ('[recipes.echo]\nargv = ["echo", "{mystery}"]', "unknown placeholder"),
        ('[recipes.echo]\nargv = ["bash", "-c", "echo hi"]', "may not invoke a shell"),
        ('[recipes.echo]\nargv = []', "non-empty array"),
        ('[recipes.echo]\nargv = "echo hi"', "non-empty array"),
    ],
)
def test_bad_recipe_tables_are_rejected(tmp_path: Path, replacement: str, message: str) -> None:
    text = _base_text(tmp_path).replace('[recipes.echo]\nargv = ["echo", "{message}"]', replacement)
    with pytest.raises(RecipeError, match=message):
        WorkerConfig.from_toml(_write(tmp_path, text))


def test_missing_token_and_server_are_rejected(tmp_path: Path) -> None:
    base = _base_text(tmp_path)
    with pytest.raises(RecipeError, match="token"):
        WorkerConfig.from_toml(_write(tmp_path, base.replace('token = "token-1"', "")))
    with pytest.raises(RecipeError, match="server"):
        WorkerConfig.from_toml(_write(tmp_path, base.replace('server = "http://127.0.0.1:8765"', "")))


def test_render_uses_config_variables_and_params(tmp_path: Path) -> None:
    config = WorkerConfig.from_toml(_write(tmp_path))
    argv = render_argv(config.recipe("verify").spec, config.recipe("verify").argv,
                       {"targets": ["tests/a.py"]}, config.variables)
    assert argv == [sys.executable, "-m", "pytest", "tests/a.py", "-q"]
    with pytest.raises(RecipeError):
        render_argv(config.recipe("echo").spec, config.recipe("echo").argv,
                    {"message": "a;b"}, config.variables)


def test_run_process_captures_output(tmp_path: Path) -> None:
    outcome = run_process((sys.executable, "-c", "print('hello')"), cwd=tmp_path,
                          env=scrub_env(), timeout=30)
    assert outcome.exit_code == 0 and "hello" in outcome.output
    assert not outcome.timed_out and not outcome.cancelled


def test_run_process_times_out_and_kills(tmp_path: Path) -> None:
    started = time.monotonic()
    outcome = run_process((sys.executable, "-c", "import time; time.sleep(30)"), cwd=tmp_path,
                          env=scrub_env(), timeout=0.3)
    assert outcome.timed_out and outcome.exit_code == 124
    assert time.monotonic() - started < 15


def test_run_process_cancel_kills_the_process_group(tmp_path: Path) -> None:
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    started = time.monotonic()
    outcome = run_process((sys.executable, "-c", "import time; time.sleep(30)"), cwd=tmp_path,
                          env=scrub_env(), timeout=60, cancel_event=cancel)
    assert outcome.cancelled and outcome.exit_code == 130
    assert time.monotonic() - started < 15


def test_run_process_scrubs_secret_environment(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LLAMA_SWAP_KEY", "super-secret")
    monkeypatch.setenv("NAI_WEB_TOKEN", "web-secret")
    script = ("import os; print(os.environ.get('LLAMA_SWAP_KEY', 'MISSING'),"
              " os.environ.get('NAI_WEB_TOKEN', 'MISSING'))")
    outcome = run_process((sys.executable, "-c", script), cwd=tmp_path, env=scrub_env(), timeout=30)
    assert "MISSING MISSING" in outcome.output
    assert "super-secret" not in outcome.output


def test_prepare_workdir_replaces_the_tree_but_keeps_caches(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.py").write_text("x = 1\n", encoding="utf-8")
    snap = snapshot_tree(source, (".venv", ".nai"), dest_dir=tmp_path / "snaps")
    base = tmp_path / "projects" / "p1"
    (base / ".venv").mkdir(parents=True)
    (base / ".venv" / "marker").write_text("keep", encoding="utf-8")
    (base / "stale.py").write_text("old", encoding="utf-8")
    workdir = prepare_workdir(base, snap.path, cache_dirs=(".venv",))
    assert (workdir / "a.py").read_text(encoding="utf-8") == "x = 1\n"
    assert not (workdir / "stale.py").exists()
    assert (workdir / ".venv" / "marker").read_text(encoding="utf-8") == "keep"


def test_write_config_is_private_and_refuses_overwrite(tmp_path: Path) -> None:
    path = write_config(tmp_path / "worker.toml", name="mac", server="http://x:1", token="t")
    assert path.stat().st_mode & 0o777 == 0o600
    config = WorkerConfig.from_toml(path)
    assert config.name == "mac" and "echo" in config.recipes
    with pytest.raises(RecipeError, match="already exists"):
        write_config(path, name="mac", server="http://x:1", token="t")
    write_config(path, name="mac", server="http://x:1", token="t", force=True)



def test_worker_params_narrow_the_registry(tmp_path: Path) -> None:
    text = _base_text(tmp_path).replace(
        '[recipes.verify]\nargv = ["{python}", "-m", "pytest", "{targets}", "-q"]',
        '[recipes.verify]\nparams = ["targets"]\nargv = ["{python}", "-m", "pytest", "{targets}", "-q"]',
    )
    config = WorkerConfig.from_toml(_write(tmp_path, text))
    assert set(config.recipe("verify").spec.params) == {"targets"}
    # ``args`` is now outside the worker's allowlist.
    with pytest.raises(RecipeError, match="does not accept"):
        render_argv(config.recipe("verify").spec, config.recipe("verify").argv,
                    {"targets": ["a.py"], "args": ["-x"]}, config.variables)


def test_worker_params_cannot_widen_or_drop_required(tmp_path: Path) -> None:
    base = _base_text(tmp_path)
    widened = base.replace('[recipes.echo]\nargv', '[recipes.echo]\nparams = ["message", "extra"]\nargv')
    with pytest.raises(RecipeError, match="unknown parameters"):
        WorkerConfig.from_toml(_write(tmp_path, widened))
    dropped = base.replace('[recipes.echo]\nargv', '[recipes.echo]\nparams = ["targets"]\nargv')
    with pytest.raises(RecipeError, match="unknown parameters"):
        WorkerConfig.from_toml(_write(tmp_path, dropped))
    empty = base.replace('[recipes.echo]\nargv', '[recipes.echo]\nparams = []\nargv')
    with pytest.raises(RecipeError, match="non-empty array"):
        WorkerConfig.from_toml(_write(tmp_path, empty))


def test_agent_survives_transport_failures(tmp_path: Path) -> None:
    class BoomClient:
        def post(self, *args, **kwargs):
            raise RuntimeError("connection refused")

        def get(self, *args, **kwargs):
            raise RuntimeError("connection refused")

    config = WorkerConfig.from_toml(_write(tmp_path))
    agent = WorkerAgent(config, client=BoomClient(), log=lambda message: None)
    threading.Timer(0.3, agent.request_stop).start()
    assert agent.run_forever() == 0
