"""Handler integration: routing helpers, coder policy and the syntax pre-check."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from nai.build.jobs import (
    _argv_targets,
    _coder_command_policy,
    _remote_target,
    _syntax_precheck,
)
from nai.remote.target import RemoteTarget


class FakeCtx:
    """The slice of RunContext the routing helpers touch."""

    def __init__(self, store, project, root: Path):
        self.store = store
        self.project = project
        self.job = {"id": "job-1", "attempt": 1}
        self.ws = SimpleNamespace(root=root, exec_target=None)
        self.events: list[tuple[str, str, str, dict | None]] = []

    def event(self, event_type, message, *, level="info", data=None):
        self.events.append((event_type, message, level, data))

    def kinds(self) -> list[str]:
        return [event[0] for event in self.events]


def _project(store, tmp_path, *, enabled: bool = True, coder_allow_bash: bool = False) -> dict:
    settings: dict = {"coder_allow_bash": coder_allow_bash}
    if enabled:
        settings["remote"] = {"enabled": True, "worker_wait_seconds": 0.1}
    pid = store.create_project("routed", "brief", workspace_path=tmp_path / "ws", settings=settings)
    return store.get_project(pid)


def _macos_node(store, project, tmp_path) -> dict:
    root = store.create_node(project["id"], "root", "system",
                             contract={"components": [{"name": "mac-bit", "platform": "macos"}]})
    leaf = store.create_node(project["id"], "mac-bit", "leaf", parent_id=root)
    return store.get_node(leaf)


def test_argv_targets_extracts_workspace_paths(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    argv = ["/outside/venv/bin/python", "-m", "pytest", "tests/test_a.py", "src", "-q", "--opt=1"]
    assert _argv_targets(argv, tmp_path) == ["tests/test_a.py", "src"]


def test_syntax_precheck_reports_the_adapter_problem(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mod.py").write_text("def broken(:\n", encoding="utf-8")

    class Adapter:
        def check_syntax(self, code: str, filename: str) -> str | None:
            return "syntax error" if "broken" in code else None

    problem = _syntax_precheck(tmp_path, ("src/mod.py", "src/**"), Adapter())
    assert problem and problem.startswith("src/mod.py: syntax error")
    assert _syntax_precheck(tmp_path, ("src/missing.py",), Adapter()) is None


def test_coder_command_policy_disables_run_command_for_macos(store, tmp_path) -> None:
    project = _project(store, tmp_path, coder_allow_bash=True)
    ctx = FakeCtx(store, project, tmp_path / "ws")
    node = _macos_node(store, project, tmp_path)
    allow_bash, macos_remote = _coder_command_policy(ctx, node)
    assert (allow_bash, macos_remote) == (False, True)
    assert "remote.run_command_disabled" in ctx.kinds()


def test_coder_command_policy_keeps_run_command_elsewhere(store, tmp_path) -> None:
    project = _project(store, tmp_path, coder_allow_bash=True)
    ctx = FakeCtx(store, project, tmp_path / "ws")
    node = store.get_node(store.create_node(project["id"], "root", "system"))
    allow_bash, macos_remote = _coder_command_policy(ctx, node)
    assert (allow_bash, macos_remote) == (True, False)
    assert ctx.kinds() == []


def test_remote_target_is_built_for_a_macos_component(store, tmp_path) -> None:
    project = _project(store, tmp_path)
    ctx = FakeCtx(store, project, tmp_path / "ws")
    node = _macos_node(store, project, tmp_path)
    target = _remote_target(ctx, node, "verify", "verify", params={"targets": ["tests/a.py"]})
    try:
        assert isinstance(target, RemoteTarget)
        assert target.recipe == "verify"
        assert "macos" in target.required_caps and "python" in target.required_caps
        assert target.worker_wait_seconds == 0.1
        assert "remote.routed" in ctx.kinds()
    finally:
        assert target is not None
        target.close()


def test_remote_target_is_none_when_routing_does_not_apply(store, tmp_path) -> None:
    disabled = _project(store, tmp_path, enabled=False)
    ctx = FakeCtx(store, disabled, tmp_path / "ws")
    node = _macos_node(store, disabled, tmp_path)
    assert _remote_target(ctx, node, "verify", "verify") is None
    assert "remote.disabled" in ctx.kinds()

    enabled = _project(store, tmp_path / "second", enabled=True)
    ctx = FakeCtx(store, enabled, tmp_path / "ws2")
    plain = store.get_node(store.create_node(enabled["id"], "root", "system"))
    assert _remote_target(ctx, plain, "verify", "verify") is None
