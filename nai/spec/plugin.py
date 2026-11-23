"""Spec pipeline plugin: job handlers + planner using the real PlanContext API."""
from __future__ import annotations

from typing import Any

from .jobs import (
    handle_check_interfaces,
    handle_clarify,
    handle_component_spec,
    handle_decompose,
    handle_intake,
    handle_interfaces,
    handle_lint,
    handle_reconcile,
    handle_select_map,
)

# Round limits for loop-back stages (SPEC-1 / BLD-3)
MAX_REPAIR_ROUNDS = 3


def register(runner: Any) -> None:
    runner.registry.register("intake", handle_intake, uses_model=True)
    runner.registry.register("decompose", handle_decompose, uses_model=True)
    runner.registry.register("select_map", handle_select_map, uses_model=True)
    runner.registry.register("interfaces", handle_interfaces, uses_model=True)
    runner.registry.register("check_interfaces", handle_check_interfaces, uses_model=True)
    runner.registry.register("component_spec", handle_component_spec, uses_model=True)
    runner.registry.register("lint", handle_lint, uses_model=False)
    runner.registry.register("reconcile", handle_reconcile, uses_model=True)
    runner.registry.register("clarify", handle_clarify, uses_model=True)
    runner.register_planner(plan)


def _round(job: dict) -> int:
    return int((job.get("input_ref") or {}).get("round", 0))


def plan(pc: Any) -> None:
    """Plan spec jobs for the ONE project in pc (runner calls once per project)."""
    project = pc.project
    pid = project["id"]
    settings = project.get("settings") or {}

    # Structured intake is a project-level gate. No spec jobs are planned until
    # the owner (or the model, once it has enough information) ends this phase.
    roots = [n for n in pc.nodes if n.get("parent_id") is None]
    if settings.get("intake") is not None and not settings.get("intake_complete"):
        if roots and not settings.get("intake_waiting"):
            if not any(j.get("kind") == "intake" for j in pc.jobs):
                pc.store.create_job(pid, "intake", "reasoner", node_id=roots[0]["id"])
        return

    for node in pc.nodes:
        nid = node["id"]
        if pc.active_jobs(nid):
            continue
        status = node.get("status")
        kind = node.get("kind")
        contract = node.get("contract")
        spec_text = node.get("spec_text")
        parent_id = node.get("parent_id")

        if parent_id is None:
            if status == "draft":
                if not contract:
                    if not settings.get("assumptions") and not settings.get("clarified"):
                        pc.store.create_job(pid, "clarify", "reasoner", node_id=nid)
                    else:
                        pc.store.create_job(pid, "decompose", "reasoner", node_id=nid)
                elif "shared_entities" in contract or "external_inputs" in contract:
                    # Decompose produced the component map; generate the root
                    # interface contract before children are specified.
                    pc.store.create_job(pid, "interfaces", "reasoner", node_id=nid)
            continue

        if kind not in ("subsystem", "component"):
            continue

        if status == "draft":
            parent = pc.store.get_node(parent_id) or {}
            if parent.get("contract"):
                pc.store.create_job(pid, "component_spec", "reasoner", node_id=nid)
        elif status == "approved":
            if not contract:
                pc.store.create_job(pid, "interfaces", "reasoner", node_id=nid)
            elif not spec_text:
                pc.store.create_job(pid, "component_spec", "reasoner", node_id=nid)
            else:
                pc.store.create_job(pid, "lint", "reasoner", node_id=nid,
                                    input_ref={"round": 0})
        elif status == "ready":
            if contract and not spec_text:
                pc.store.create_job(pid, "component_spec", "reasoner", node_id=nid)
            elif contract and spec_text:
                pc.store.create_job(pid, "lint", "reasoner", node_id=nid,
                                    input_ref={"round": 0})
        elif status == "stale":
            if contract and spec_text:
                pc.store.create_job(pid, "lint", "reasoner", node_id=nid,
                                    input_ref={"round": 0})
