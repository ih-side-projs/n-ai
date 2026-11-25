"""The execution seam: environment scrubbing, output capture, helpers and selection."""

from __future__ import annotations

import pytest

from nai.exec import (
    CANCEL_EXIT,
    DEFAULT_ENV,
    PHASE_RUN,
    PHASE_SETUP,
    TIMEOUT_EXIT,
    BaseTarget,
    Capabilities,
    OutputCapture,
    RunResult,
    as_argv,
    is_secret,
    make_target,
    needs_shell,
    scrub_env,
    shell_argv,
)
from nai.exec.docker import DockerTarget
from nai.exec.local import LocalTarget

# --- environment scrubbing ---------------------------------------------------


def test_scrub_env_keeps_allowlist_and_drops_secrets(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HOME", "/home/tester")
    monkeypatch.setenv("LANG", "C.UTF-8")
    monkeypatch.setenv("LLAMA_SWAP_KEY", "super-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
    monkeypatch.setenv("NAI_WEB_TOKEN", "web-secret")

    env = scrub_env()

    assert env["PATH"] == "/usr/bin:/bin"
    assert env["HOME"] == "/home/tester"
    assert env["LANG"] == "C.UTF-8"
    for secret in ("LLAMA_SWAP_KEY", "OPENAI_API_KEY", "AWS_SECRET_ACCESS_KEY", "NAI_WEB_TOKEN"):
        assert secret not in env
    for key, value in DEFAULT_ENV.items():
        assert env[key] == value


def test_scrub_env_filters_overrides_and_keeps_adapter_needs(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    env = scrub_env(
        {
            "PYTHONPATH": "src",
            "CI": "0",
            "LLAMA_SWAP_KEY": "leak",
            "DATABASE_URL": "postgres://user:pw@host/db",
        }
    )
    assert env["PYTHONPATH"] == "src"
    assert env["CI"] == "0"            # explicit value wins over the default
    assert "LLAMA_SWAP_KEY" not in env
    assert "DATABASE_URL" not in env


def test_scrub_env_never_returns_an_empty_path(monkeypatch):
    monkeypatch.delenv("PATH", raising=False)
    assert scrub_env()["PATH"] == "/usr/local/bin:/usr/bin:/bin"


@pytest.mark.parametrize(
    "name",
    [
        "LLAMA_SWAP_KEY",
        "LLAMA_SWAP_URL",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "HF_TOKEN",
        "AWS_ACCESS_KEY_ID",
        "AZURE_CLIENT_SECRET",
        "DOCKER_AUTH_CONFIG",
        "KUBECONFIG",
        "PGPASSWORD",
        "DATABASE_URL",
        "PRIVATE_KEY",
        "SECRET_VALUE",
        "TOKEN",
        "PASSWORD",
        "CREDENTIAL",
        "NAI_WEB_TOKEN",
    ],
)
def test_is_secret_recognises_credentials(name):
    assert is_secret(name)


def test_is_secret_allows_ordinary_names():
    for name in ("PATH", "HOME", "LANG", "PYTHONPATH", "CI", "PYTHONWARNINGS"):
        assert not is_secret(name)


# --- argv helpers ------------------------------------------------------------


def test_as_argv_splits_strings_without_a_shell():
    assert as_argv("python -m pytest -q") == ["python", "-m", "pytest", "-q"]
    assert as_argv(["python", "-m", "pytest"]) == ["python", "-m", "pytest"]


def test_shell_argv_is_an_explicit_non_login_shell():
    argv = shell_argv("echo $CI | cat")
    assert argv[:4] == ["bash", "-o", "pipefail", "-c"]
    assert argv[4] == "echo $CI | cat"
    assert "-l" not in argv and "-lc" not in argv


def test_needs_shell_detects_shell_syntax():
    assert needs_shell("echo $CI | cat")
    assert needs_shell("a && b")
    assert needs_shell("echo hi > out.txt")
    assert not needs_shell("python -m pytest -q")
    assert not needs_shell("python -c 'print(1)'")


# --- output capture ----------------------------------------------------------


def test_output_capture_keeps_head_and_tail_under_the_cap():
    cap = 40
    capture = OutputCapture(cap=cap)
    capture.feed("a" * 30)
    assert not capture.truncated
    assert capture.text() == "a" * 30

    capture = OutputCapture(cap=cap)
    capture.feed("a" * 100)
    text = capture.text()
    assert capture.truncated
    assert text.startswith("a" * 20)
    assert text.endswith("a" * 20)
    assert "chars truncated" in text
    # The retained text stays near the cap plus the marker.
    assert len(text) < cap + 80


def test_output_capture_streams_every_chunk_to_the_sink():
    seen: list[str] = []
    capture = OutputCapture(cap=64, sink=seen.append)
    capture.feed("one")
    capture.feed("two")
    assert seen == ["one", "two"]
    assert "one" in capture.text() and "two" in capture.text()


def test_output_capture_ignores_empty_chunks():
    capture = OutputCapture(cap=16)
    capture.feed("")
    assert capture.text() == ""
    assert not capture.truncated


# --- RunResult and Capabilities ---------------------------------------------


def test_run_result_ok_and_rendering():
    result = RunResult(argv=("echo", "hi"), exit_code=0, output="hi\n")
    assert result.ok
    assert str(result) == "exit code: 0\nhi\n"
    assert not RunResult(argv=(), exit_code=1, output="").ok
    assert not RunResult(argv=(), exit_code=0, output="", timed_out=True).ok
    assert not RunResult(argv=(), exit_code=0, output="", cancelled=True).ok
    assert RunResult(argv=(), exit_code=TIMEOUT_EXIT, output="", timed_out=True).timed_out
    assert RunResult(argv=(), exit_code=CANCEL_EXIT, output="", cancelled=True).cancelled


def test_run_result_to_dict_round_trips():
    result = RunResult(
        argv=("ls",),
        exit_code=2,
        output="boom",
        duration=1.5,
        truncated=True,
        mode="docker",
        network="off",
        weaker_isolation=True,
        error="oops",
        notes=("note",),
    )
    data = result.to_dict()
    assert data["argv"] == ["ls"]
    assert data["exit_code"] == 2
    assert data["truncated"] is True
    assert data["mode"] == "docker"
    assert data["network"] == "off"
    assert data["weaker_isolation"] is True
    assert data["notes"] == ["note"]


def test_capabilities_to_dict_reports_every_flag():
    caps = Capabilities(mode="docker", network_off=True, real_cancel=True)
    data = caps.to_dict()
    assert data["mode"] == "docker"
    assert data["network_off"] is True
    assert data["real_cancel"] is True
    assert data["streaming"] is True
    assert set(data) == {
        "mode", "network_off", "filesystem_isolated", "memory_limit",
        "cpu_limit", "pids_limit", "capability_drop", "no_new_privileges",
        "user_namespace", "real_cancel", "streaming",
    }


# --- BaseTarget plumbing -----------------------------------------------------


def test_base_target_resolves_network_by_phase(tmp_path):
    target = BaseTarget(root=tmp_path)
    assert target._network_for(PHASE_SETUP, None) is True
    assert target._network_for(PHASE_RUN, None) is False
    assert target._network_for(PHASE_RUN, True) is True
    assert target._network_for(PHASE_SETUP, False) is False


def test_base_target_clamps_timeouts(tmp_path):
    target = BaseTarget(root=tmp_path)
    assert target._clamp_timeout(None) == 600.0
    assert target._clamp_timeout(0.5) == 1.0
    assert target._clamp_timeout(99_999) == 3600.0


# --- make_target selection ---------------------------------------------------


def test_make_target_defaults_to_local(tmp_path):
    target = make_target(tmp_path)
    assert isinstance(target, LocalTarget)
    assert target.capabilities().mode == "local"
    target.close()


def test_make_target_selects_docker(tmp_path, monkeypatch):
    started: list[str] = []

    def _start(self):
        started.append("started")
        self.container = "fake-container"
        return self.container

    monkeypatch.setattr(DockerTarget, "_start_container", _start)
    monkeypatch.setattr(DockerTarget, "close", lambda self: None)

    target = make_target(tmp_path, docker_image="python:3.12")
    assert isinstance(target, DockerTarget)
    assert target.capabilities().mode == "docker"
    assert started == ["started"]
    assert "docker image python:3.12" in target.describe()
    target.close()


def test_capabilities_for_helper(tmp_path):
    from nai.exec import capabilities_for

    target = make_target(tmp_path)
    try:
        assert capabilities_for(target)["mode"] == "local"
    finally:
        target.close()


def test_execution_target_protocol_is_runtime_checkable(tmp_path):
    from nai.exec import ExecutionTarget

    target = make_target(tmp_path)
    try:
        assert isinstance(target, ExecutionTarget)
    finally:
        target.close()
