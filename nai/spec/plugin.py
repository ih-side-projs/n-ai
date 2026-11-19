"""WP3 plugin entry point for SS-SPEC.

Registers all spec pipeline job handlers and the spec planner.
"""

from __future__ import annotations

from typing import Any

from .jobs import (
    handle_clarify,
    handle_component_spec,
    handle_check_interfaces,
    handle_decompose,
    handle_interfaces,
    handle_lint,
    handle_reconcile,
    handle_select_map,
)


def register(runner: Any) -> None:
    """WP3 plugin entry point registering all SS-SPEC job handlers and planner."""
    # Register job handlers (all use reasoner model)
    runner.registry.register("decompose", handle_decompose, uses_model=True)
    runner.registry.register("select_map", handle_select_map, uses_model=True)
    runner.registry.register("interfaces", handle_interfaces, uses_model=True)
    runner.registry.register("check_interfaces", handle_check_interfaces, uses_model=True)
    runner.registry.register("component_spec", handle_component_spec, uses_model=True)
    runner.registry.register("lint", handle_lint, uses_model=False)  # code only
    runner.registry.register("reconcile", handle_reconcile, uses_model=True)
    runner.registry.register("clarify", handle_clarify, uses_model=True)

    # Register planner
    runner.register_planner(plan)


def plan(pc: Any) -> None:
    """Spec pipeline planner.

    For each active unpaused project, examines nodes and creates appropriate
    spec pipeline jobs based on their status and progress.
    """
    for project in pc.active_projects():
        pid = project["id"]
        nodes = pc.nodes_with_status(pid, ["draft", "awaiting_approval", "approved", "ready", "running", "verifying", "failed", "stale"])

        for node in nodes:
            # Skip if node already has active jobs
            if pc.active_jobs(node["id"]):
                continue

            status = node.get("status")
            kind = node.get("kind")

            # Root node (system) starts with clarify or decompose
            if node.get("parent_id") is None:
                if status == "draft":
                    # Check if brief needs clarification
                    if not project.get("settings", {}).get("assumptions"):
                        pc.store.create_job(pid, "clarify", "reasoner", node_id=node["id"])
                    else:
                        pc.store.create_job(pid, "decompose", "reasoner", node_id=node["id"])
                continue

            # Child nodes: spec pipeline stages
            if kind in ("subsystem", "component"):
                _plan_node_jobs(pc, node)


def _plan_node_jobs(pc: Any, node: dict[str, Any]) -> None:
    """Plan jobs for a single node based on its status."""
    pid = pc.project["id"]
    nid = node["id"]
    status = node.get("status")
    contract = node.get("contract")
    spec_text = node.get("spec_text")

    # Stage progression:
    # draft -> (decompose) -> awaiting_approval -> approved -> (interfaces) -> ready
    # ready -> (check_interfaces) -> verifying -> (lint) -> ready
    # ready -> (component_spec for children) -> awaiting_approval -> approved -> ready

    if status == "draft":
        if node.get("parent_id"):  # child node
            # Parent must have approved contract first
            parent = pc.store.get_node(node["parent_id"])
            if parent and parent.get("status") in ("approved", "ready", "running", "verifying", "done"):
                pc.store.create_job(pid, "component_spec", "reasoner", node_id=nid)
        else:  # root node
            pc.store.create_job(pid, "decompose", "reasoner", node_id=nid)

    elif status == "awaiting_approval":
        # Waiting for owner approval - no auto job
        pass

    elif status == "approved":
        if not contract:
            # Need to generate interfaces
            pc.store.create_job(pid, "interfaces", "reasoner", node_id=nid)
        elif node.get("parent_id"):  # child node
            # Child spec approved, ready for lint
            pc.store.create_job(pid, "lint", "reasoner", node_id=nid)
        else:  # root node
            # Root interfaces approved, ready for children
            pc.store.create_job(pid, "check_interfaces", "reasoner", node_id=nid)

    elif status == "ready":
        if contract and not node.get("spec_text") and node.get("kind") in ("subsystem", "component"):
            # Need component spec
            pc.store.create_job(pid, "component_spec", "reasoner", node_id=nid)
        elif contract and node.get("spec_text") and node.get("kind") in ("subsystem", "component"):
            # Has spec, need lint
            pc.store.create_job(pid, "lint", "reasoner", node_id=nid)
        elif not node.get("parent_id") and contract:
            # Root with contract, check interfaces
            pc.store.create_job(pid, "check_interfaces", "reasoner", node_id=nid)

    elif status == "running":
        # Job in progress - wait
        pass

    elif status == "verifying":
        # Verification in progress - wait
        pass

    elif status == "failed":
        # Could retry or escalate
        pass

    elif status == "stale":
        # Stale node - check what needs re-running
        if contract and node.get("kind") in ("subsystem", "component"):
            pc.store.create_job(pid, "lint", "reasoner", node_id=nid)