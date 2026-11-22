"""Small security helpers for the local N-AI web interface."""

from __future__ import annotations

import hmac
import os
import secrets
from urllib.parse import urlsplit

from fastapi import HTTPException, Request


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def _configured_hosts() -> set[str]:
    """Fixed host allowlist: loopback plus explicitly configured dashboard names."""
    hosts = {"localhost", "127.0.0.1", "::1"}
    for var in ("NAI_WEB_ALLOWED_HOSTS", "NAI_WEB_HOST"):
        value = os.environ.get(var, "")
        for part in value.split(","):
            part = part.strip().lower()
            if part and part not in ("0.0.0.0", "::", "*"):
                hosts.add(part.split(":", 1)[0])
    return hosts


def verify_same_origin(request: Request) -> None:
    """Reject cross-origin writes using a fixed allowlist.

    The request's own ``Host`` and ``X-Forwarded-Host`` are deliberately NOT
    trusted: DNS rebinding can make a malicious page send a matching Host.
    """
    origin = request.headers.get("origin")
    if not origin:
        return
    if (request.headers.get("sec-fetch-site") or "").lower() == "same-origin":
        return
    parsed = urlsplit(origin)
    origin_host = (parsed.hostname or "").lower()
    if origin_host not in _configured_hosts():
        raise HTTPException(status_code=403, detail="Cross-origin request rejected")


def verify_token(cookie_token: str | None, supplied_token: str | None) -> None:
    if not cookie_token or not supplied_token or not hmac.compare_digest(cookie_token, supplied_token):
        raise HTTPException(status_code=403, detail="CSRF token missing or invalid")


def verify_bearer_or_cookie(request: Request, expected: str) -> bool:
    authorization = request.headers.get("authorization", "")
    supplied = authorization[7:].strip() if authorization.lower().startswith("bearer ") else request.cookies.get("nai_auth")
    return bool(supplied and hmac.compare_digest(supplied, expected))
