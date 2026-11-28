"""Platform values a contract component may declare (SPEC3_PLATFORM)."""

from __future__ import annotations

from typing import Any

PLATFORMS: tuple[str, ...] = ("any", "linux", "macos")
DEFAULT_PLATFORM = "any"

#: A component on one of these platforms may not consume a component that is
#: restricted to a different one.
PORTABLE_PLATFORMS: frozenset[str] = frozenset({"any", "linux"})


def _contract(node: dict[str, Any] | None) -> dict[str, Any]:
    value = (node or {}).get("contract")
    return value if isinstance(value, dict) else {}


def node_platform(store: Any, node: dict[str, Any] | None) -> str:
    """Return the platform a node builds for (``any`` when unknown).

    A leaf's own contract usually carries no platform; the parent's component
    list names the leaf and its platform, so both places are checked.
    """
    if not node:
        return DEFAULT_PLATFORM
    own = str(_contract(node).get("platform") or "").strip().lower()
    if own in PLATFORMS:
        return own
    name = str(node.get("name") or "").strip()
    parent_id = node.get("parent_id")
    if store is not None and parent_id:
        try:
            parent = store.get_node(parent_id)
        except Exception:  # noqa: BLE001 - a missing parent is not a routing failure
            parent = None
        for component in _contract(parent).get("components") or []:
            if isinstance(component, dict) and str(component.get("name") or "").strip() == name:
                platform = str(component.get("platform") or DEFAULT_PLATFORM).strip().lower()
                return platform if platform in PLATFORMS else DEFAULT_PLATFORM
    return DEFAULT_PLATFORM


__all__ = ["DEFAULT_PLATFORM", "PLATFORMS", "PORTABLE_PLATFORMS", "node_platform"]
