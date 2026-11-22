"""cli: the v1 command line (arguments, wiring, exit codes), plus a hook for later subcommands."""
from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path

from . import terminal
from .coder import Coder
from .config import ConfigError, load_config
from .llm import LLM, JsonlCallLog, MissingDependency
from .models import ModelClient
from .orchestrator import Orchestrator
from .workspace import Workspace

SUBCOMMANDS: dict[str, Callable[[list[str]], int | None]] = {}


def register_subcommand(name: str, fn: Callable[[list[str]], int | None]) -> None:
    """Later subsystems (`run`, `mcp`, `bench`, ...) add commands here; `nai <name> ...` calls fn(argv_rest)."""
    SUBCOMMANDS[name] = fn


def build_parser() -> argparse.ArgumentParser:
    # Defaults of None mean "not given": the effective default then comes from env, config file or the built-in profile
    # (qwen3.6-reasoning / qwen3-coder, 32768 context, 16384 max tokens, http://localhost:8080/v1, key 'none').
    ap = argparse.ArgumentParser(prog="nai", description="Build software from a spec using llama-swap models.")
    ap.add_argument("spec", nargs="?", help="Spec file path, or '-' for stdin")
    ap.add_argument("-w", "--workspace", default="./workspace")
    ap.add_argument("--base-url", default=None, help="Server URL (env LLAMA_SWAP_URL; default http://localhost:8080/v1)")
    ap.add_argument("--api-key", default=None, help="API key (env LLAMA_SWAP_KEY; default 'none')")
    ap.add_argument("--reasoner", default=None, help="llama-swap model id of the reasoning model (env REASONER_MODEL; default qwen3.6-reasoning)")
    ap.add_argument("--coder", default=None, help="llama-swap model id of the coder model (env CODER_MODEL; default qwen3-coder)")
    ap.add_argument("--ctx", type=int, default=None, help="Context size configured for the reasoner (default 32768)")
    ap.add_argument("--coder-ctx", type=int, default=None, help="Context size configured for the coder (default 32768)")
    ap.add_argument("--max-tokens", type=int, default=None, help="Max output tokens per call, thinking included (default 16384)")
    ap.add_argument("--max-steps", type=int, default=200, help="Max orchestrator steps")
    ap.add_argument("--coder-max-steps", type=int, default=40)
    ap.add_argument("--cmd-timeout", type=int, default=600, help="Default seconds per shell command")
    ap.add_argument("--docker", metavar="IMAGE", help="Run commands inside a persistent container (e.g. python:3.12 or node:22)")
    ap.add_argument("--coder-bash", action="store_true", help="Also give the coder model the run_command tool")
    ap.add_argument("--no-git", action="store_true", help="Disable automatic git checkpoints")
    ap.add_argument("--test-cmd", action="append", metavar="CMD",
                    help="Command that must exit 0 (after the last change) before 'success' is accepted. Repeatable; "
                         "matched as a substring of commands the agent runs. Default: auto-detect common test runners")
    ap.add_argument("--no-gate", action="store_true", help="Disable the verification gate on finish")
    ap.add_argument("--no-stream", action="store_true", help="Disable live token streaming")
    ap.add_argument("--hide-thinking", action="store_true", help="Stream output but show only a progress counter for thinking")
    ap.add_argument("--resume", action="store_true", help="Resume from <workspace>/.agent/state.json")
    ap.add_argument("--list-models", action="store_true", help="Print model ids reported by the server and exit")
    # new in nai (X7, X6)
    ap.add_argument("--config", metavar="FILE", help="TOML config file (default: $NAI_CONFIG, then ./nai.toml if present)")
    ap.add_argument("--profile", metavar="NAME", help="Model profile from the config file")
    ap.add_argument("--call-log", metavar="FILE", help="Append every model call (full prompt and reply) to this JSONL file")
    ap.add_argument("--doctor", action="store_true", help="Run integration self-checks and exit")
    return ap


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in SUBCOMMANDS:
        sys.exit(SUBCOMMANDS[argv[0]](argv[1:]) or 0)
    ap = build_parser()
    a = ap.parse_args(argv)
    if a.doctor:
        from .doctor import main as doctor_main
        sys.exit(doctor_main([]))
    try:
        cfg = load_config(a.config)
        profile = cfg.get_profile(a.profile, reasoner=a.reasoner, coder=a.coder, ctx=a.ctx, coder_ctx=a.coder_ctx,
                                  max_tokens=a.max_tokens)
        srv = cfg.server
        llm = LLM(a.base_url or srv.base_url, a.api_key or srv.api_key, timeout=srv.timeout,
                  stream=srv.stream and not a.no_stream, show_thinking=not a.hide_thinking,
                  swap_threshold=srv.swap_threshold_seconds)
    except ConfigError as e:
        sys.exit(f"config error: {e}")
    except MissingDependency as e:
        sys.exit(str(e))
    if a.call_log:
        llm.observers.append(JsonlCallLog(Path(a.call_log)))

    if a.list_models:
        for m in llm.client.models.list().data:
            print(m.id)
        return
    if not a.spec and not a.resume:
        ap.error("provide a SPEC file (or --resume)")

    client = ModelClient(llm, profile)
    ws = Workspace(Path(a.workspace), a.cmd_timeout, a.docker, git=not a.no_git, test_cmds=a.test_cmd, gate=not a.no_gate)
    terminal.set_transcript(ws.root / ".agent" / "transcript.log")
    coder = Coder(client, ws, a.coder_max_steps, a.coder_bash)
    orch = Orchestrator(client, ws, coder, max_steps=a.max_steps)

    if a.resume:
        orch.load()
    else:
        spec = sys.stdin.read() if a.spec == "-" else Path(a.spec).read_text()
        (ws.root / "SPEC.md").write_text(spec)  # so agents can pass spec sections via context_files
        if a.spec != "-":  # subsystem specs written next to the main spec (SPEC-<n>.md)
            for sib in Path(a.spec).resolve().parent.glob("SPEC-*.md"):
                (ws.root / sib.name).write_text(sib.read_text())
        orch.start(spec)

    terminal.log("sys", f"workspace={ws.root} reasoner={profile.reasoner.model_id} coder={profile.coder.model_id}", "cyan")
    try:
        status, summary = orch.run()
    except KeyboardInterrupt:
        orch.save()
        terminal.log("sys", "interrupted; state saved. Re-run with --resume to continue.", "red")
        sys.exit(130)
    color = {"success": "green", "partial": "yellow", "blocked": "red"}.get(status, "dim")
    terminal.log("done", f"status={status}\n{summary}", color)
    sys.exit(0 if status == "success" else 1)
