"""consult-threads: named, persistent conversations with the reasoning model (v1 consult-threads)."""
from __future__ import annotations

from . import promptstore
from .models import ModelClient
from .terminal import log
from .textutil import strip_think
from .workspace import Workspace


class ConsultThreads:
    def __init__(self, client: ModelClient, ws: Workspace, convos: dict | None = None):
        self.client, self.ws = client, ws
        self.convos: dict[str, list[dict]] = convos if convos is not None else {}
        self.system = promptstore.get("consult_system")

    def files_block(self, files: list[str] | None) -> str:
        return self.ws.files_block(files, missing="(file does not exist)")

    def consult(self, conversation_id: str, message: str, context_files: list[str] | None = None, reset: bool = False) -> str:
        conv = self.convos.get(conversation_id)
        if conv is None or reset:
            conv = self.convos[conversation_id] = [{"role": "system", "content": self.system.text}]
        conv.append({"role": "user", "content": message + self.files_block(context_files)})
        msg, usage, dt = self.client.chat("reasoner", conv, tag=f"consult:{conversation_id}", meta={"prompts": [self.system.id]})
        text = strip_think(msg.content) or "(empty response)"
        conv.append({"role": "assistant", "content": text})
        if usage and usage.prompt_tokens > self.client.ctx("reasoner") * 0.75 and len(conv) > 6:
            del conv[1:5]  # drop the two oldest exchanges
        log("consult", f"{conversation_id} replied in {dt:.0f}s ({len(text)} chars)", "cyan")
        return text
