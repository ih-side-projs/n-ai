"""Scripted OpenAI-compatible stub model server for deterministic tests (X8, EVAL-2 starting point).

Serves POST /v1/chat/completions (plain and SSE streaming, tools, response_format/grammar are
recorded), GET /v1/models and GET /running (llama-swap style). Requests are classified like the
v1 spec's scenarios: model "r" with `tools` -> channel "orch"; model "r" without -> "consult";
model "c" -> "coder". Each channel is a script (list of replies) consumed in order.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def call(name: str, **args) -> dict:
    return {"name": name, "arguments": args}


def reply(content: str | None = None, calls: list[dict] | None = None, reasoning: str | None = None,
          prompt_tokens: int | None = 100, completion_tokens: int | None = 10, finish_reason: str | None = None,
          first_token_delay: float = 0.0, error: int | None = None) -> dict:
    return dict(content=content, calls=calls or [], reasoning=reasoning, prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens, finish_reason=finish_reason, first_token_delay=first_token_delay,
                error=error)


class StubServer:
    def __init__(self, orch=None, consult=None, coder=None, models=("r", "c"), repeat_last=(), running_endpoint=True,
                 swap_delay: float = 0.0, classify=None):
        self.scripts = {"orch": list(orch or []), "consult": list(consult or []), "coder": list(coder or [])}
        self.repeat_last = set(repeat_last)        # channels whose last reply repeats forever
        self.models = list(models)
        self.running_endpoint = running_endpoint
        self.swap_delay = swap_delay               # extra delay before first token when the model must be "loaded"
        self.classify = classify or self._classify
        self.running: list[str] = []
        self.requests: list[dict] = []             # every chat request body, in order, with "channel" added
        self._lock = threading.Lock()
        self._httpd: ThreadingHTTPServer | None = None

    @staticmethod
    def _classify(body: dict) -> str:
        if body.get("model") == "c":
            return "coder"
        return "orch" if body.get("tools") else "consult"

    # -- lifecycle -------------------------------------------------------------
    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_address[1]}/v1"

    def start(self) -> StubServer:
        srv = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence
                pass

            def _json(self, code, obj):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path.endswith("/models"):
                    self._json(200, {"object": "list", "data": [{"id": m, "object": "model"} for m in srv.models]})
                elif self.path == "/running" and srv.running_endpoint:
                    self._json(200, {"running": [{"model": m, "state": "ready"} for m in srv.running]})
                else:
                    self._json(404, {"error": "not found"})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                srv._chat(self, body)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    # -- helpers for assertions --------------------------------------------------
    def on(self, channel: str) -> list[dict]:
        return [r for r in self.requests if r["channel"] == channel]

    def remaining(self, channel: str) -> int:
        return len(self.scripts[channel])

    # -- chat ----------------------------------------------------------------------
    def _next(self, channel: str):
        with self._lock:
            s = self.scripts.setdefault(channel, [])
            if not s:
                return None
            if channel in self.repeat_last and len(s) == 1:
                return s[0]
            return s.pop(0)

    def _chat(self, h, body: dict) -> None:
        channel = self.classify(body)
        with self._lock:
            self.requests.append({**body, "channel": channel})
            model = body.get("model")
            needs_load = model not in self.running
            if needs_load:
                self.running = [model]
        rep = self._next(channel)
        if rep is None:
            h._json(500, {"error": {"message": f"stub script exhausted for channel {channel}", "type": "stub"}})
            return
        if rep["error"]:
            h._json(rep["error"], {"error": {"message": "scripted error", "type": "stub"}})
            return
        delay = rep["first_token_delay"] + (self.swap_delay if needs_load else 0)
        if body.get("stream"):
            self._stream(h, body, rep, delay)
            return
        time.sleep(delay)
        msg = {"role": "assistant", "content": rep["content"]}
        if rep["reasoning"]:
            msg["reasoning_content"] = rep["reasoning"]
        if rep["calls"]:
            msg["tool_calls"] = [{"id": f"call_{i}", "type": "function",
                                  "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}}
                                 for i, c in enumerate(rep["calls"])]
        out = {"id": "stub", "object": "chat.completion", "created": 0, "model": model,
               "choices": [{"index": 0, "message": msg,
                            "finish_reason": rep["finish_reason"] or ("tool_calls" if rep["calls"] else "stop")}]}
        if rep["prompt_tokens"] is not None:
            out["usage"] = {"prompt_tokens": rep["prompt_tokens"], "completion_tokens": rep["completion_tokens"],
                            "total_tokens": rep["prompt_tokens"] + (rep["completion_tokens"] or 0)}
        h._json(200, out)

    def _stream(self, h, body, rep, delay) -> None:
        h.send_response(200)
        h.send_header("Content-Type", "text/event-stream")
        h.end_headers()

        def send(delta=None, finish=None, usage=None):
            ch = {"id": "stub", "object": "chat.completion.chunk", "created": 0, "model": body.get("model"),
                  "choices": [] if usage else [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}
            if usage:
                ch["usage"] = usage
            h.wfile.write(f"data: {json.dumps(ch)}\n\n".encode())
            h.wfile.flush()

        time.sleep(delay)
        send({"role": "assistant"})
        if rep["reasoning"]:
            half = len(rep["reasoning"]) // 2
            send({"reasoning_content": rep["reasoning"][:half]})
            send({"reasoning_content": rep["reasoning"][half:]})
        if rep["content"]:
            half = len(rep["content"]) // 2
            send({"content": rep["content"][:half]})
            send({"content": rep["content"][half:]})
        for i, c in enumerate(rep["calls"]):
            args = json.dumps(c["arguments"])
            send({"tool_calls": [{"index": i, "id": f"call_{i}", "type": "function",
                                  "function": {"name": c["name"], "arguments": ""}}]})
            half = len(args) // 2
            for frag in (args[:half], args[half:]):   # arguments arrive in fragments, without name/id
                send({"tool_calls": [{"index": i, "function": {"arguments": frag}}]})
        send({}, finish=rep["finish_reason"] or ("tool_calls" if rep["calls"] else "stop"))
        if rep["prompt_tokens"] is not None:
            send(usage={"prompt_tokens": rep["prompt_tokens"], "completion_tokens": rep["completion_tokens"],
                        "total_tokens": rep["prompt_tokens"] + (rep["completion_tokens"] or 0)})
        h.wfile.write(b"data: [DONE]\n\n")
        h.wfile.flush()
