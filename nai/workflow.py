"""Named workflow phases derived from existing state.

``info_gathering`` is the first, project-level phase for structured intake;
``design`` generates and approves the spec; ``implementation`` codes, tests and
integrates. Design and implementation may overlap at the project level.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from typing import Any

INFO_GATHERING = "info_gathering"
DESIGN = "design"
IMPLEMENTATION = "implementation"

# Job kinds belonging to each phase (matches the spec pipeline / build pipeline).
DESIGN_JOBS = frozenset({
    "intake", "clarify", "decompose", "select_map", "interfaces", "check_interfaces",
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
            """Key."""
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


def is_info_gathering(project: dict[str, Any] | None) -> bool:
    """True while a structured-intake project has not entered design yet."""
    settings = (project or {}).get("settings") or {}
    return bool(settings.get("intake")) and not bool(settings.get("intake_complete"))


def project_phase_summary(nodes: Iterable[dict[str, Any]],
                          jobs_by_node: dict[str, list[dict[str, Any]]] | None,
                          project: dict[str, Any] | None = None) -> dict[str, Any]:
    """Project phase summary including the project-level Info Gathering phase."""
    summary = phase_summary(nodes, jobs_by_node)
    if is_info_gathering(project):
        return {**summary, "phase": INFO_GATHERING, "phase_label": "Info Gathering", "info_gathering": True}
    label = "Design" if summary["phase"] == DESIGN else (
        "Implementation" if summary["phase"] == IMPLEMENTATION else summary["phase"].capitalize()
    )
    return {**summary, "phase_label": label, "info_gathering": False}


def next_step(project: dict[str, Any], nodes: Iterable[dict[str, Any]],
              jobs: Iterable[dict[str, Any]] | None = None) -> dict[str, str]:
    """Return the single most useful next action for the project overview."""
    pid = str(project.get("id") or "")
    all_nodes = list(nodes)
    all_jobs = list(jobs or [])
    statuses = {str(node.get("status") or "") for node in all_nodes}
    active = [job for job in all_jobs if job.get("status") in ("queued", "running")]

    if is_info_gathering(project):
        return {
            "title": "Finish Info Gathering",
            "body": "Answer the model's questions below, or press “Begin design / spec-ing now” if you already have enough detail.",
            "label": "", "href": "",
        }
    if "awaiting_approval" in statuses:
        return {
            "title": "Review pending approvals",
            "body": "A node is waiting for a design decision. Approve it to continue, or reject it with a note to send it back for revision.",
            "label": "Open approvals", "href": "/approvals",
        }
    if statuses & {"failed", "escalated", "blocked"}:
        return {
            "title": "Resolve blocked work",
            "body": "Inspect the failed/escalated node, retry it, or mark it stale to trigger a fresh design pass.",
            "label": "Open escalations", "href": "/escalations",
        }
    if active:
        return {
            "title": "Watch the current job",
            "body": "The runner is working. Follow the live event stream for progress, tokens, and errors.",
            "label": "Open logs", "href": f"/projects/{pid}/logs",
        }
    if "draft" in statuses:
        return {
            "title": "Design is ready to continue",
            "body": "The refined brief is saved on the root node. Open the Design tab and review the spec tree; a new spec job will appear when the runner polls.",
            "label": "Open Design", "href": f"/projects/{pid}?phase={DESIGN}",
        }
    if statuses & IMPLEMENTATION_STATUSES:
        return {
            "title": "Watch implementation",
            "body": "Nodes are in an implementation state. Check that the relevant interfaces approvals exist, then follow jobs, artifacts, and traceability.",
            "label": "Open logs", "href": f"/projects/{pid}/logs",
        }
    return {
        "title": "Review the spec tree",
        "body": "Inspect nodes, artifacts, and runner state. Use Design or Implementation tabs to focus on one part of the workflow.",
        "label": "Open Design", "href": f"/projects/{pid}?phase={DESIGN}",
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
        """Walk."""
        out = []
        for node in nodes:
            kids = walk(node.get("children") or [])
            own = per_node.get(node.get("id")) or node_phase(node)
            if own == phase or kids:
                item = {**node, "children": kids, "phase_context": own != phase, "phase": own}
                out.append(item)
        return out

    return walk(tree)
