"""DockerTarget: hardening flags, network phases, scrubbed env, cancellation.

The ``fake_docker`` fixture runs payloads locally, so these tests need no
daemon.  ``test_real_docker_roundtrip`` is skipped when Docker or the image is
unavailable.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time

from nai.exec import CANCEL_EXIT, PHASE_SETUP
from nai.exec.docker import DockerTarget


def _calls(log):
    return [json.loads(line) for line in log.read_text().splitlines()]


def _env_values(call):
    return [call[i + 1] for i, token in enumerate(call) if token == "-e"]


# --- hardening flags ---------------------------------------------------------


def test_hardening_flags_on_the_run_container(tmp_path, fake_docker):
    target = DockerTarget(tmp_path, "python:3.12")
    try:
        run = _calls(fake_docker)[0]
        assert run[0] == "run" and "-d" in run
        assert "--network" in run and "none" in run
        assert "--memory" in run and "2g" in run
        assert "--cpus" in run and "2" in run
        assert "--pids-limit" in run and "512" in run
        assert "--cap-drop" in run and "ALL" in run
        assert "--security-opt" in run and "no-new-privileges" in run
        assert "--user" in run and f"{os.getuid()}:{os.getgid()}" in run
        assert f"{tmp_path}:/work" in run
        assert run[-2:] == ["sleep", "infinity"]
    finally:
        target.close()


def test_run_phase_uses_exec_in_the_network_off_container(tmp_path, fake_docker):
    target = DockerTarget(tmp_path, "python:3.12")
    try:
        result = target.run([sys.executable, "-c", "print('hi')"])
        assert result.exit_code == 0
        assert "hi" in result.output
        assert result.network == "off"
        calls = _calls(fake_docker)
        assert calls[-1][0] == "exec"
        assert "none" in calls[0]
    finally:
        target.close()


def test_setup_phase_runs_a_one_shot_container_with_network(tmp_path, fake_docker):
    target = DockerTarget(tmp_path, "python:3.12")
    try:
        result = target.run([sys.executable, "-c", "print('setup')"], phase=PHASE_SETUP)
        assert result.exit_code == 0
        assert result.network == "on"
        setup = _calls(fake_docker)[-1]
        assert setup[0] == "run"
        assert "-d" not in setup
        assert "--network" in setup and "bridge" in setup
        assert "none" not in setup
    finally:
        target.close()


# --- environment scrubbing ---------------------------------------------------


def test_environment_is_scrubbed_in_docker_flags(tmp_path, fake_docker, monkeypatch):
    monkeypatch.setenv("LLAMA_SWAP_KEY", "super-secret")
    target = DockerTarget(tmp_path, "python:3.12")
    try:
        result = target.run([sys.executable, "-c", "print('ok')"])
        assert result.exit_code == 0
        values = _env_values(_calls(fake_docker)[-1])
        assert not any("LLAMA_SWAP_KEY" in value for value in values)
        assert any(value.startswith("PATH=") for value in values)
    finally:
        target.close()


def test_secret_in_env_overrides_is_dropped(tmp_path, fake_docker):
    target = DockerTarget(tmp_path, "python:3.12")
    try:
        target.run(
            [sys.executable, "-c", "print('ok')"],
            env_overrides={"LLAMA_SWAP_KEY": "leak", "PYTHONPATH": "src"},
        )
        values = _env_values(_calls(fake_docker)[-1])
        assert not any("leak" in value for value in values)
        assert "PYTHONPATH=src" in values
    finally:
        target.close()


# --- cancellation and cleanup ------------------------------------------------


def test_cancel_kills_the_named_container(tmp_path, fake_docker):
    target = DockerTarget(tmp_path, "python:3.12")
    results: list = []

    def _run() -> None:
        results.append(
            target.run([sys.executable, "-c", "import time; time.sleep(10)"], timeout=30)
        )

    thread = threading.Thread(target=_run)
    thread.start()
    deadline = time.time() + 5
    while time.time() < deadline and target._proc is None:
        time.sleep(0.02)
    try:
        assert target.cancel() is True
        thread.join(timeout=10)
        assert not thread.is_alive()
        assert results and results[0].cancelled
        assert results[0].exit_code == CANCEL_EXIT
        assert any(call[0] == "kill" for call in _calls(fake_docker))
    finally:
        target.close()


def test_close_is_idempotent(tmp_path, fake_docker):
    target = DockerTarget(tmp_path, "python:3.12")
    name = target.container
    target.close()
    target.close()
    rm_calls = [call for call in _calls(fake_docker) if call[0] == "rm"]
    assert rm_calls == [["rm", "-f", name]]


def test_cancel_during_setup_kills_the_setup_container(tmp_path, fake_docker):
    """A setup run uses a one-shot container; cancel must kill *that* one."""
    target = DockerTarget(tmp_path, "python:3.12")
    results = []
    try:
        def _run():
            results.append(
                target.run(
                    [sys.executable, "-c", "import time; time.sleep(10)"],
                    timeout=30,
                    phase=PHASE_SETUP,
                )
            )

        thread = threading.Thread(target=_run)
        thread.start()
        deadline = time.time() + 5
        while time.time() < deadline and target._proc is None:
            time.sleep(0.02)
        setup_name = target._setup_container
        assert setup_name is not None
        assert target.cancel() is True
        thread.join(timeout=10)
        assert not thread.is_alive()
        assert results and results[0].cancelled
        assert results[0].exit_code == CANCEL_EXIT
        kills = [call for call in _calls(fake_docker) if call[0] == "kill"]
        assert kills, "expected a docker kill"
        # Every kill targets the setup container, never the long-lived run one.
        for call in kills:
            assert setup_name in call
            assert target.container not in call
    finally:
        target.close()


def test_cancelled_run_does_not_poison_the_next_run(tmp_path, fake_docker):
    target = DockerTarget(tmp_path, "python:3.12")
    results = []
    try:
        def _run():
            results.append(
                target.run(
                    [sys.executable, "-c", "import time; time.sleep(10)"], timeout=30
                )
            )

        thread = threading.Thread(target=_run)
        thread.start()
        deadline = time.time() + 5
        while time.time() < deadline and target._proc is None:
            time.sleep(0.02)
        assert target.cancel() is True
        thread.join(timeout=10)
        assert results and results[0].cancelled
        after = target.run([sys.executable, "-c", "print('ok')"])
        assert after.exit_code == 0
        assert not after.cancelled
        assert "ok" in after.output
    finally:
        target.close()


# --- capabilities and describe ----------------------------------------------


def test_capabilities_and_describe(tmp_path, fake_docker):
    target = DockerTarget(tmp_path, "python:3.12")
    try:
        caps = target.capabilities()
        assert caps.mode == "docker"
        assert caps.network_off and caps.filesystem_isolated
        assert caps.memory_limit and caps.cpu_limit and caps.pids_limit
        assert caps.capability_drop and caps.no_new_privileges and caps.user_namespace
        assert caps.real_cancel and caps.streaming
        assert "docker image python:3.12" in target.describe()
        assert "network=off" in target.describe()
    finally:
        target.close()


# --- cwd translation ---------------------------------------------------------


def test_cwd_is_translated_into_the_container(tmp_path, fake_docker):
    sub = tmp_path / "pkg"
    sub.mkdir()
    target = DockerTarget(tmp_path, "python:3.12")
    try:
        target.run([sys.executable, "-c", "print('ok')"], cwd=str(sub))
        exec_call = _calls(fake_docker)[-1]
        assert exec_call[1] == "-w"
        assert exec_call[2] == "/work/pkg"
    finally:
        target.close()


def test_cwd_outside_root_falls_back_to_work(tmp_path, fake_docker):
    target = DockerTarget(tmp_path, "python:3.12")
    try:
        target.run([sys.executable, "-c", "print('ok')"], cwd="/tmp")
        assert _calls(fake_docker)[-1][2] == "/work"
    finally:
        target.close()


# --- real Docker (skipped when unavailable) ---------------------------------


def test_real_docker_roundtrip(tmp_path, real_docker):
    target = DockerTarget(tmp_path, real_docker)
    try:
        result = target.run(["python", "-c", "print('hi')"])
        assert result.exit_code == 0, result.output
        assert "hi" in result.output
        assert result.network == "off"
    finally:
        target.close()
