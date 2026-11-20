"""CORE-8 / X8: the six v1 system scenarios (S1-S6) run through the real command line, plus CLI requirements.

Each scenario runs both entry points: the thin `devswarm.py` (v1 command line) and `python -m nai`.
"""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from tests.core.conftest import ROOT, needs_git
from nai.testing.stub_server import StubServer, call, reply

ENTRY = {"devswarm.py": [sys.executable, str(ROOT / "devswarm.py")], "python -m nai": [sys.executable, "-m", "nai"]}


@pytest.fixture(params=list(ENTRY))
def cli(request, tmp_path):
    def run(*args, stdin=None, env=None, timeout=60, cwd=None):
        e = {**os.environ, "PYTHONPATH": str(ROOT), **(env or {})}
        for k in ("LLAMA_SWAP_URL", "LLAMA_SWAP_KEY", "REASONER_MODEL", "CODER_MODEL", "NAI_CONFIG"):
            e.pop(k, None) if not (env and k in env) else None
        return subprocess.run([*ENTRY[request.param], *args], input=stdin, capture_output=True, text=True, timeout=timeout,
                              cwd=cwd or tmp_path, env=e)
    return run


def s1_scripts():
    return dict(
        orch=[reply(calls=[call("consult_reasoner", conversation_id="architect", message="design?")]),
              reply(calls=[call("delegate_to_coder", task="make hello", context_files=["nope.py"])]),
              reply(calls=[call("finish", status="success", summary="too early")]),
              reply(calls=[call("run_command", command="python -m pytest -q")]),
              reply(calls=[call("finish", status="success", summary="verified")])],
        consult=[reply("Use one module.")],
        coder=[reply(calls=[call("write_file", path="app/hello.py", content="def hi():\n    return 'hi'\n"),
                            call("write_file", path="test_hello.py", content="from app.hello import hi\ndef test_hi():\n    assert hi()=='hi'\n")]),
               reply("Done.")])


def tool_results(stub, channel="orch"):
    return [m["content"] for m in stub.on(channel)[-1]["messages"] if m["role"] == "tool"]


@needs_git
def test_S1_walking_skeleton(cli, tmp_path):
    (tmp_path / "spec.md").write_text("# spec\nbuild hello\n")
    W = tmp_path / "W"
    with StubServer(**s1_scripts()) as s:
        r = cli("spec.md", "-w", str(W), "--reasoner", "r", "--coder", "c", "--no-stream", "--base-url", s.url)
        res = tool_results(s)
        coder_res = res[1]
    assert r.returncode == 0, r.stdout + r.stderr
    import re
    lines = re.sub(r"\x1b\[[0-9;]*m", "", r.stdout).rstrip().splitlines()
    assert lines[-2:] == ["[done] status=success", "verified"]
    for f in ("SPEC.md", "app/hello.py", "test_hello.py", ".agent/state.json"):
        assert (W / f).exists(), f
    assert res[2].startswith("REJECTED: No passing verification since your last file change.")
    assert "Files written/edited by coder: app/hello.py, test_hello.py" in coder_res
    log = subprocess.run(["git", "log", "--format=%s"], cwd=W, capture_output=True, text=True).stdout.split("\n")[:-1]
    assert log == ["coder: make hello", "initial"]                          # no 'final' commit: nothing changed
    assert (W / ".agent/transcript.log").read_text().count("[orch] ── step") == 5


@needs_git
def test_S2_interrupted_run_resumes(cli, tmp_path):
    (tmp_path / "spec.md").write_text("spec")
    W = tmp_path / "W"
    common = ["-w", str(W), "--reasoner", "r", "--coder", "c", "--no-stream"]
    with StubServer(**s1_scripts()) as s:
        r1 = cli("spec.md", *common, "--max-steps", "2", "--base-url", s.url)
        assert r1.returncode == 1
        assert "[done] status=partial" in r1.stdout and "Stopped after reaching max steps (2). Use --resume to continue." in r1.stdout
        r2 = cli("--resume", *common, "--max-steps", "10", "--base-url", s.url)
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert "[sys] resumed at step 2 with 6 messages" in r2.stdout and "status=success" in r2.stdout


def test_S3_unverified_finish_downgraded(cli, tmp_path):
    (tmp_path / "spec.md").write_text("spec")
    with StubServer(orch=[reply(calls=[call("finish", status="success", summary="done")])], repeat_last=["orch"]) as s:
        r = cli("spec.md", "-w", str(tmp_path / "W"), "--reasoner", "r", "--coder", "c", "--no-stream", "--no-git",
                "--max-steps", "20", "--base-url", s.url)
        res = tool_results(s)
        assert len(s.on("orch")) == 6
    assert r.returncode == 1
    assert len(res) == 5 and all(x.startswith("REJECTED:") for x in res)
    assert "status=partial" in r.stdout and "[downgraded from 'success': could not be verified]" in r.stdout


def test_S4_path_escape_refused(cli, tmp_path):
    (tmp_path / "spec.md").write_text("spec")
    with StubServer(orch=[reply(calls=[call("read_file", path="../../etc/passwd")]),
                          reply(calls=[call("finish", status="blocked", summary="x")])]) as s:
        r = cli("spec.md", "-w", str(tmp_path / "W"), "--reasoner", "r", "--coder", "c", "--no-stream", "--no-git", "--base-url", s.url)
        res = tool_results(s)
    assert res == ["ERROR: ValueError: path escapes workspace: ../../etc/passwd"]
    assert r.returncode == 1 and "status=blocked" in r.stdout


def test_S5_command_timeout(cli, tmp_path):
    (tmp_path / "spec.md").write_text("spec")
    with StubServer(orch=[reply(calls=[call("run_command", command="sleep 5", timeout_seconds=1)]),
                          reply(calls=[call("finish", status="partial", summary="x")])]) as s:
        t0 = time.time()
        r = cli("spec.md", "-w", str(tmp_path / "W"), "--reasoner", "r", "--coder", "c", "--no-stream", "--no-git", "--base-url", s.url)
        elapsed = time.time() - t0
        res = tool_results(s)
    assert res[0].startswith("exit code: 124") and res[0].endswith("[timed out after 1s and was killed]")
    assert elapsed < 4 and r.returncode == 1


def test_S6_missing_spec(cli, tmp_path):
    r = cli("-w", str(tmp_path / "W"))
    assert r.returncode == 2 and "provide a SPEC file (or --resume)" in r.stderr and "usage:" in r.stderr
    assert not (tmp_path / "W" / "SPEC.md").exists()


# ---- other cli requirements -------------------------------------------------------------------
def test_cli_R2_list_models_before_workspace(cli, tmp_path):
    with StubServer(models=("alpha", "beta")) as s:
        r = cli("--list-models", "-w", str(tmp_path / "W"), "--base-url", s.url)
    assert r.returncode == 0 and r.stdout.split() == ["alpha", "beta"] and not (tmp_path / "W").exists()


def test_cli_env_vars_and_stdin_spec_and_sibling_specs(cli, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "SPEC-api.md").write_text("api spec")
    (src / "main.md").write_text("main spec")
    script = [reply(calls=[call("finish", status="blocked", summary="b")])]
    with StubServer(orch=script) as s:
        r = cli(str(src / "main.md"), "-w", str(tmp_path / "W"), "--no-stream", "--no-git",
                env={"LLAMA_SWAP_URL": s.url, "REASONER_MODEL": "r", "CODER_MODEL": "c"})
        assert s.on("orch")[0]["model"] == "r"
    assert r.returncode == 1 and "reasoner=r coder=c" in r.stdout
    assert (tmp_path / "W/SPEC.md").read_text() == "main spec" and (tmp_path / "W/SPEC-api.md").read_text() == "api spec"
    with StubServer(orch=script) as s:
        r = cli("-", "-w", str(tmp_path / "W2"), "--reasoner", "r", "--no-stream", "--no-git", "--base-url", s.url, stdin="from stdin")
    assert (tmp_path / "W2/SPEC.md").read_text() == "from stdin" and not list((tmp_path / "W2").glob("SPEC-*"))


def test_cli_test_cmd_listed_in_first_message(cli, tmp_path):
    (tmp_path / "spec.md").write_text("S")
    with StubServer(orch=[reply(calls=[call("finish", status="blocked", summary="b")])]) as s:
        cli("spec.md", "-w", str(tmp_path / "W"), "--reasoner", "r", "--no-stream", "--no-git", "--base-url", s.url,
            "--test-cmd", "pytest -q")
        first = s.on("orch")[0]["messages"][1]["content"]
    assert "REQUIRED VERIFICATION" in first and "- pytest -q" in first


def test_cli_E1_resume_without_state_and_E2_unreachable(cli, tmp_path):
    r = cli("--resume", "-w", str(tmp_path / "W"), "--base-url", "http://127.0.0.1:9/v1", "--no-git")
    assert r.returncode != 0 and "FileNotFoundError" in r.stderr
    (tmp_path / "spec.md").write_text("S")
    r = cli("spec.md", "-w", str(tmp_path / "W"), "--base-url", "http://127.0.0.1:9/v1", "--no-git", "--no-stream", timeout=120)
    assert r.returncode != 0 and "APIConnectionError" in r.stderr and (tmp_path / "W/SPEC.md").exists()


def test_cli_R7_keyboard_interrupt_saves_state(tmp_path):
    (tmp_path / "spec.md").write_text("S")
    with StubServer(orch=[reply(calls=[call("list_files")], first_token_delay=10)]) as s:
        p = subprocess.Popen([sys.executable, "-m", "nai", "spec.md", "-w", str(tmp_path / "W"), "--reasoner", "r", "--no-stream", "--no-git",
                              "--base-url", s.url], cwd=tmp_path, env={**os.environ, "PYTHONPATH": str(ROOT)},
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        time.sleep(2.5)
        p.send_signal(signal.SIGINT)
        out, err = p.communicate(timeout=20)
    assert p.returncode == 130 and "interrupted; state saved. Re-run with --resume to continue." in out
    assert json.loads((tmp_path / "W/.agent/state.json").read_text())["step"] == 1


def test_cli_new_options_config_profile_and_call_log(tmp_path):
    (tmp_path / "spec.md").write_text("S")
    with StubServer(orch=[reply(calls=[call("finish", status="blocked", summary="b")])]) as s:
        (tmp_path / "nai.toml").write_text(f"""
[server]
base_url = "{s.url}"
[profiles.small.reasoner]
model_id = "r"
temperature = 0.1
top_p = 0.5
[profiles.small.coder]
model_id = "c"
""")
        r = subprocess.run([sys.executable, "-m", "nai", "spec.md", "-w", "W", "--profile", "small", "--no-stream", "--no-git",
                            "--call-log", "calls.jsonl"], cwd=tmp_path, capture_output=True, text=True,
                           env={**os.environ, "PYTHONPATH": str(ROOT)})
        assert s.on("orch")[0]["temperature"] == 0.1
    assert r.returncode == 1 and "reasoner=r coder=c" in r.stdout
    rec = json.loads((tmp_path / "calls.jsonl").read_text().splitlines()[0])
    assert rec["model"] == "r" and rec["meta"]["role"] == "reasoner" and rec["meta"]["prompts"] == ["orch_system@1"] and rec["request"]["messages"]


def test_cli_config_errors_exit_cleanly(tmp_path):
    r = subprocess.run([sys.executable, "-m", "nai", "x.md", "--config", "missing.toml"], cwd=tmp_path, capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": str(ROOT)})
    assert r.returncode == 1 and "config error: config file not found" in r.stderr


def test_cli_subcommand_hook(monkeypatch):
    from nai import cli
    seen = []
    cli.register_subcommand("hello", lambda argv: seen.append(argv) or 3)
    with pytest.raises(SystemExit) as e:
        cli.main(["hello", "a", "b"])
    assert e.value.code == 3 and seen == [["a", "b"]]
    monkeypatch.setitem(cli.SUBCOMMANDS, "hello", None)
    cli.SUBCOMMANDS.pop("hello")
