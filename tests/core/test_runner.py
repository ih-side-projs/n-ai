"""v1 command-runner R1-R6, E1-E4, example; CORE-6 Docker mode (fake docker), extra env, cancel."""
import subprocess
import threading
import time

import pytest

from nai.gate import VerificationGate
from nai.runner import CommandRunner


def mk(tmp_path, **kw):
    return CommandRunner(tmp_path, 10, kw.pop("docker_image", None), VerificationGate(tmp_path, kw.pop("test_cmds", [])), **kw)


def test_R1_example_echo(tmp_path):
    assert mk(tmp_path).run_command("echo hi") == "exit code: 0\nhi\n"


def test_R1_cwd_stderr_merged_stdin_closed_pipefail(tmp_path):
    r = mk(tmp_path)
    assert r.run_command("pwd; echo err >&2").splitlines()[1:] == [str(tmp_path.resolve()), "err"]
    assert r.run_command("cat; echo done").endswith("done\n")          # stdin is /dev/null, cat returns at once
    assert r.run_command("false | true").startswith("exit code: 1")    # pipefail


def test_R2_environment(tmp_path):
    out = mk(tmp_path).run_command("echo $CI $PYTHONUNBUFFERED $DEBIAN_FRONTEND $GIT_TERMINAL_PROMPT $PIP_DISABLE_PIP_VERSION_CHECK $PYTHONDONTWRITEBYTECODE")
    assert out.splitlines()[1] == "1 1 noninteractive 0 1 1"


def test_E1_E4_nonzero_and_empty_output(tmp_path):
    assert mk(tmp_path).run_command("exit 3") == "exit code: 3\n"


def test_R3_example_timeout_kills_group(tmp_path):
    t0 = time.time()
    out = mk(tmp_path).run_command("sleep 5", 1)
    assert out.startswith("exit code: 124") and out.endswith("[timed out after 1s and was killed]")
    assert time.time() - t0 < 4


def test_E2_timeout_selection(tmp_path):
    r = CommandRunner(tmp_path, 1, None, VerificationGate(tmp_path, []))
    assert r.run_command("sleep 3", None).startswith("exit code: 124")     # default 1s
    assert r.run_command("sleep 3", 0).startswith("exit code: 124")
    r2 = mk(tmp_path)
    assert "timed out after 3600s" not in r2.run_command("echo x", 99999)  # accepted (capped, not an error)


def test_R4_truncates_and_notes_verification(tmp_path):
    r = mk(tmp_path)
    out = r.run_command("python3 -c \"print('x'*20000)\"")
    assert "chars truncated" in out
    ok = r.run_command("echo '1 passed' # pytest")
    assert ok.endswith("[verification: counted as a passing test run]") and r.gate.passed == {"auto"}


def test_R4_scope_is_passed_to_gate(tmp_path):
    r = mk(tmp_path)
    r.gate.set_required("n1", "echo node-test")
    assert "[verification" in r.run_command("echo node-test", scope="n1")
    assert r.gate.problem("n1") is None and r.gate.passed == set()


def test_extra_env(tmp_path):
    assert mk(tmp_path, extra_env={"MY_VAR": "abc"}).run_command("echo $MY_VAR").splitlines()[1] == "abc"


def test_cancel_kills_running_group(tmp_path):
    r = mk(tmp_path)
    res = {}
    th = threading.Thread(target=lambda: res.setdefault("o", r.run_command("sleep 20", 30)))
    th.start()
    time.sleep(0.5)
    t0 = time.time()
    assert r.cancel() is True
    th.join(5)
    assert not th.is_alive() and time.time() - t0 < 3
    assert res["o"].startswith("exit code: 130") and res["o"].endswith("[cancelled]")
    assert r.cancel() is False                                              # nothing running


# --- Docker mode through a fake `docker` executable ------------------------------------
def test_R5_R6_docker_commands_and_cleanup(tmp_path, fake_docker):
    import json
    r = mk(tmp_path, docker_image="python:3.12", extra_env={"X": "1"})
    calls = [json.loads(line) for line in fake_docker.read_text().splitlines()]
    run = calls[0]
    assert run[:6] == ["run", "-d", "--rm", "--init", "--name", r.container]
    assert run[6:10] == ["-v", f"{tmp_path}:/work", "-w", "/work"] and run[10:] == ["python:3.12", "sleep", "infinity"]
    assert r.run_command("echo $CI-$X") == "exit code: 0\n1-1\n"
    ex = [json.loads(line) for line in fake_docker.read_text().splitlines()][-1]
    assert ex[:3] == ["exec", "-w", "/work"] and "CI=1" in ex and "X=1" in ex
    assert ex[-5:] == ["bash", "-o", "pipefail", "-lc", "echo $CI-$X"]
    assert ex[ex.index("timeout"):ex.index("timeout") + 4] == ["timeout", "-k", "5", "10"]
    name = r.container
    r.close()
    r.close()                                                              # idempotent
    last = json.loads(fake_docker.read_text().splitlines()[-1])
    assert last == ["rm", "-f", name]


def test_E3_docker_run_failure_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    with pytest.raises((subprocess.CalledProcessError, FileNotFoundError)):
        mk(tmp_path, docker_image="img")


def test_docker_names_unique_per_runner(tmp_path, fake_docker):
    a, b = mk(tmp_path, docker_image="x"), mk(tmp_path, docker_image="x")
    assert a.container != b.container
    a.close(); b.close()
