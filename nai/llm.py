"""llm-client: one chat-completion call against the model server (v1 + CORE-2, X6).

New in v2: constrained output (`response_format` / `grammar`), model-swap detection, a
`ChatResult` that still unpacks like v1's `(reply, usage, seconds)`, and call observers.
"""
from __future__ import annotations

import json
import re
import time
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any

from .terminal import Streamer, log


class MissingDependency(ImportError):
    pass


@dataclass
class Usage:
    prompt_tokens: int
    completion_tokens: int | None = None
    estimated: bool = False   # True when the server reported no usage and prompt_tokens is a guess


@dataclass
class ChatResult:
    message: Any                      # Reply: .content, .tool_calls, (.reasoning when streaming)
    usage: Usage
    seconds: float
    swapped: bool = False             # did this call trigger a model load/swap? (CORE-2)
    swap_method: str = "none"         # "running" (llama-swap /running), "latency" (first token), or "none" (unknown)
    first_token_seconds: float | None = None
    finish_reason: str | None = None

    def __iter__(self):               # v1 compatibility: msg, usage, dt = llm.chat(...)
        yield self.message
        yield self.usage
        yield self.seconds


@dataclass
class CallRecord:
    """X6: everything about one model call, including the full prompt and response."""
    time: str
    model: str
    tag: str
    seconds: float
    prompt_tokens: int | None
    completion_tokens: int | None
    usage_estimated: bool
    swapped: bool
    swap_method: str
    first_token_seconds: float | None
    finish_reason: str | None
    request: dict
    response: dict | None
    error: str | None = None
    meta: dict = field(default_factory=dict)   # e.g. role, prompt ids, job id


def json_schema_format(schema: dict, name: str = "result") -> dict:
    """response_format value for llama-server's JSON-schema constrained decoding."""
    return {"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}}


class JsonlCallLog:
    """Observer that appends each CallRecord as one JSON line."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, rec: CallRecord) -> None:
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(rec), ensure_ascii=False, default=str) + "\n")


class LLM:
    def __init__(self, base_url: str, api_key: str, timeout: float = 3600, stream: bool = True,
                 show_thinking: bool = True, swap_threshold: float = 10.0, probe_running: bool = True,
                 prefill_tokens_per_second: float = 2000.0):
        try:
            from openai import OpenAI
        except ImportError as e:
            raise MissingDependency("Missing dependency: pip install openai") from e
        # Generous timeout: llama-swap may need to load a model first.
        self.client = OpenAI(base_url=base_url, api_key=api_key, timeout=timeout, max_retries=1)
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.stream = stream
        self.show_thinking = show_thinking
        self.swap_threshold = swap_threshold      # seconds to first token above which a call counts as a swap
        self.probe_running = probe_running
        self.prefill_tokens_per_second = max(1.0, prefill_tokens_per_second)
        self.observers: list[Callable[[CallRecord], None]] = []
        self._probe_retry_at = 0.0

    # -- swap detection ------------------------------------------------------
    def running_models(self) -> list[str] | None:
        """Models llama-swap reports as loaded (GET <root>/running), or None if it can't tell."""
        if not self.probe_running or time.time() < self._probe_retry_at:
            return None
        url = re.sub(r"/v1$", "", self.base_url) + "/running"
        try:
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.api_key}"})
            with urllib.request.urlopen(req, timeout=2) as r:
                data = json.load(r)
            items = data.get("running", []) if isinstance(data, dict) else data
            return [i["model"] for i in items if isinstance(i, dict) and i.get("state") in (None, "ready", "starting")]
        except Exception:
            self._probe_retry_at = time.time() + 60   # endpoint missing or server down: don't hammer it
            return None

    # -- the call --------------------------------------------------------------
    def chat(self, model, messages, sampling, tools=None, max_tokens=None, tag="llm",
             response_format: dict | None = None, grammar: str | None = None, meta: dict | None = None,
             cancel_event=None) -> ChatResult:
        s = dict(sampling)
        kw: dict[str, Any] = dict(model=model, messages=messages, temperature=s.pop("temperature"), top_p=s.pop("top_p"))
        if tools:
            kw.update(tools=tools, tool_choice="auto")
        if max_tokens:
            kw["max_tokens"] = max_tokens
        if response_format:
            kw["response_format"] = response_format
        if grammar:
            s["grammar"] = grammar
        kw["extra_body"] = s
        before = self.running_models()
        started = datetime.now(timezone.utc).isoformat(timespec="seconds")
        t0 = time.time()
        ttft: float | None = None
        try:
            if self.stream:
                msg, usage, finish, ttft = self._stream(kw, tag, t0, cancel_event=cancel_event)
            else:
                r = self.client.chat.completions.create(**kw)
                msg, usage, finish = r.choices[0].message, r.usage, r.choices[0].finish_reason
        except Exception as e:
            self._notify(CallRecord(started, model, tag, time.time() - t0, None, None, False, False, "none", None, None,
                                    self._req_dict(kw), None, f"{type(e).__name__}: {e}", dict(meta or {})))
            raise
        dt = time.time() - t0
        if finish == "length":
            log("llm", f"{model}: output hit max_tokens (truncated)", "yellow")
        ct = getattr(usage, "completion_tokens", None)
        if self.stream and ct:
            log(tag, f"↳ {model}: {ct} tokens in {dt:.0f}s ({ct / max(dt, 0.01):.1f} tok/s incl. load/prompt time)")
        pt = getattr(usage, "prompt_tokens", None)
        if pt:
            u = Usage(int(pt), ct)
        else:  # server did not report usage: rough estimate
            u = Usage(int(len(json.dumps(messages, ensure_ascii=False)) / 3.5), ct, True)
        if before is not None:
            # Authoritative: llama-swap's /running endpoint
            swapped, method = model not in before, "running"
        elif ttft is not None and pt:
            # Latency fallback: subtract estimated prompt prefill so a long
            # prompt is not mistaken for a model swap (Pass 4).
            estimated_prefill = int(pt) / self.prefill_tokens_per_second
            swapped = (ttft - estimated_prefill) > self.swap_threshold
            method = "latency-adjusted"
        else:
            swapped, method = False, "none"
        res = ChatResult(msg, u, dt, swapped, method, ttft, finish)
        self._notify(CallRecord(started, model, tag, dt, u.prompt_tokens, u.completion_tokens, u.estimated, swapped, method,
                                ttft, finish, self._req_dict(kw), self._resp_dict(msg), None, dict(meta or {})))
        return res

    @staticmethod
    def _req_dict(kw: dict) -> dict:
        return {k: kw[k] for k in ("messages", "tools", "max_tokens", "response_format") if k in kw} | \
               {"model": kw["model"], "temperature": kw["temperature"], "top_p": kw["top_p"], "extra": kw.get("extra_body", {})}

    @staticmethod
    def _resp_dict(msg) -> dict:
        calls = [{"id": getattr(tc, "id", None), "name": tc.function.name, "arguments": tc.function.arguments}
                 for tc in (getattr(msg, "tool_calls", None) or [])]
        return {"content": getattr(msg, "content", None), "reasoning": getattr(msg, "reasoning", None), "tool_calls": calls}

    def _notify(self, rec: CallRecord) -> None:
        for ob in self.observers:
            try:
                ob(rec)
            except Exception as e:   # an observer must never break a model call
                log("llm", f"call observer failed: {type(e).__name__}: {e}", "yellow")

    def _stream(self, kw, tag, t0, cancel_event=None):
        out = Streamer(tag, self.show_thinking)
        content: list[str] = []
        reasoning: list[str] = []
        calls: dict[int, dict] = {}
        usage = finish = ttft = None
        stream = self.client.chat.completions.create(**kw, stream=True, stream_options={"include_usage": True})
        try:
            for chunk in stream:
                if cancel_event is not None and cancel_event.is_set():
                    stream.close()
                    raise InterruptedError("model generation cancelled")
                if getattr(chunk, "usage", None):
                    usage = chunk.usage
                if not chunk.choices:
                    continue
                ch = chunk.choices[0]
                if ch.finish_reason:
                    finish = ch.finish_reason
                d = ch.delta
                r = getattr(d, "reasoning_content", None) or getattr(d, "reasoning", None)
                if r:
                    ttft = ttft if ttft is not None else time.time() - t0
                    reasoning.append(r)
                    out.think(r)
                if getattr(d, "content", None):
                    ttft = ttft if ttft is not None else time.time() - t0
                    content.append(d.content)
                    out.text(d.content)
                for t in getattr(d, "tool_calls", None) or []:
                    ttft = ttft if ttft is not None else time.time() - t0
                    c = calls.setdefault(t.index, {"id": None, "name": "", "args": ""})
                    if t.id:
                        c["id"] = t.id
                    if t.function:
                        if t.function.name and not c["name"]:
                            c["name"] = t.function.name
                        if t.function.arguments:
                            c["args"] += t.function.arguments
                    out.tool(c["name"], len(c["args"]))
        finally:
            out.end()
            try:
                stream.close()
            except Exception:
                pass
        tool_calls = [NS(id=c["id"], function=NS(name=c["name"], arguments=c["args"]))
                      for _, c in sorted(calls.items()) if c["name"]] or None
        msg = NS(content="".join(content), tool_calls=tool_calls, reasoning="".join(reasoning))
        return msg, usage, finish, ttft
