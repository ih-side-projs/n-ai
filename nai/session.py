"""session-state: spec message, state file, plan file, context compaction (v1 session-state)."""
from __future__ import annotations

import json
from pathlib import Path

from . import promptstore
from .models import ModelClient
from .terminal import log
from .textutil import strip_think
from .workspace import Workspace


def spec_message(spec: str, test_cmds: list[str]) -> str:
    req = ""
    if test_cmds:
        req = ("\n\nREQUIRED VERIFICATION: before finish(success), each of these commands must have exited 0 "
               "after your last file change:\n" + "\n".join(f"- {c}" for c in test_cmds))
    return (f"# SPECIFICATION\n\n{spec}{req}\n\n---\nThe spec is also saved in the workspace as SPEC.md "
            "(plus any SPEC-*.md subsystem specs): list those paths in context_files so the coder and consultants can read them. "
            "Workspace root is the current directory for all tools. Begin.")


def initial_messages(spec: str, test_cmds: list[str]) -> list[dict]:
    return [{"role": "system", "content": promptstore.get("orch_system").text},
            {"role": "user", "content": spec_message(spec, test_cmds)}]


def save(path: Path, spec: str, messages: list[dict], convos: dict, step: int) -> None:
    path = Path(path)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"spec": spec, "messages": messages, "convos": convos, "step": step}), encoding="utf-8")
    tmp.replace(path)


def load(path: Path) -> tuple[str, list[dict], dict, int]:
    s = json.loads(Path(path).read_text(encoding="utf-8"))
    log("sys", f"resumed at step {s['step']} with {len(s['messages'])} messages", "cyan")
    return s["spec"], s["messages"], s["convos"], s["step"]


def write_plan(root: Path, content: str) -> str:
    (Path(root) / ".agent" / "plan.md").write_text(content, encoding="utf-8")
    return "plan saved"


def compact(client: ModelClient, messages: list[dict], spec: str, ws: Workspace, last_tokens: int) -> list[dict]:
    log("sys", f"context at {last_tokens} tokens, compacting...", "cyan")
    prompt = promptstore.get("compact")
    msg, _, _ = client.chat("reasoner", messages + [{"role": "user", "content": prompt.text}], tag="orch",
                            meta={"prompts": [prompt.id]})
    summary = strip_think(msg.content) or "(no summary produced)"
    plan_path = ws.root / ".agent" / "plan.md"
    plan = plan_path.read_text(encoding="utf-8") if plan_path.exists() else "(no plan written yet)"
    return [messages[0], {"role": "user", "content": (
        f"{spec_message(spec, ws.test_cmds)}\n\n---\nCONTEXT RESET. Your own summary of progress so far:\n{summary}\n\n"
        f"Current plan (.agent/plan.md):\n{plan}\n\nCurrent file tree:\n{ws.list_files('.', 3)}\n\n"
        "Continue the work. Re-read files as needed.")}]
