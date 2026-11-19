"""orchestrator-loop: the lead model's tool-calling loop (v1 orchestrator-loop, on ModelClient)."""
from __future__ import annotations

from pathlib import Path

from . import promptstore, session
from .coder import Coder
from .consult import ConsultThreads
from .models import ModelClient
from .terminal import log
from .textutil import msg_to_dict, parse_args, preview, truncate
from .toolspec import ORCH_TOOLS
from .workspace import Workspace

FinalResult = tuple[str, str]   # (status, summary); status is "success", "partial" or "blocked"


class Orchestrator:
    def __init__(self, client: ModelClient, ws: Workspace, coder: Coder, max_steps: int = 200,
                 state_path: Path | None = None):
        self.client, self.ws, self.coder, self.max_steps = client, ws, coder, max_steps
        self.state_path = state_path or ws.root / ".agent" / "state.json"
        self.spec = ""
        self.messages: list[dict] = []
        self.convos: dict[str, list[dict]] = {}
        self.threads = ConsultThreads(client, ws, self.convos)   # shares self.convos
        self.step = 0
        self.last_prompt_tokens = 0
        self.done: FinalResult | None = None
        self.finish_rejects = 0
        self.system = promptstore.get("orch_system")

    # -- state ---------------------------------------------------------------
    def start(self, spec: str) -> None:
        self.spec = spec
        self.messages = session.initial_messages(spec, self.ws.test_cmds)
        self.ws.checkpoint("initial")

    def save(self) -> None:
        session.save(self.state_path, self.spec, self.messages, self.convos, self.step)

    def load(self) -> None:
        self.spec, self.messages, convos, self.step = session.load(self.state_path)
        self.convos.clear()
        self.convos.update(convos)

    # -- tools ---------------------------------------------------------------
    def delegate(self, task, context_files=None):
        out = self.coder.run(task, context_files or [])
        self.ws.checkpoint(f"coder: {task.strip().splitlines()[0][:80] if task.strip() else 'task'}")
        return out

    def finish(self, status="success", summary=""):
        if status == "success":
            problem = self.ws.verification_problem()
            if problem:
                self.finish_rejects += 1
                if self.finish_rejects > 5:
                    self.done = ("partial", f"[downgraded from 'success': could not be verified] {summary}")
                    return "ok (recorded as partial)"
                return ("REJECTED: " + problem + " If the project genuinely cannot be tested, "
                        "call finish with status 'partial' and explain why.")
        self.done = (status, summary)
        return "ok"

    def handle(self, tc: dict) -> str:
        name, raw = tc["function"]["name"], tc["function"]["arguments"]
        args = parse_args(raw)
        if args is None:
            return "ERROR: tool arguments were not a valid JSON object; retry"
        log("orch", f"→ {name} {preview(name, args)}", "yellow")
        try:
            if name == "delegate_to_coder":
                out = self.delegate(**args)
            elif name == "consult_reasoner":
                out = self.threads.consult(**args)
            elif name == "update_plan":
                out = session.write_plan(self.ws.root, **args)
            elif name == "finish":
                out = self.finish(**args)
            else:
                out = self.ws.call(name, args)
        except TypeError as e:
            out = f"ERROR: bad arguments for {name}: {e}"
        except Exception as e:
            out = f"ERROR: {type(e).__name__}: {e}"
        out = truncate(str(out))
        log("orch", "  ← " + out.replace("\n", " ⏎ ")[:300])
        return out

    # -- main loop -----------------------------------------------------------
    def run(self) -> FinalResult:
        nudges = 0
        ctx = self.client.ctx("reasoner")
        while self.step < self.max_steps and not self.done:
            self.step += 1
            if self.last_prompt_tokens > ctx * 0.7:
                self.messages = session.compact(self.client, self.messages, self.spec, self.ws, self.last_prompt_tokens)
                self.last_prompt_tokens = 0
            log("orch", f"── step {self.step} ──")
            msg, usage, dt = self.client.chat("reasoner", self.messages, tools=ORCH_TOOLS, tag="orch",
                                              meta={"prompts": [self.system.id], "step": self.step})
            self.last_prompt_tokens = usage.prompt_tokens if usage else 0
            d = msg_to_dict(msg)
            self.messages.append(d)
            if d["content"] and not self.client.llm.stream:
                log("orch", f"({dt:.0f}s) {d['content'][:500]}", "green")
            calls = d.get("tool_calls")
            if not calls:
                nudges += 1
                if nudges > 3:
                    self.done = ("blocked", "Orchestrator stopped issuing tool calls.")
                    break
                self.messages.append({"role": "user", "content": "Continue: call a tool, or call `finish` if the spec is fully implemented and verified."})
                continue
            nudges = 0
            for tc in calls:
                result = self.handle(tc)
                self.messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
            self.save()
        self.save()
        self.ws.checkpoint("final")
        if not self.done:
            self.done = ("partial", f"Stopped after reaching max steps ({self.max_steps}). Use --resume to continue.")
        return self.done
