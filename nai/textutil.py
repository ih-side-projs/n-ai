"""text-utils: pure helpers for model output, tool arguments and message conversion (v1 text-utils)."""
from __future__ import annotations

import json
import re
import uuid


def strip_think(s: str | None) -> str:
    """R1: remove <think> blocks, keep text after a stray </think>, drop an unfinished <think>."""
    if not s:
        return ""
    s = re.sub(r"<think>.*?</think>", "", s, flags=re.S)
    if "</think>" in s:  # the chat template opened <think> for the model
        s = s.rsplit("</think>", 1)[1]
    if "<think>" in s:  # truncated thinking
        s = s.split("<think>", 1)[0]
    return s.strip()


def truncate(s: str, limit: int = 12000) -> str:
    """R2: keep the first limit//4 and last limit-limit//4 characters, noting how many were cut."""
    if len(s) <= limit:
        return s
    head = limit // 4
    tail = limit - head
    return f"{s[:head]}\n... [{len(s) - limit} chars truncated] ...\n{s[-tail:]}"


def parse_args(raw) -> dict | None:
    """R3: {} for blank, the dict for a JSON object, None for anything else."""
    if raw is None or not str(raw).strip():
        return {}
    try:
        v = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return v if isinstance(v, dict) else None


def preview(name: str, args: dict) -> str:
    """R4: short single-line description of a tool call for logs."""
    if name in ("write_file", "edit_file", "read_file"):
        extra = f" ({len(args.get('content', ''))} chars)" if name == "write_file" else ""
        return f"{args.get('path', '?')}{extra}"
    s = json.dumps(args, ensure_ascii=False)
    return s[:200] + ("…" if len(s) > 200 else "")


def msg_to_dict(msg) -> dict:
    """R5: convert a reply object into an assistant Message dict."""
    d: dict = {"role": "assistant", "content": strip_think(msg.content)}
    if msg.tool_calls:
        d["tool_calls"] = [
            {
                "id": tc.id or f"call_{uuid.uuid4().hex[:8]}",
                "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments or "{}"},
            }
            for tc in msg.tool_calls
        ]
    return d
