"""Stub model server for deterministic testing (WP9 / EVAL-2)."""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections import deque
from dataclasses import dataclass, field

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel


@dataclass
class StubServerConfig:
    delay_range: tuple[float, float] = (0.01, 0.1)
    error_rate: float = 0.0
    error_codes: list[int] = field(default_factory=lambda: [500, 503])
    stream_chunk_size: int = 10
    models: list[str] = field(default_factory=lambda: ["test-reasoner", "test-coder"])
    running_model: str | None = "test-reasoner"


class ChatMessage(BaseModel):
    role: str
    content: str | None = None
    tool_calls: list[dict] | None = None


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    max_tokens: int | None = None
    temperature: float | None = None
    stream: bool = False
    response_format: dict | None = None
    tools: list[dict] | None = None
    tool_choice: str | dict | None = None


class StubModelServer:
    """Scripted OpenAI-compatible stub server for deterministic testing."""
    
    def __init__(self, config: StubServerConfig | None = None):
        self.config = config or StubServerConfig()
        self.call_history: list[dict] = []
        self.scripted_responses: deque[dict] = deque()
        self.request_count = 0
    
    def add_scripted_response(self, response: dict) -> None:
        self.scripted_responses.append(response)
    
    def add_scripted_responses(self, responses: list[dict]) -> None:
        self.scripted_responses.extend(responses)
    
    def create_app(self) -> FastAPI:
        app = FastAPI(title="Stub Model Server", version="1.0.0")
        
        @app.get("/v1/models")
        async def list_models():
            return {
                "object": "list",
                "data": [
                    {"id": m, "object": "model", "owned_by": "stub", "permission": []}
                    for m in self.config.models
                ]
            }
        
        @app.get("/running")
        async def get_running():
            return {"model": self.config.running_model or ""}
        
        @app.get("/health")
        async def health():
            return {"status": "healthy"}
        
        @app.post("/v1/chat/completions")
        async def chat_completions(request: ChatCompletionRequest, raw_request: Request):
            self.request_count += 1
            call_record = {
                "id": self.request_count,
                "timestamp": time.time(),
                "model": request.model,
                "messages": [m.model_dump() for m in request.messages],
                "stream": request.stream,
                "tools": request.tools,
                "response_format": request.response_format,
            }
            self.call_history.append(call_record)
            
            delay = random.uniform(*self.config.delay_range)
            await asyncio.sleep(delay)
            
            if random.random() < self.config.error_rate:
                error_code = random.choice(self.config.error_codes)
                raise HTTPException(status_code=error_code, detail="Simulated error")
            
            if self.scripted_responses:
                response = self.scripted_responses.popleft()
                return response
            
            return self._generate_response(request)
        
        return app
    
    def _generate_response(self, request: ChatCompletionRequest) -> dict:
        last_message = request.messages[-1].content if request.messages else ""
        
        if request.tools:
            tool_name = request.tools[0]["function"]["name"]
            return {
                "id": f"chatcmpl-{self.request_count}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": request.model,
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "id": f"call_{self.request_count}",
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": json.dumps({"result": "ok"})
                            }
                        }]
                    },
                    "finish_reason": "tool_calls"
                }],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
            }
        elif request.response_format:
            return {
                "id": f"chatcmpl-{self.request_count}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": request.model,
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": json.dumps({"status": "ok", "result": "success"})
                    },
                    "finish_reason": "stop"
                }],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
            }
        else:
            return {
                "id": f"chatcmpl-{self.request_count}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": request.model,
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": f"Response to: {last_message[:50]}"
                    },
                    "finish_reason": "stop"
                }],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
            }
    
    def get_call_history(self) -> list[dict]:
        return self.call_history.copy()
    
    def reset(self) -> None:
        self.call_history.clear()
        self.scripted_responses.clear()
        self.request_count = 0


class StubServer:
    def __init__(self, orch=None, consult=None, coder=None):
        self.stub = StubModelServer()
        if orch:
            for resp in orch:
                self.stub.add_scripted_response(resp)
    
    def reply(self, *args, **kwargs):
        pass
    
    def call(self, *args, **kwargs):
        pass