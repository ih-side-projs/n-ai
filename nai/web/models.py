"""Read-only llama-swap/OpenAI-compatible model-server inspection."""

from __future__ import annotations

import ipaddress
import json
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlsplit, urlunsplit


class ModelServerError(RuntimeError):
    pass


class ModelServerClient:
    def __init__(self, base_url: str | None, api_key: str | None = None, timeout: float = 3.0):
        self.base_url = (base_url or "").rstrip("/")
        if self.base_url:
            parsed = urlsplit(self.base_url)
            hostname = parsed.hostname or ""
            try:
                loopback = ipaddress.ip_address(hostname).is_loopback
            except ValueError:
                loopback = hostname.lower() == "localhost"
            if parsed.scheme not in {"http", "https"} or not loopback or parsed.username or parsed.password:
                raise ValueError("model server URL must be an http(s) loopback address without embedded credentials")
            if parsed.query or parsed.fragment:
                raise ValueError("model server URL must not include a query string or fragment")
        self.api_key = api_key
        self.timeout = timeout
        self.root_url = ""
        self.api_base_url = ""
        if self.base_url:
            # LLAMA_SWAP_URL/NAI_LLAMA_SWAP_URL is the OpenAI-compatible base URL
            # and commonly ends in /v1. llama-swap's health and /running endpoints
            # are on the server root, so keep both roots explicitly.
            path = parsed.path.rstrip("/")
            if path.endswith("/v1"):
                root_path = path[:-3].rstrip("/")
                api_path = path
            else:
                root_path = path
                api_path = f"{path}/v1" if path else "/v1"
            self.root_url = urlunsplit((parsed.scheme, parsed.netloc, root_path, "", ""))
            self.api_base_url = urlunsplit((parsed.scheme, parsed.netloc, api_path, "", ""))

    def _url_for(self, path: str) -> str:
        """Resolve an endpoint path against the correct root or OpenAI base."""
        if path == "/v1" or path.startswith("/v1/"):
            return self.api_base_url + path[3:] if path != "/v1" else self.api_base_url
        if path == "/health" or path.startswith("/health/") or path == "/running" or path.startswith("/running/"):
            return self.root_url + path
        return self.base_url + path

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    def _request(self, path: str, *, method: str = "GET", body: dict[str, Any] | None = None,
                 timeout: float | None = None) -> tuple[int, Any, float]:
        if not self.configured:
            raise ModelServerError("Model server URL is not configured")
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self._url_for(path), data=data, headers=headers, method=method)
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                raw = response.read(2_000_000)
                elapsed = time.monotonic() - started
                content_type = response.headers.get("Content-Type", "")
                if "json" in content_type:
                    try:
                        payload = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        payload = {"raw": raw.decode("utf-8", errors="replace")[:2000]}
                else:
                    payload = {"raw": raw.decode("utf-8", errors="replace")[:2000]}
                return int(response.status), payload, elapsed
        except urllib.error.HTTPError as exc:
            detail = exc.read(2000).decode("utf-8", errors="replace")
            raise ModelServerError(f"HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ModelServerError(f"{type(exc).__name__}: {exc}") from exc

    def snapshot(self) -> dict[str, Any]:
        if not self.configured:
            return {"configured": False, "healthy": None, "error": "NAI_LLAMA_SWAP_URL is not configured",
                    "models": [], "loaded_models": []}
        output: dict[str, Any] = {"configured": True, "healthy": False, "models": [], "loaded_models": [], "error": None}
        try:
            status, payload, elapsed = self._request("/health")
            output["healthy"] = status == 200
            output["health_seconds"] = round(elapsed, 3)
            output["health_response"] = payload
        except ModelServerError as exc:
            output["error"] = str(exc)
        try:
            _, payload, _ = self._request("/v1/models")
            if isinstance(payload, dict):
                output["models"] = payload.get("data", payload.get("models", [])) or []
            elif isinstance(payload, list):
                output["models"] = payload
        except ModelServerError as exc:
            output["models_error"] = str(exc)
        try:
            _, payload, _ = self._request("/running")
            output["loaded_models"] = _extract_running_models(payload)
        except ModelServerError as exc:
            output["running_error"] = str(exc)
        return output

    def smoke_test(self, model_id: str, *, timeout: float = 180.0) -> dict[str, Any]:
        status, payload, elapsed = self._request(
            "/v1/chat/completions", method="POST", timeout=timeout,
            body={"model": model_id, "messages": [{"role": "user", "content": "Reply with exactly: nai-ok"}],
                  "temperature": 0, "max_tokens": 8, "stream": False},
        )
        choices = payload.get("choices", []) if isinstance(payload, dict) else []
        content = ""
        if choices:
            message = choices[0].get("message") or {}
            content = str(message.get("content") or "")[:300]
        usage = payload.get("usage", {}) if isinstance(payload, dict) else {}
        return {"ok": status == 200, "status_code": status, "model_id": model_id,
                "latency_seconds": round(elapsed, 3), "response": content, "usage": usage}


def _extract_running_models(payload: Any) -> list[dict[str, Any]]:
    """Normalize known /running response shapes without assuming one schema."""
    raw: Any = payload
    if isinstance(raw, dict):
        for key in ("running", "models", "data", "loaded"):
            if key in raw:
                raw = raw[key]
                break
        else:
            raw = [raw]
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    result = []
    for item in raw:
        if isinstance(item, str):
            result.append({"id": item, "status": "running"})
        elif isinstance(item, dict):
            model_id = item.get("id") or item.get("model") or item.get("model_id") or item.get("name")
            if model_id:
                result.append({"id": str(model_id), "status": str(item.get("status") or item.get("state") or "running")})
    return result
