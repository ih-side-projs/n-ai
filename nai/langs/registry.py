"""Language adapter registry.

The registry is the single source of truth for "supported languages".  It is
populated by importing :mod:`nai.langs`, which registers the built-in adapters.
"""

from __future__ import annotations

from typing import Any

from .base import LanguageAdapter

_REGISTRY: dict[str, LanguageAdapter] = {}
DEFAULT_LANGUAGE = "python"


class UnknownLanguageError(LookupError):
    """Raised when a language has no registered adapter."""


def register(adapter: LanguageAdapter) -> LanguageAdapter:
    """Register *adapter* under its ``id``.  Re-registering replaces it."""
    _REGISTRY[adapter.id] = adapter
    return adapter


def unregister(language_id: str) -> None:
    """Remove *language_id* from the registry (plugins and tests)."""
    _REGISTRY.pop(language_id, None)


def get(language_id: str | None) -> LanguageAdapter:
    """Return the adapter for *language_id* (defaults to Python)."""
    key = (language_id or DEFAULT_LANGUAGE).strip().lower()
    try:
        return _REGISTRY[key]
    except KeyError:
        raise UnknownLanguageError(
            f"unsupported language {language_id!r}; supported: {', '.join(supported())}"
        ) from None


def supported() -> list[str]:
    """Return the sorted ids of every registered adapter."""
    return sorted(_REGISTRY)


def all_adapters() -> dict[str, LanguageAdapter]:
    """Return a copy of the registry."""
    return dict(_REGISTRY)


def language_for(project: dict[str, Any] | None) -> LanguageAdapter:
    """Return the adapter selected by ``project.settings.language``."""
    settings = (project or {}).get("settings") or {}
    return get(str(settings.get("language") or DEFAULT_LANGUAGE))
