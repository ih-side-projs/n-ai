"""coder-agent: tool-using loop for the coder model (v1 coder-agent, on ModelClient)."""
from __future__ import annotations

from typing import Callable

from . import promptstore
from .models import ModelClient
from .terminal import log
from .textutil import msg_to_dict, parse_args, preview, truncate
from .toolspec import CODER_TOOLS_BASE, RUN_COMMAND
from .workspace import Workspace


class Coder:
    def __init__(self, client: ModelClient, ws: Workspace, max_steps: int, allow_bash: bool = False,
                 extra_tools: list[dict] | None = None, tool_runner: Callable[[str, dict], str] | None = None):
        """`extra_tools` (OpenAI schemas) are offered too; calls to them go to `tool_runner(name, args)` (TLS-3 allowlists)."""
        self.client, self.ws, self.max_steps = client, ws, max_steps
        self.tools = CODER_TOOLS_BASE + ([RUN_COMMAND] if allow_bash else []) + list(extra_tools or [])
        self.allowed = {t["function"]["name"] for t in self.tools}
        self.extra_names = {t["function"]["name"] for t in (extra_tools or [])}
        self.tool_runner = tool_runner
        self.system = promptstore.get("coder_system")

    def run(self, task: str, context_files: list[str]) -> str:
        ctx_txt = self.ws.files_block(context_files, missing="(file does not exist yet)")
        user = f"## Task\n{task}\n"
        if ctx_txt:
            user += f"\n## Context files (current contents){ctx_txt}"
        user += f"\n## Workspace tree\n{self.ws.list_files('.', 3)}"

        msgs = [{"role": "system", "content": self.system.text}, {"role": "user", "content": user}]
        ctx = self.client.ctx("coder")
        self.ws.touched.clear()
        nudged = False
        final = "[coder hit its step limit]"
        for step in range(1, self.max_steps + 1):
            msg, usage, dt = self.client.chat("coder", msgs, tools=self.tools, tag="coder", meta={"prompts": [self.system.id]})
            d = msg_to_dict(msg)
            msgs.append(d)
            calls = d.get("tool_calls")
            if not calls:
                if not self.ws.touched and not nudged:
                    nudged = True
                    msgs.append({"role": "user", "content": "You have not written any files. Use write_file / edit_file to make the changes, then reply with a short summary."})
                    continue
                final = d["content"] or "(no summary)"
                break
            for tc in calls:
                name, raw = tc["function"]["name"], tc["function"]["arguments"]
                args = parse_args(raw)
                if name not in self.allowed:
                    res = f"ERROR: unknown tool '{name}'. Available: {', '.join(sorted(self.allowed))}"
                elif args is None:
                    res = "ERROR: tool arguments were not a valid JSON object; retry"
                else:
                    log("coder", f"step {step} ({dt:.0f}s) → {name} {preview(name, args)}")
                    if name in self.extra_names and self.tool_runner:
                        try:
                            res = str(self.tool_runner(name, args))
                        except Exception as e:
                            res = f"ERROR: {type(e).__name__}: {e}"
                    else:
                        res = self.ws.call(name, args)
                msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": truncate(res)})
            if usage and usage.prompt_tokens > ctx * 0.9:
                final = "[coder stopped: context nearly full; task may be incomplete, split it into smaller tasks]"
                break
        files = ", ".join(sorted(self.ws.touched)) or "none"
        return f"{final}\n\nFiles written/edited by coder: {files}"
