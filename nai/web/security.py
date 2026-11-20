"""Small security helpers for the local N-AI web interface."""

from __future__ import annotations

import hmac
import secrets
from urllib.parse import urlsplit

from fastapi import HTTPException, Request


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def verify_same_origin(request: Request) -> None:
    """Reject explicit cross-origin writes; token validation is performed separately."""
    origin = request.headers.get("origin")
    if not origin:
        return
    parsed = urlsplit(origin)
    request_host = request.headers.get("host", "")
    expected = f"{request.url.scheme}://{request_host}"
    if f"{parsed.scheme}://{parsed.netloc}" != expected:
        raise HTTPException(status_code=403, detail="Cross-origin request rejected")


def verify_token(cookie_token: str | None, supplied_token: str | None) -> None:
    if not cookie_token or not supplied_token or not hmac.compare_digest(cookie_token, supplied_token):
        raise HTTPException(status_code=403, detail="CSRF token missing or invalid")


def verify_bearer_or_cookie(request: Request, expected: str) -> bool:
    authorization = request.headers.get("authorization", "")
    supplied = authorization[7:].strip() if authorization.lower().startswith("bearer ") else request.cookies.get("nai_auth")
    return bool(supplied and hmac.compare_digest(supplied, expected))
