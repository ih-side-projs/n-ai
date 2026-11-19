"""nai.web: SS-WEB v2 (WP8) - Full control dashboard with approvals, editing, escalation, tools."""

from .app import create_app, WebSettings

__all__ = ["create_app", "WebSettings"]