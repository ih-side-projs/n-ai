"""nai.web: SS-WEB v2 (WP8) - Full control dashboard with approvals, editing, escalation, tools."""

from .app import WebSettings, create_app

__all__ = ["create_app", "WebSettings"]