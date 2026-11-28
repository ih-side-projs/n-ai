"""WP3 plugin entry point (`register`) and deterministic build planner (`plan`)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .jobs import (
    handle_author_tests,
    handle_contract_stub,
    handle_diagnose,
    handle_implement,
    handle_integrate,
    handle_review,
    handle_verify,
)


def _all_project_nodes(store: Any, project_id: str) -> list[dict[str, Any]]:
    """Breadth-first traversal across the project tree using Store.list_nodes."""
    collected: list[dict[str, Any]] = []
    queue = list(store.list_nodes(project_id, parent_id=None) or [])
    seen: set[str] = set()
    while queue:
        node = queue.pop(0)
        nid = node["id"]
        if nid in seen:
            continue
        seen.add(nid)
        collected.append(node)
        queue.extend(store.list_nodes(project_id, parent_id=nid) or [])
    return collected


def _can_fast_verify(store: Any, node: dict[str, Any], ws_path: Path | None) -> bool:
    """True when BLD-10's fast-verify conditions are actually satisfied."""
    if not node.get("built_hash") or node["built_hash"] != node.get("content_hash"):
        return False
    records = list(store.list_test_records(node["id"]) or [])
    if not records or ws_path is None:
        return False
    return all((ws_path / record["file_path"]).is_file() for record in records)


def _deps_done(node: dict[str, Any], node_map: dict[str, dict[str, Any]]) -> bool:
    for dep_id in node.get("depends_on") or ():
        dep = node_map.get(dep_id)
        if dep is None or dep.get("status") != "done":
            return False
    return True


def _parent_contract_approved(store: Any, node: dict[str, Any], node_map: dict[str, dict[str, Any]]) -> bool:
    pid = node.get("parent_id")
    if not pid:
        return True
    parent = node_map.get(pid)
    if parent is None:
        return False
    if parent.get("status") in ("approved", "ready", "running", "verifying", "done"):
        return True
    approvals = store.list_approvals(pid) or []
    return any(
        a.get("stage") in ("interfaces", "component-spec") and a.get("decision") == "approved"
        for a in approvals
    )


def _node_jobs(all_jobs: list[dict[str, Any]], node_id: str) -> list[dict[str, Any]]:
    return [j for j in all_jobs if j.get("node_id") == node_id]


def _latest_reset_index(node_jobs: list[dict[str, Any]], events: list[dict[str, Any]]) -> int:
    """Count jobs after the last escalation resolution / test-reauthor approval."""
    # Because jobs are ordered oldest->newest (or newest->oldest depending on store), sort by created/id order:
    # We treat list_jobs order if started_at/id increases; check last escalation approval event time if present.
    last_reset_time = ""
    for ev in events:
        if ev.get("type") in ("build.escalation_resolved", "build.test_reauthor_approved"):
            t = str(ev.get("time") or "")
            if t > last_reset_time:
                last_reset_time = t
    if not last_reset_time:
        return 0
    for idx, j in enumerate(node_jobs):
        jt = str(j.get("started_at") or j.get("ended_at") or "")
        if jt >= last_reset_time:
            return idx
    return len(node_jobs)


def _has_succeeded_for_hash(
    node_jobs: list[dict[str, Any]],
    kind: str,
    content_hash: str | None,
) -> bool:
    for j in reversed(node_jobs):
        if j.get("kind") == kind and j.get("status") == "succeeded":
            ref = j.get("result_ref") or {}
            if content_hash is None or ref.get("content_hash") == content_hash:
                return True
    return False


def _ensure_integration_nodes(
    store: Any,
    project: dict[str, Any],
    nodes: list[dict[str, Any]],
    node_map: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Create or activate bottom-up integration nodes once all non-integration children are done (BLD-7)."""
    by_parent: dict[str | None, list[dict[str, Any]]] = {}
    for n in nodes:
        by_parent.setdefault(n.get("parent_id"), []).append(n)

    created = False
    for parent in nodes:
        if parent.get("kind") not in ("system", "subsystem", "component"):
            continue
        children = by_parent.get(parent["id"], [])
        non_int = [c for c in children if c.get("kind") != "integration"]
        if not non_int:
            continue
        if not all(c.get("status") == "done" for c in non_int):
            continue

        int_children = [c for c in children if c.get("kind") == "integration"]
        if not int_children:
            nid = store.create_node(
                project["id"],
                f"{parent['name']}-integration",
                "integration",
                parent_id=parent["id"],
                objective=f"Verify integration scenarios for {parent['name']}",
                spec_text=parent.get("spec_text") or "",
                contract=parent.get("contract"),
                depends_on=tuple(c["id"] for c in non_int),
            )
            # Parent interfaces/specs already approved -> advance integration node to ready per STO-4
            store.transition_node_status(nid, "awaiting_approval", note="Auto-created integration node")
            store.transition_node_status(nid, "approved", note="Inherited parent contract approval")
            store.transition_node_status(nid, "ready", note="All child components done (BLD-7)")
            created = True

    return _all_project_nodes(store, project["id"]) if created else nodes


def plan(pc: Any) -> None:
    """Deterministic build planner invoked by WP3 scheduler (SCH-4, BLD-1..12)."""
    store = pc.store
    project = pc.project
    project_id = project["id"]
    settings = project.get("settings") or {}
    max_coder = int(settings.get("max_attempts_coder", 3))
    max_diagnosed = int(settings.get("max_attempts_diagnosed", 2))
    enable_review = bool(settings.get("enable_review", False))

    nodes = _all_project_nodes(store, project_id)
    node_map = {n["id"]: n for n in nodes}

    # Ensure bottom-up integration nodes exist when all children of a parent are done (BLD-7)
    nodes = _ensure_integration_nodes(store, project, nodes, node_map)
    node_map = {n["id"]: n for n in nodes}

    all_jobs = list(store.list_jobs(project_id, limit=100000) or [])
    # Ensure chronological oldest -> newest order if store returns newest first
    if len(all_jobs) >= 2 and str(all_jobs[0].get("started_at") or "") > str(all_jobs[-1].get("started_at") or ""):
        all_jobs = list(reversed(all_jobs))
    ws_path = Path(project["workspace_path"]) if project.get("workspace_path") else None

    for node in nodes:
        nid = node["id"]
        status = node.get("status")
        kind = node.get("kind")

        # 1. SCH-4 & BLD-10: Advance approved or stale nodes to ready when deps & contract allow
        if status == "approved":
            if _deps_done(node, node_map) and _parent_contract_approved(store, node, node_map):
                store.transition_node_status(nid, "ready", note="Dependencies and parent contract ready")
                status = "ready"

        elif status == "stale":
            if (_deps_done(node, node_map)
                    and _parent_contract_approved(store, node, node_map)
                    and _can_fast_verify(store, node, ws_path)):
                # BLD-10 applies only when the built content is unchanged and
                # frozen tests still exist. Otherwise stale work stays stale
                # until design/owner action re-enters the pipeline.
                store.transition_node_status(nid, "ready", note="Re-evaluating stale node (BLD-10)")
                status = "ready"

        # Skip nodes that already have active (queued/running) jobs
        if pc.active_jobs(nid):
            continue

        node_jobs = _node_jobs(all_jobs, nid)
        events = list(store.list_events(project_id, node_id=nid, limit=200) or [])

        # Check if node was escalated and needs its escalated large-budget diagnose pass (BLD-9 step 3)
        if status == "escalated":
            has_escalated_diag = any(
                j.get("kind") == "diagnose" and (j.get("input_ref") or {}).get("escalated_pass")
                for j in node_jobs
            )
            if not has_escalated_diag:
                last_fail = next(
                    (
                        (j.get("result_ref") or {}).get("output") or j.get("error") or ""
                        for j in reversed(node_jobs)
                        if j.get("status") == "failed"
                    ),
                    "",
                )
                store.create_job(
                    project_id,
                    "diagnose",
                    "reasoner",
                    node_id=nid,
                    input_ref={"escalated_pass": True, "failure_output": last_fail},
                )
            continue

        # 2. Failure ladder (BLD-9) for failed nodes
        if status == "failed":
            # Do not auto-retry nodes that failed due to context pack exceeding size cap (BLD-1)
            last_failed_job = next((j for j in reversed(node_jobs) if j.get("status") == "failed"), None)
            if last_failed_job and (last_failed_job.get("result_ref") or {}).get("needs_decomposition"):
                store.transition_node_status(nid, "escalated", note="Context pack exceeded budget; requires decomposition")
                continue

            slice_start = _latest_reset_index(node_jobs, events)
            recent_jobs = node_jobs[slice_start:]
            impl_count = sum(1 for j in recent_jobs if j.get("kind") == "implement")
            diag_jobs = [
                j
                for j in recent_jobs
                if j.get("kind") == "diagnose" and not (j.get("input_ref") or {}).get("escalated_pass")
            ]

            last_verify_err = ""
            for j in reversed(recent_jobs):
                if j.get("kind") == "verify" and j.get("status") == "failed":
                    last_verify_err = str((j.get("result_ref") or {}).get("output") or j.get("error") or "")
                    break

            if impl_count < max_coder:
                store.transition_node_status(nid, "ready", note=f"Retry implement ({impl_count + 1}/{max_coder})")
                store.create_job(
                    project_id,
                    "implement",
                    "coder",
                    node_id=nid,
                    attempt=impl_count + 1,
                    input_ref={"retry_feedback": last_verify_err},
                )
            elif not diag_jobs:
                store.transition_node_status(nid, "ready", note="Running reasoner diagnosis (BLD-9)")
                store.create_job(
                    project_id,
                    "diagnose",
                    "reasoner",
                    node_id=nid,
                    input_ref={"failure_output": last_verify_err},
                )
            elif impl_count < max_coder + max_diagnosed:
                latest_diag = diag_jobs[-1].get("result_ref") or {}
                if latest_diag.get("fault_kind") in ("test", "spec", "contract", "environment"):
                    store.transition_node_status(nid, "escalated", note=f"Diagnose identified {latest_diag.get('fault_kind')} fault")
                else:
                    store.transition_node_status(
                        nid,
                        "ready",
                        note=f"Diagnosed retry ({impl_count - max_coder + 1}/{max_diagnosed})",
                    )
                    store.create_job(
                        project_id,
                        "implement",
                        "coder",
                        node_id=nid,
                        attempt=impl_count + 1,
                        input_ref={
                            "retry_feedback": last_verify_err,
                            "revised_brief": latest_diag.get("revised_brief", ""),
                        },
                    )
            else:
                store.transition_node_status(nid, "escalated", note="Failure ladder exhausted (BLD-9)")
            continue

        # A component with no children is a leaf for build purposes. Decompose
        # currently emits leaf nodes with kind="component".
        effective_kind = "leaf" if kind == "component" and not store.list_nodes(project_id, parent_id=nid) else kind

        # 3. Optional review job for done leaf nodes (BLD-11)
        if status == "done" and effective_kind == "leaf" and enable_review:
            if not _has_succeeded_for_hash(node_jobs, "review", None):
                store.create_job(project_id, "review", "reasoner", node_id=nid)
            continue

        # 4. Normal build progression for ready/running leaf or integration nodes
        if status not in ("ready", "running"):
            continue
        if effective_kind not in ("leaf", "integration"):
            continue

        chash = node.get("content_hash")
        # BLD-10: fast-path if built_hash matches content_hash and frozen test exists on disk
        if _can_fast_verify(store, node, ws_path):
            store.create_job(project_id, "verify", "coder", node_id=nid)
            continue

        if effective_kind == "leaf":
            if not _has_succeeded_for_hash(node_jobs, "contract_stub", chash):
                store.create_job(project_id, "contract_stub", "coder", node_id=nid)
                continue

            test_records = list(store.list_test_records(nid) or [])
            reauthor_approved = any(e.get("type") == "build.test_reauthor_approved" for e in events)
            has_authored_tests = _has_succeeded_for_hash(node_jobs, "author_tests", chash)
            if not test_records or (reauthor_approved and not has_authored_tests):
                store.create_job(project_id, "author_tests", "reasoner", node_id=nid)
                continue

            # A later lint/review job can be the last job even after a
            # successful implementation. Look for any successful implement for
            # this exact content hash instead of inspecting only the last row.
            if _has_succeeded_for_hash(node_jobs, "implement", chash):
                store.create_job(project_id, "verify", "coder", node_id=nid)
            else:
                store.create_job(project_id, "implement", "coder", node_id=nid, attempt=1)

        elif effective_kind == "integration":
            test_records = list(store.list_test_records(nid) or [])
            if not test_records:
                store.create_job(project_id, "author_tests", "reasoner", node_id=nid)
                continue
            last_job = node_jobs[-1] if node_jobs else None
            if last_job and last_job.get("kind") == "integrate" and last_job.get("status") == "succeeded":
                store.create_job(project_id, "verify", "coder", node_id=nid)
            else:
                store.create_job(project_id, "integrate", "coder", node_id=nid)


def register(runner: Any) -> None:
    """WP3 plugin entry point registering all SS-BUILD job handlers and planner."""
    runner.registry.register("contract_stub", handle_contract_stub, uses_model=False)
    runner.registry.register("author_tests", handle_author_tests, uses_model=True)
    runner.registry.register("implement", handle_implement, uses_model=True)
    runner.registry.register("verify", handle_verify, uses_model=False)
    runner.registry.register("integrate", handle_integrate, uses_model=True)
    runner.registry.register("diagnose", handle_diagnose, uses_model=True)
    runner.registry.register("review", handle_review, uses_model=True)
    runner.register_planner(plan)
