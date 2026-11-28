"""Fixtures for the execution-target tests.

``fake_docker`` puts a ``docker`` executable on ``PATH`` that logs every call
and runs ``run``/``exec`` payloads locally (via ``exec``), so the Docker target
can be exercised without a Docker daemon.  ``real_docker`` skips when Docker or
the requested image is unavailable.
"""

from __future__ import annotations

import os
import shutil
import stat

import pytest

_FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, sys

log = os.environ["NAI_FAKE_DOCKER_LOG"]
args = sys.argv[1:]
with open(log, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(args) + "\n")

VALUE_FLAGS = {
    "--name", "--network", "--memory", "--cpus", "--pids-limit",
    "--cap-drop", "--security-opt", "--user", "-v", "-w", "-e",
    "--env", "--workdir", "--volume", "--mount", "--entrypoint",
}

if not args:
    sys.exit(0)

def mount_root():
    try:
        return args[args.index("-v") + 1].split(":")[0]
    except ValueError:
        return "."

def split_env():
    """Return (env from -e flags, index of the first non-flag token)."""
    env = {}
    i = 1
    while i < len(args):
        token = args[i]
        if token in ("-e", "--env"):
            key, _, value = args[i + 1].partition("=")
            env[key] = value
            i += 2
        elif token in VALUE_FLAGS:
            i += 2
        elif token.startswith("-"):
            i += 1
        else:
            break
    return env, i

def payload(start):
    """Skip flags; return the index of the image (for run) or container name."""
    i = start
    while i < len(args):
        token = args[i]
        if token in VALUE_FLAGS:
            i += 2
        elif token.startswith("-"):
            i += 1
        else:
            return i
    return len(args)

command = args[0]
if command == "run":
    if "-d" in args:
        print("fakeid")
        sys.exit(0)
    i = payload(1)
    rest = args[i + 1:]
    if not rest:
        sys.exit(0)
    env, _ = split_env()
    child = dict(env)
    child.setdefault("PATH", os.environ.get("PATH", "/usr/bin:/bin"))
    os.chdir(mount_root())
    os.execvpe(rest[0], rest, child)
if command == "exec":
    env, i = split_env()
    rest = args[i + 1:]
    child = dict(env)
    child.setdefault("PATH", os.environ.get("PATH", "/usr/bin:/bin"))
    os.execvpe(rest[0], rest, child)
if command in ("kill", "rm", "stop"):
    sys.exit(0)
sys.exit(0)
'''


@pytest.fixture
def fake_docker(tmp_path, monkeypatch):
    """A ``docker`` executable that logs calls and runs payloads locally."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    log = tmp_path / "docker.log"
    script = bindir / "docker"
    script.write_text(_FAKE_DOCKER, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("NAI_FAKE_DOCKER_LOG", str(log))
    return log


def _docker_image_available(image: str) -> bool:
    import subprocess

    if shutil.which("docker") is None:
        return False
    try:
        probe = subprocess.run(
            ["docker", "image", "inspect", image],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0


@pytest.fixture
def real_docker():
    """Skip unless Docker and the test image are available locally."""
    image = os.environ.get("NAI_TEST_DOCKER_IMAGE", "python:3.12-slim")
    if shutil.which("docker") is None:
        pytest.skip("docker is not installed")
    if not _docker_image_available(image):
        pytest.skip(f"docker image {image} is not present locally")
    return image
