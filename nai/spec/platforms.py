"""Platform values a contract component may declare (SPEC3_PLATFORM)."""

from __future__ import annotations

PLATFORMS: tuple[str, ...] = ("any", "linux", "macos")
DEFAULT_PLATFORM = "any"

#: A component on one of these platforms may not consume a component that is
#: restricted to a different one.
PORTABLE_PLATFORMS: frozenset[str] = frozenset({"any", "linux"})

__all__ = ["DEFAULT_PLATFORM", "PLATFORMS", "PORTABLE_PLATFORMS"]
