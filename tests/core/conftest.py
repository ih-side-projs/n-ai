import os
import shutil
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
# keep any sandbox/corporate proxy away from the loopback stub server
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(k, None)
for k in ("LLAMA_SWAP_URL", "LLAMA_SWAP_KEY", "REASONER_MODEL", "CODER_MODEL", "NAI_CONFIG"):
    os.environ.pop(k, None)

from nai.llm import LLM  # noqa: E402
from nai.models import ModelClient, ModelProfile, RoleConfig  # noqa: E402
from nai.testing.stub_server import StubServer  # noqa: E402

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


@pytest.fixture(autouse=True)
def _reset_terminal():
    from nai import terminal
    terminal.set_transcript(None)
    yield
    terminal.set_transcript(None)


@pytest.fixture
def stub():
    with StubServer() as s:
        yield s


def make_client(stub, ctx=32768, stream=False, **llm_kw):
    llm = LLM(stub.url, "none", stream=stream, show_thinking=False, **llm_kw)
    prof = ModelProfile("t", RoleConfig("r", ctx=ctx, max_tokens=1000, temperature=0.6, top_p=0.95, top_k=20),
                        RoleConfig("c", ctx=ctx, max_tokens=1000, temperature=0.7, top_p=0.8, top_k=20,
                                   extras={"repeat_penalty": 1.05}))
    return ModelClient(llm, prof)


@pytest.fixture
def fake_docker(tmp_path, monkeypatch):
    """A `docker` executable on PATH that logs calls and runs `exec` commands locally in the mounted dir."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    log = tmp_path / "docker.log"
    script = bindir / "docker"
    script.write_text(f"""#!/usr/bin/env python3
import os, subprocess, sys, json
log = {str(log)!r}
args = sys.argv[1:]
open(log, "a").write(json.dumps(args) + "\\n")
VALUE_FLAGS = {{"--name", "--network", "--memory", "--cpus", "--pids-limit",
               "--cap-drop", "--security-opt", "--user", "-v", "-w", "-e",
               "--env", "--workdir", "--volume"}}

def mount_root():
    try:
        return args[args.index("-v") + 1].split(":")[0]
    except ValueError:
        return "."

def split_env():
    env = dict(os.environ)
    i = 1
    while i < len(args) and args[i] in ("-w", "-e"):
        if args[i] == "-e":
            k, v = args[i + 1].split("=", 1); env[k] = v
        i += 2
    return env, i

def payload(start):
    i = start
    while i < len(args):
        if args[i] in VALUE_FLAGS:
            i += 2
        elif args[i].startswith("-"):
            i += 1
        else:
            return i
    return len(args)

if args[0] == "run":
    root = mount_root()
    open(log + ".root", "w").write(root)
    if "-d" in args:
        print("fakeid"); sys.exit(0)
    i = payload(1)                       # one-shot setup container: image then argv
    rest = args[i + 1:]
    if not rest:
        sys.exit(0)
    env, _ = split_env()
    sys.exit(subprocess.run(rest, cwd=root, env=env).returncode)
if args[0] == "exec":
    root = open(log + ".root").read()
    env, i = split_env()
    name = args[i]; rest = args[i + 1:]
    sys.exit(subprocess.run(rest, cwd=root, env=env).returncode)
sys.exit(0)
""")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return log
