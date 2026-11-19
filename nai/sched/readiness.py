"""SCH-4: a node becomes `ready` only when every node in depends_on is `done` and its parent's contract is approved."""
from __future__ import annotations

from .util import is_invalid_transition

CONTRACT_STAGE = "interfaces"
APPROVING = ("approved", "edited")


def walk_nodes(store, project_id: str) -> list[dict]:
    """All nodes of a project, parents before children (list_nodes only returns one level at a time)."""
    out, frontier = [], [None]
    while frontier:
        parent = frontier.pop(0)
        kids = store.list_nodes(project_id, parent_id=parent)
        out.extend(kids)
        frontier.extend(k["id"] for k in kids if k.get("kind") not in ("leaf", "integration"))
    return out


def depth_map(nodes: list[dict]) -> dict[str, int]:
    by_id = {n["id"]: n for n in nodes}
    depth: dict[str, int] = {}

    def d(nid: str) -> int:
        if nid not in depth:
            p = by_id[nid].get("parent_id")
            depth[nid] = 0 if not p or p not in by_id else d(p) + 1
        return depth[nid]

    for n in nodes:
        d(n["id"])
    return depth


def dep_ids(node: dict) -> list[str]:
    return [d["id"] if isinstance(d, dict) else d for d in (node.get("depends_on") or [])]


def contract_approved(store, parent_id: str | None, cache: dict) -> bool:
    """No parent (the root) has no contract to wait for. Otherwise the latest `interfaces` approval must approve it."""
    if not parent_id:
        return True
    if parent_id not in cache:
        rows = [(i, a) for i, a in enumerate(store.list_approvals(parent_id)) if a.get("stage") == CONTRACT_STAGE]
        rows.sort(key=lambda t: (str(t[1].get("time") or ""), t[0]))
        cache[parent_id] = bool(rows) and rows[-1][1].get("decision") in APPROVING
    return cache[parent_id]


def promote_ready(store, nodes: list[dict], *, require_contract: bool = True) -> list[str]:
    """Move every `approved` node whose dependencies are done and whose parent contract is approved to `ready`."""
    by_id = {n["id"]: n for n in nodes}
    cache: dict = {}
    promoted = []
    for n in nodes:
        if n.get("status") != "approved":
            continue
        if any(by_id.get(d, {}).get("status") != "done" for d in dep_ids(n)):
            continue
        if require_contract and not contract_approved(store, n.get("parent_id"), cache):
            continue
        try:
            store.transition_node_status(n["id"], "ready", note="dependencies done and parent contract approved")
        except Exception as e:
            if not is_invalid_transition(e):
                raise
            continue
        n["status"] = "ready"
        promoted.append(n["id"])
    return promoted
