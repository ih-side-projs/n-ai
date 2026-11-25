"""Decide whether a build/test step runs on a remote worker.

Only platform-specific work leaves the orchestrator:

* a ``macos`` component runs its commands on a worker (``platform = "macos"``);
* an ``integrate`` job runs remotely when *any* node in its subtree is macos;
* everything else stays local.

Remote routing also requires ``settings.remote.enabled``.  Without it the step
runs locally and the caller can record a note, so a project with macos
components still builds on a single machine.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .errors import RemoteError

MACOS = "macos"
KNOWN_PLATFORMS = ("any", "linux", "macos")
#: Job kinds whose commands may leave the machine.
REMOTE_JOB_KINDS = frozenset({"contract_stub", "author_tests", "implement", "verify", "integrate"})


def _contract(node: dict[str, Any] | None) -> dict[str, Any]:
    value = (node or {}).get("contract")
    return value if isinstance(value, dict) else {}


def node_platform(store: Any, node: dict[str, Any] | None) -> str:
    """Return the platform a node builds for (``any`` when unknown).

    A leaf's own contract usually carries no platform; the parent's component
    list names the leaf and its platform, so both places are checked.
    """
    if not node:
        return "any"
    own = str(_contract(node).get("platform") or "").strip().lower()
    if own in KNOWN_PLATFORMS:
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
                platform = str(component.get("platform") or "any").strip().lower()
                return platform if platform in KNOWN_PLATFORMS else "any"
    return "any"


def subtree_has_platform(store: Any, project_id: str, node_id: str, platform: str = MACOS) -> bool:
    """True when *node_id* or any descendant builds for *platform*."""
    try:
        root = store.get_node(node_id)
    except Exception:  # noqa: BLE001
        root = None
    stack = [root] if root else []
    seen: set[str] = set()
    while stack:
        node = stack.pop()
        if node["id"] in seen:
            continue
        seen.add(node["id"])
        if node_platform(store, node) == platform:
            return True
        for child in store.list_nodes(project_id, parent_id=node["id"]):
            stack.append(child)
    return False


def remote_enabled(project: dict[str, Any]) -> bool:
    """``settings.remote.enabled`` turns remote routing on (default off)."""
    settings = project.get("settings") or {}
    block = settings.get("remote") if isinstance(settings, dict) else None
    return bool(isinstance(block, dict) and block.get("enabled"))


def integration_scope(store: Any, node: dict[str, Any] | None) -> str | None:
    """The node whose subtree an integration job verifies.

    An ``integration`` node wires its parent's children, so its scope is the
    parent (its siblings), not just its own children.
    """
    if not node:
        return None
    if node.get("kind") == "integration" and node.get("parent_id"):
        return str(node["parent_id"])
    return str(node["id"])


def resolved_platform(store: Any, project: dict[str, Any], node: dict[str, Any] | None) -> str:
    """The platform a node's commands must run on.

    An ``integration`` node inherits ``macos`` from its scope (its parent's
    subtree), because that is what its tests exercise.
    """
    platform = node_platform(store, node)
    if node is not None and node.get("kind") == "integration":
        scope = integration_scope(store, node)
        if scope and subtree_has_platform(store, project["id"], scope, MACOS):
            return MACOS
    return platform


def route_for(
    project: dict[str, Any],
    node: dict[str, Any] | None,
    job_kind: str,
    *,
    store: Any = None,
) -> tuple[bool, str]:
    """Return ``(remote?, reason)`` for one job's commands."""
    if job_kind not in REMOTE_JOB_KINDS:
        return False, f"{job_kind} runs on the orchestrator"
    if not remote_enabled(project):
        return False, "remote execution is disabled (settings.remote.enabled)"
    platform = resolved_platform(store, project, node)
    if job_kind == "integrate":
        if platform == MACOS:
            return True, "subtree contains macos components"
        return False, "no macos components in the subtree"
    if platform == MACOS:
        return True, "component platform is macos"
    return False, f"component platform is {platform}"


def target_for(
    project: dict[str, Any],
    node: dict[str, Any] | None,
    job_kind: str,
    recipe: str,
    *,
    store: Any = None,
    local_target: Any = None,
    make_remote: Callable[[str], Any] | None = None,
) -> Any:
    """Pick the execution target for one command.

    Returns *local_target* when the step stays here, or ``make_remote(recipe)``
    when it must run on a worker.  ``make_remote`` is required for a remote
    route: silently falling back to local would hide a misconfiguration.
    """
    remote, reason = route_for(project, node, job_kind, store=store)
    if not remote:
        return local_target
    if make_remote is None:
        raise RemoteError(f"{job_kind} needs a worker ({reason}) but no remote target is configured")
    return make_remote(recipe)
