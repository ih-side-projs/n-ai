"""Named workflow phases derived from existing state (no schema change).

Two phases exist: ``design`` (generating and approving the spec) and
``implementation`` (coding, testing, integration). They may overlap at the
project level: different nodes can be in different phases at the same time.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from typing import Any

DESIGN = "design"
IMPLEMENTATION = "implementation"

# Job kinds belonging to each phase (matches the spec pipeline / build pipeline).
DESIGN_JOBS = frozenset({
    "clarify", "decompose", "select_map", "interfaces", "check_interfaces",
    "component_spec", "lint", "reconcile",
})
IMPLEMENTATION_JOBS = frozenset({
    "contract_stub", "author_tests", "implement", "verify", "integrate",
    "diagnose", "review",
})

# Node statuses that unambiguously identify a phase.
DESIGN_STATUSES = frozenset({"draft", "awaiting_approval", "approved"})
IMPLEMENTATION_STATUSES = frozenset({
    "ready", "running", "verifying", "done", "failed", "escalated", "blocked",
})


def node_phase(node: dict[str, Any], jobs: Iterable[dict[str, Any]] | None = None) -> str:
    """Derive a node's phase from its status and (for `stale`) recent job kinds.

    Rules:
      * design statuses -> design, implementation statuses -> implementation
      * `stale` is ambiguous: use the most recent job kind when available
      * default to design for nodes that have not started implementation
    """
    status = str(node.get("status") or "")
    if status in DESIGN_STATUSES:
        return DESIGN
    if status in IMPLEMENTATION_STATUSES:
        return IMPLEMENTATION

    # stale (or unknown): disambiguate via job history
    recent = list(jobs or [])
    if recent:
        def key(j: dict[str, Any]) -> str:
            return str(j.get("created_at") or j.get("started_at") or j.get("ended_at") or "")

        latest = max(recent, key=key)
        kind = str(latest.get("kind") or "")
        if kind in IMPLEMENTATION_JOBS:
            return IMPLEMENTATION
        if kind in DESIGN_JOBS:
            return DESIGN
    return DESIGN


def job_phase(kind: str) -> str | None:
    """Return the phase a job kind belongs to, or None if unknown."""
    if kind in DESIGN_JOBS:
        return DESIGN
    if kind in IMPLEMENTATION_JOBS:
        return IMPLEMENTATION
    return None


def phase_summary(nodes: Iterable[dict[str, Any]], jobs_by_node: dict[str, list[dict[str, Any]]] | None = None) -> dict[str, Any]:
    """Aggregate node phases into a project-level summary.

    A project is ``design`` when every node is in design, ``implementation``
    when every node is in implementation, and ``mixed`` otherwise. Empty
    projects are reported as ``design`` (their next step is specification).
    """
    jobs_by_node = jobs_by_node or {}
    counts = Counter()
    per_node: dict[str, str] = {}
    for node in nodes:
        phase = node_phase(node, jobs_by_node.get(node.get("id"), []))
        counts[phase] += 1
        per_node[node.get("id")] = phase

    total = sum(counts.values())
    if total == 0:
        phase = DESIGN
    elif counts[IMPLEMENTATION] == 0:
        phase = DESIGN
    elif counts[DESIGN] == 0:
        phase = IMPLEMENTATION
    else:
        phase = "mixed"

    return {
        "phase": phase,
        "design_count": counts[DESIGN],
        "implementation_count": counts[IMPLEMENTATION],
        "total": total,
        "per_node": per_node,
    }


def filter_tree_to_phase(tree: list[dict[str, Any]], phase: str,
                         per_node: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Keep nodes in `phase`, retaining ancestors so the tree stays navigable.

    Nodes whose own phase differs are kept only when they have a descendant in
    the requested phase (as context), but are marked `phase_context=True`.
    """
    if phase not in (DESIGN, IMPLEMENTATION):
        return tree
    per_node = per_node or {}

    def walk(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for node in nodes:
            kids = walk(node.get("children") or [])
            own = per_node.get(node.get("id")) or node_phase(node)
            if own == phase or kids:
                item = {**node, "children": kids, "phase_context": own != phase, "phase": own}
                out.append(item)
        return out

    return walk(tree)
