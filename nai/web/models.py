"""Model server client for WP8."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import httpx


class ModelServerError(Exception):
    """Raised when the model server returns an error."""
    pass


@dataclass
class ModelServerClient:
    """Client for interacting with the local llama-swap model server."""
    base_url: str | None
    api_key: str | None
    _client: httpx.AsyncClient | None = None

    def __post_init__(self):
        if self.base_url:
            self._client = httpx.AsyncClient(
                base_url=self.base_url.rstrip("/"),
                headers={"Authorization": f"Bearer {self.api_key}"} if self.api_key else {},
                timeout=30.0,
            )

    async def close(self):
        if self._client:
            await self._client.aclose()

    def snapshot(self) -> dict[str, Any]:
        """Return a snapshot of the model server state (synchronous, for template rendering)."""
        if not self._client or not self.base_url:
            return {"models": [], "running": None, "healthy": False}
        try:
            with httpx.Client(
                base_url=self.base_url.rstrip("/"),
                headers={"Authorization": f"Bearer {self.api_key}"} if self.api_key else {},
                timeout=5.0,
            ) as client:
                models_resp = client.get("/v1/models")
                models_resp.raise_for_status()
                models = models_resp.json().get("data", [])

                running = None
                try:
                    running_resp = client.get("/running")
                    if running_resp.status_code == 200:
                        running = running_resp.json().get("model")
                except httpx.HTTPError:
                    pass

                return {"models": models, "running": running, "healthy": True}
        except httpx.HTTPError:
            return {"models": [], "running": None, "healthy": False}

    async def smoke_test(self, model_id: str) -> dict[str, Any]:
        """Run a quick smoke test against the specified model."""
        if not self._client:
            raise ModelServerError("Model server not configured")

        start = time.perf_counter()
        try:
            resp = await self._client.post(
                "/v1/chat/completions",
                json={
                    "model": model_id,
                    "messages": [{"role": "user", "content": "Say 'ok'."}],
                    "max_tokens": 10,
                    "temperature": 0,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            latency_ms = int((time.perf_counter() - start) * 1000)
            return {
                "ok": True,
                "model_id": model_id,
                "latency_ms": latency_ms,
                "response": data.get("choices", [{}])[0].get("message", {}).get("content", ""),
            }
        except httpx.HTTPError as exc:
            latency_ms = int((time.perf_counter() - start) * 1000)
            raise ModelServerError(f"Smoke test failed: {exc}") from exc