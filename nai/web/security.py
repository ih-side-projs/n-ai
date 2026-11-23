"""Small security helpers for the local N-AI web interface."""

from __future__ import annotations

import hmac
import logging
import os
import secrets
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

logger = logging.getLogger("nai.web.security")


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
    sec_fetch_site = (request.headers.get("sec-fetch-site") or "").lower()
    requested_with = (request.headers.get("x-requested-with") or "").lower()
    if sec_fetch_site == "same-origin":
        return
    parsed = urlsplit(origin)
    origin_host = (parsed.hostname or "").lower()
    allowed = _configured_hosts()
    if origin_host in allowed:
        return
    # Some privacy-focused browsers/extensions send `Origin: null` even for a
    # same-origin form post. Accept that opaque origin only when the request was
    # made by our own JavaScript with a custom header: a cross-origin HTML form
    # cannot set this header, and cross-origin fetch would require CORS.
    if origin.strip().lower() == "null" and requested_with == "fetch":
        return
    logger.warning(
        "cross-origin rejected: origin=%r parsed_host=%r host=%r sec_fetch_site=%r x_requested_with=%r allowed=%s",
        origin, origin_host, request.headers.get("host", ""), sec_fetch_site, requested_with, sorted(allowed),
    )
    raise HTTPException(
        status_code=403,
        detail=f"Cross-origin request rejected: origin host {origin_host!r} is not allowlisted",
    )


def verify_token(cookie_token: str | None, supplied_token: str | None) -> None:
    if not cookie_token or not supplied_token or not hmac.compare_digest(cookie_token, supplied_token):
        raise HTTPException(status_code=403, detail="CSRF token missing or invalid")


def verify_bearer_or_cookie(request: Request, expected: str) -> bool:
    authorization = request.headers.get("authorization", "")
    supplied = authorization[7:].strip() if authorization.lower().startswith("bearer ") else request.cookies.get("nai_auth")
    return bool(supplied and hmac.compare_digest(supplied, expected))
