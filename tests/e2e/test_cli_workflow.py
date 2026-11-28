"""Pass 9: full CLI workflow against the scripted model server.

The v1 CLI is exercised as a real subprocess (both supported entry points),
and the integrated `nai doctor` CLI is smoke-tested from a clean working dir.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from nai.testing.stub_server import StubServer, call, reply
from tests.core.conftest import ROOT

ENTRYPOINTS = {
    "python -m nai": [sys.executable, "-m", "nai"],
    "devswarm.py": [sys.executable, str(ROOT / "devswarm.py")],
}


def _scripts() -> dict:
    test_file = (
        "from app.hello import hi\n"
        "\n"
        "def test_hi():\n"
        "    assert hi() == 'hi'\n"
    )
    return dict(
        orch=[
            reply(calls=[call("consult_reasoner", conversation_id="architect", message="design?")]),
            reply(calls=[call("delegate_to_coder", task="make hello", context_files=[])]),
            reply(calls=[call("finish", status="success", summary="too early")]),
            reply(calls=[call("run_command", command=f"{sys.executable} -m pytest -q")]),
            reply(calls=[call("finish", status="success", summary="verified")]),
        ],
        consult=[reply("Use one module.")],
        coder=[
            reply(calls=[
                call("write_file", path="app/hello.py", content="def hi():\n    return 'hi'\n"),
                call("write_file", path="test_hello.py", content=test_file),
            ]),
            reply("Done."),
        ],
    )


def _tool_results(stub: StubServer) -> list[str]:
    return [m["content"] for m in stub.on("orch")[-1]["messages"] if m["role"] == "tool"]


def _run_cli(entry: list[str], args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    for key in ("LLAMA_SWAP_URL", "LLAMA_SWAP_KEY", "REASONER_MODEL", "CODER_MODEL", "NAI_CONFIG"):
        env.pop(key, None)
    return subprocess.run([*entry, *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=90)


@pytest.mark.parametrize("entry", list(ENTRYPOINTS), ids=list(ENTRYPOINTS))
def test_cli_brief_to_verified_workspace(entry: str, tmp_path: Path):
    """A brief becomes a workspace with code, a passing test, and a success exit."""
    (tmp_path / "brief.md").write_text("# brief\nBuild a hello function.\n", encoding="utf-8")
    workspace = tmp_path / "workspace"

    with StubServer(**_scripts()) as stub:
        result = _run_cli(ENTRYPOINTS[entry], [
            "brief.md", "-w", str(workspace), "--reasoner", "r", "--coder", "c",
            "--no-stream", "--no-git", "--base-url", stub.url,
        ], tmp_path)
        tools = _tool_results(stub)
        calls = stub.on("orch")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "status=success" in result.stdout and "verified" in result.stdout
    for rel in ("SPEC.md", "app/hello.py", "test_hello.py", ".agent/state.json", ".agent/transcript.log"):
        assert (workspace / rel).exists(), rel
    assert (workspace / "SPEC.md").read_text(encoding="utf-8").startswith("# brief")
    assert "return 'hi'" in (workspace / "app/hello.py").read_text(encoding="utf-8")
    assert isinstance(json.loads((workspace / ".agent/state.json").read_text(encoding="utf-8")), dict)

    assert len(calls) == 5
    assert "REJECTED: No passing verification" in tools[2]
    assert "exit code: 0" in tools[3]
    assert (workspace / "test_hello.py").exists()


def test_cli_doctor_subprocess(tmp_path: Path):
    """The integrated doctor command works from a clean directory without a model server."""
    result = _run_cli([sys.executable, "-m", "nai"], ["doctor"], tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "nai doctor" in result.stdout
    assert "FAIL" not in result.stdout
    assert "checks passed" in result.stdout
