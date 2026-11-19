"""Security utilities for WP8 web interface."""

from __future__ import annotations

import hmac
import secrets
from typing import Any

from fastapi import HTTPException, Request


def new_csrf_token() -> str:
    """Generate a new CSRF token."""
    return secrets.token_urlsafe(32)


def verify_token(cookie_token: str | None, supplied_token: str) -> None:
    """Verify that the supplied token matches the cookie token using constant-time comparison."""
    if not cookie_token:
        raise HTTPException(status_code=403, detail="CSRF token missing from cookie")
    if not supplied_token:
        raise HTTPException(status_code=403, detail="CSRF token missing from request")
    if not hmac.compare_digest(cookie_token, supplied_token):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")


def verify_same_origin(request: Request) -> None:
    """Verify that the request originates from the same origin."""
    origin = request.headers.get("origin")
    referer = request.headers.get("referer")
    host = request.headers.get("host")

    if not origin and not referer:
        raise HTTPException(status_code=403, detail="Missing Origin/Referer header")

    expected = f"http://{host}"
    expected_https = f"https://{host}"

    for header_value in (origin, referer):
        if header_value and not (header_value.startswith(expected) or header_value.startswith(expected_https)):
            raise HTTPException(status_code=403, detail="Cross-origin request denied")


def verify_bearer_or_cookie(request: Request, bearer_token: str) -> bool:
    """Verify bearer token in Authorization header or cookie."""
    # Check Authorization header
    auth = request.headers.get("authorization")
    if auth and auth.startswith("Bearer "):
        token = auth[7:]
        return hmac.compare_digest(token, bearer_token)

    # Check cookie
    cookie_token = request.cookies.get("nai_auth")
    if cookie_token:
        return hmac.compare_digest(cookie_token, bearer_token)

    return False