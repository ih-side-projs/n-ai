"""SS-SPEC job handlers executed by the WP3 scheduler (SPEC-1..9)."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .checkers import check_interfaces, lint_spec
from .prompts import (
    CHECK_INTERFACES,
    CLARIFY,
    COMPONENT_SPEC,
    DECOMPOSE,
    INTAKE,
    INTERFACES,
    RECONCILE,
    SELECT_MAP,
    get_prompt,
)
from .schemas import (
    CHECK_INTERFACES_SCHEMA,
    CLARIFY_SCHEMA,
    COMPONENT_SPEC_SCHEMA,
    DECOMPOSE_SCHEMA,
    INTAKE_SCHEMA,
    INTERFACES_SCHEMA,
    RECONCILE_SCHEMA,
    SELECT_MAP_SCHEMA,
)


def _make_job_result(ok: bool, **kwargs: Any) -> Any:
    try:
        from nai.sched import JobResult
        return JobResult.success(**kwargs) if ok else JobResult.failure(kwargs.pop("error", "failed"), **kwargs)
    except Exception:
        return {"ok": ok, **kwargs}

def _workspace_root(ctx: Any) -> Path:
    if ctx.ws is not None and getattr(ctx.ws, "root", None) is not None:
        return Path(ctx.ws.root)
    wp = (ctx.project or {}).get("workspace_path")
    if not wp:
        raise RuntimeError("Project has no workspace_path configured")
    return Path(wp)

def _spec_dir(ws_root: Path, node: dict[str, Any]) -> Path:
    if node.get("parent_id") is None:
        return ws_root
    return ws_root / f"SPEC-{node['name']}"

def _read_spec_file(ws_root: Path, node: dict[str, Any]) -> str:
    path = _spec_dir(ws_root, node) / "SPEC.md"
    return path.read_text(encoding="utf-8") if path.is_file() else ""

def _write_spec_file(ws_root: Path, node: dict[str, Any], content: str) -> Path:
    path = _spec_dir(ws_root, node) / "SPEC.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path

def _write_contract(ws_root: Path, node: dict[str, Any], contract: dict[str, Any]) -> Path:
    path = _spec_dir(ws_root, node) / "interfaces.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(contract, indent=2, sort_keys=True), encoding="utf-8")
    return path

def _get_parent_contract(ctx: Any, node: dict[str, Any]) -> dict[str, Any] | None:
    pid = node.get("parent_id")
    if not pid:
        return node.get("contract")
    parent = ctx.store.get_node(pid)
    return parent.get("contract") if parent else None

def _get_siblings(ctx: Any, node: dict[str, Any]) -> list[dict[str, Any]]:
    pid = node.get("parent_id")
    return ctx.store.list_nodes(ctx.project["id"], parent_id=pid) or []

def _call_structured(ctx: Any, prompt_name: str, payload: dict[str, Any], schema: dict[str, Any], tag: str, meta: dict[str, Any] | None = None, max_repairs: int = 3) -> dict[str, Any]:
    from .structured import StructuredOutputError, call_structured_json
    prompt = get_prompt(prompt_name)
    messages = [{"role": "system", "content": prompt.text}, {"role": "user", "content": json.dumps(payload, indent=2)}]
    try:
        return call_structured_json(ctx.client, "reasoner", messages, schema, tag=tag, meta={"job_id": ctx.job["id"], "prompts": [prompt.id], **(meta or {})}, max_repairs=max_repairs)
    except StructuredOutputError as exc:
        raise RuntimeError(f"Structured output failed after {max_repairs} repairs: {exc}") from exc

def _size_limits(ctx: Any) -> dict[str, Any]:
    """X9 size limits: project settings > NAI config > spec defaults.

    D2 candidate counts (3 at root, 1 elsewhere) are separate from the X9
    ``max_components_per_level`` default of 7. Both are read from config.
    """
    try:
        from ..config import load_config
        defaults = load_config().limits.to_dict()
    except Exception:
        defaults = {
            "max_requirements_per_component": 8,
            "max_public_functions_per_component": 10,
            "max_components_per_level": 7,
            "max_depth": 4,
            "candidates_root": 3,
            "candidates_nonroot": 1,
        }
    settings = (getattr(ctx, "project", None) or {}).get("settings") or {}
    overrides = {k: v for k, v in settings.items() if k in defaults}
    return {**defaults, **overrides}


MAX_REPAIR_ROUNDS = 3

def _round(ctx: Any) -> int:
    return int((ctx.job.get("input_ref") or {}).get("round", 0))


def _requeue(ctx: Any, kind: str, rounds: int, **kwargs: Any) -> Any:
    if rounds >= MAX_REPAIR_ROUNDS:
        return _make_job_result(False, error=f"repair round limit ({MAX_REPAIR_ROUNDS}) exceeded", **kwargs)
    ctx.enqueue(kind, "reasoner", input_ref={"round": rounds + 1})
    return _make_job_result(True, requeued=True, **kwargs)


# ---------------------------------------------------------------------------
# Stage 1: Decompose
# ---------------------------------------------------------------------------

def handle_decompose(ctx: Any) -> Any:
    ctx.check()
    node, project = ctx.node, ctx.project
    _workspace_root(ctx)
    brief = node.get("spec_text") or project.get("brief") or ""
    target_stack = project.get("settings", {}).get("target_env", {}).get("language", "Python 3.12")
    limits = _size_limits(ctx)
    # X9: max_components_per_level (default 7), NOT the D2 candidate count.
    # D2 candidate count is used by handle_select_map, passed separately.
    payload = {
        "brief": brief,
        "target_stack": target_stack,
        "max_components_per_level": limits["max_components_per_level"],
        "max_depth": limits["max_depth"],
        "size_limits": limits,
    }
    try:
        result = _call_structured(ctx, DECOMPOSE, payload, DECOMPOSE_SCHEMA, "decompose")
    except RuntimeError as e:
        return _make_job_result(False, error=str(e))
    cm = {"components": result["components"], "shared_entities": result["shared_entities"], "external_inputs": result["external_inputs"], "external_outputs": result["external_outputs"]}
    ctx.store.update_node_contract(node["id"], cm)
    # Create a child node for each component so the spec tree is recursable (stage 11).
    existing = {c["name"] for c in ctx.store.list_nodes(project["id"], parent_id=node["id"])}
    for child in cm["components"]:
        name = str(child.get("name") or "").strip()
        if not name or name in existing:
            continue
        ctx.store.create_node(
            project["id"], name, "component", parent_id=node["id"],
            objective=str(child.get("objective") or ""),
            depends_on=list(child.get("depends_on") or []),
        )
    ctx.artifact("report", json.dumps(cm, indent=2), f"component_map_{node['name']}.json")
    ctx.event("spec.decompose_done", f"Generated component map with {len(cm['components'])} components", level="info", data={"component_count": len(cm["components"])})
    return _make_job_result(True, component_map=cm, prompt_id=get_prompt(DECOMPOSE).id)


# ---------------------------------------------------------------------------
# Stage 2: Select Map
# ---------------------------------------------------------------------------

def handle_select_map(ctx: Any) -> Any:
    ctx.check()
    node, project = ctx.node, ctx.project
    cm = node.get("contract") or {}
    candidates = cm.get("candidates", [])
    if not candidates or len(candidates) < 2:
        return _make_job_result(True, skipped=True, reason="no_candidates")
    payload = {"brief": project.get("brief", ""), "candidates": candidates}
    try:
        result = _call_structured(ctx, SELECT_MAP, payload, SELECT_MAP_SCHEMA, "select_map")
    except RuntimeError as e:
        return _make_job_result(False, error=str(e))
    selected = result["merged_map"]
    ctx.store.update_node_contract(node["id"], selected)
    ctx.artifact("report", json.dumps(selected, indent=2), f"selected_map_{node['name']}.json")
    ctx.event("spec.select_map_done", "Selected/merged component map", level="info", data={"selected_index": result["selected_index"]})
    return _make_job_result(True, selected_map=selected, prompt_id=get_prompt(SELECT_MAP).id)


# ---------------------------------------------------------------------------
# Stage 4: Interfaces
# ---------------------------------------------------------------------------

def handle_interfaces(ctx: Any) -> Any:
    ctx.check()
    node, project = ctx.node, ctx.project
    ws_root = _workspace_root(ctx)
    cm = node.get("contract") or {}
    brief = project.get("brief", "")
    payload = {"brief": brief, "component_map": cm}
    try:
        result = _call_structured(ctx, INTERFACES, payload, INTERFACES_SCHEMA, "interfaces")
    except RuntimeError as e:
        return _make_job_result(False, error=str(e))
    contract = result["contract"]
    ctx.store.update_node_contract(node["id"], contract)
    _write_contract(ws_root, node, contract)
    ctx.artifact("contract", json.dumps(contract, indent=2), f"interfaces_{node['name']}.json")
    ctx.event("spec.interfaces_done", f"Generated interface contract with {len(contract.get('components', []))} components", level="info", data={"component_count": len(contract.get("components", []))})
    return _make_job_result(True, contract=contract, prompt_id=get_prompt(INTERFACES).id)


# ---------------------------------------------------------------------------
# Stage 6: Check Interfaces
# ---------------------------------------------------------------------------

def handle_check_interfaces(ctx: Any) -> Any:
    ctx.check()
    node = ctx.node
    contract = node.get("contract")
    if not contract:
        return _make_job_result(False, error="No contract found for node")
    code_findings = check_interfaces(contract)
    if code_findings:
        gaps = []
        for f in code_findings:
            match = re.search(r"Scenario (\w+) step (\d+)", f.message)
            gaps.append({
                "scenario_id": match.group(1) if match else "",
                "step_index": int(match.group(2)) if match else 0,
                "issue": f.code.replace("SPEC3_", "").lower(),
                "missing_element": f.message.split("'")[1] if "'" in f.message else f.message,
                "suggested_fix": f"Fix {f.code}: {f.message}",
                "severity": f.severity,
            })
        if gaps:
            payload = {"contract": contract, "gaps": gaps}
            try:
                result = _call_structured(ctx, CHECK_INTERFACES, payload, CHECK_INTERFACES_SCHEMA, "check_interfaces")
                if not result.get("all_verified"):
                    ctx.event("spec.check_interfaces_gaps", f"Found {len(gaps)} gaps in contract", level="warning", data={"gaps": gaps})
                    return _requeue(ctx, "check_interfaces", _round(ctx), gaps=gaps, prompt_id=get_prompt(CHECK_INTERFACES).id)
            except RuntimeError as e:
                return _make_job_result(False, error=str(e))
    ctx.event("spec.check_interfaces_passed", "All interface checks passed", level="info")
    return _make_job_result(True, all_verified=True, prompt_id=get_prompt(CHECK_INTERFACES).id)

# ---------------------------------------------------------------------------
# Stage 8: Component Spec
# ---------------------------------------------------------------------------

def handle_component_spec(ctx: Any) -> Any:
    ctx.check()
    node, project = ctx.node, ctx.project
    ws_root = _workspace_root(ctx)
    parent_contract = _get_parent_contract(ctx, node)
    if not parent_contract:
        return _make_job_result(False, error="Parent contract not found")
    component_info = next((c for c in parent_contract.get("components", []) if c["name"] == node["name"]), None)
    if not component_info:
        return _make_job_result(False, error=f"Component {node['name']} not found in parent contract")
    shared_entities = []
    for entity in parent_contract.get("entities", []):
        used = any(entity["name"].lower() in p.get("signature", "").lower() for p in component_info.get("provides", []))
        used = used or (node["name"] in component_info.get("consumes", []))
        if used:
            shared_entities.append(entity)
    payload = {"component_name": node["name"], "objective": node.get("objective", ""), "shared_entities": shared_entities, "provides": component_info.get("provides", []), "consumes": component_info.get("consumes", []), "dependencies": node.get("depends_on", []), "target_stack": project.get("settings", {}).get("target_env", {}).get("language", "Python 3.12")}
    try:
        result = _call_structured(ctx, COMPONENT_SPEC, payload, COMPONENT_SPEC_SCHEMA, "component_spec")
    except RuntimeError as e:
        return _make_job_result(False, error=str(e))
    spec_markdown = result["spec_markdown"]
    _write_spec_file(ws_root, node, spec_markdown)
    # Persist to the store so lint/planning and downstream traceability can read it.
    ctx.store.update_node_spec(node["id"], spec_markdown)
    requirements = [{"id": r["id"], "text": r["text"], "testable": r.get("testable", True)} for r in result["requirements"]]
    ctx.store.replace_requirements(node["id"], requirements)
    ctx.artifact("spec", spec_markdown, f"SPEC-{node['name']}.md")
    ctx.event("spec.component_spec_done", f"Generated spec for {node['name']} with {len(requirements)} requirements", level="info", data={"requirement_count": len(requirements)})
    return _make_job_result(True, spec=spec_markdown, requirements=requirements, prompt_id=get_prompt(COMPONENT_SPEC).id)


# ---------------------------------------------------------------------------
# Stage 9: Lint
# ---------------------------------------------------------------------------

def handle_lint(ctx: Any) -> Any:
    ctx.check()
    node = ctx.node
    ws_root = _workspace_root(ctx)
    spec_text = _read_spec_file(ws_root, node)
    if not spec_text:
        return _make_job_result(False, error="No spec file found")
    parent_contract = _get_parent_contract(ctx, node)
    findings = lint_spec(spec_text, parent_contract, f"SPEC-{node['name']}.md")
    if findings:
        error_count = sum(1 for f in findings if f.severity == "error")
        ctx.event("spec.lint_failed", f"Lint found {error_count} errors, {len(findings) - error_count} warnings", level="warning" if error_count else "info", data={"findings": [str(f) for f in findings]})
        return _requeue(ctx, "lint", _round(ctx), findings=[str(f) for f in findings])
    ctx.event("spec.lint_passed", "Spec lint passed", level="info")
    return _make_job_result(True, findings=[])


# ---------------------------------------------------------------------------
# Stage 10: Reconcile
# ---------------------------------------------------------------------------

def handle_reconcile(ctx: Any) -> Any:
    ctx.check()
    node, _project = ctx.node, ctx.project
    siblings = _get_siblings(ctx, node)
    if len(siblings) <= 1:
        return _make_job_result(True, skipped=True, reason="no_siblings")
    sibling_specs = []
    for sib in siblings:
        if sib["id"] == node["id"]:
            continue
        spec_text = ctx.store.get_node(sib["id"]).get("spec_text", "")
        if spec_text:
            sibling_specs.append({"name": sib["name"], "spec": spec_text})
    if not sibling_specs:
        return _make_job_result(True, skipped=True, reason="no_sibling_specs")
    parent_contract = _get_parent_contract(ctx, node)
    payload = {"current_component": node["name"], "sibling_specs": sibling_specs, "parent_contract": parent_contract}
    try:
        result = _call_structured(ctx, RECONCILE, payload, RECONCILE_SCHEMA, "reconcile")
    except RuntimeError as e:
        return _make_job_result(False, error=str(e))
    findings = result.get("findings", [])
    error_findings = [f for f in findings if f.get("severity") == "error"]
    if error_findings:
        ctx.event("spec.reconcile_errors", f"Reconcile found {len(error_findings)} errors", level="error", data={"findings": findings})
        return _requeue(ctx, "reconcile", _round(ctx), findings=findings)
    ctx.event("spec.reconcile_passed", "Sibling reconciliation passed", level="info", data={"findings": findings})
    return _make_job_result(True, findings=findings, prompt_id=get_prompt(RECONCILE).id)


# ---------------------------------------------------------------------------
# Stage -1: Info Gathering (structured project intake)
# ---------------------------------------------------------------------------

def handle_intake(ctx: Any) -> Any:
    """Act as a product manager: ask focused questions, then hand off to design."""
    ctx.check()
    project = ctx.project
    settings = dict(project.get("settings") or {})
    intake = dict(settings.get("intake") or {})
    forced = bool((ctx.job.get("input_ref") or {}).get("force_refine"))
    payload = {
        "force_design": forced,
        "brief": project.get("brief", ""),
        "intake": intake,
        "answers": intake.get("answers") or {},
        "previous_questions": settings.get("intake_questions") or [],
        "readiness_summary": settings.get("intake_readiness_summary") or "",
    }
    try:
        result = _call_structured(ctx, INTAKE, payload, INTAKE_SCHEMA, "intake")
    except RuntimeError as exc:
        return _make_job_result(False, error=str(exc))

    questions = result.get("questions") or []
    ready = bool(result.get("ready_for_design")) or forced
    if forced:
        questions = []
    if not ready and not questions:
        # Avoid a tight planner loop if a model returns neither readiness nor a
        # follow-up question.
        questions = [{
            "id": "Q1",
            "question": "What else should we know before design begins?",
            "why": "More detail is required to design the solution",
        }]
    settings["intake_questions"] = questions
    settings["intake_readiness_summary"] = result.get("readiness_summary", "")
    settings["intake_refined_brief"] = result.get("refined_brief", "")
    settings["intake_guardrails"] = result.get("guardrails") or {}
    settings["intake_waiting"] = not ready and bool(questions)

    if ready:
        settings["intake_complete"] = True
        settings["clarified"] = True
        refined = str(result.get("refined_brief") or "").strip()
        if refined:
            roots = ctx.store.list_nodes(project["id"], parent_id=None) or []
            if roots:
                ctx.store.update_node_spec(roots[0]["id"], refined)
        ctx.event("spec.intake_ready", "Info gathering complete; design can begin",
                  level="info", data={"summary": settings["intake_readiness_summary"]})
    else:
        ctx.event("spec.intake_questions", f"Info gathering needs {len(questions)} answer(s)",
                  level="info", data={"questions": questions})

    ctx.store.update_project(project["id"], settings=settings)
    return _make_job_result(True, ready_for_design=ready, questions=questions,
                            prompt_id=get_prompt(INTAKE).id)


# ---------------------------------------------------------------------------
# Stage 0: Clarify
# ---------------------------------------------------------------------------

def handle_clarify(ctx: Any) -> Any:
    ctx.check()
    project = ctx.project
    brief = project.get("brief", "")
    target_stack = project.get("settings", {}).get("target_env", {}).get("language", "Python 3.12")
    payload = {"brief": brief, "target_stack": target_stack}
    try:
        result = _call_structured(ctx, CLARIFY, payload, CLARIFY_SCHEMA, "clarify")
    except RuntimeError as e:
        return _make_job_result(False, error=str(e))
    questions = result.get("questions", [])
    # Persist assumptions so the planner does not re-queue clarify forever.
    settings = dict(project.get("settings") or {})
    settings["clarified"] = True
    settings["clarify_questions"] = questions
    settings["assumptions"] = [q.get("default_answer") for q in questions if q.get("default_answer")]
    ctx.store.update_project(project["id"], settings=settings)
    ctx.event("spec.clarify_done", f"Generated {len(questions)} clarification questions", level="info", data={"questions": questions})
    return _make_job_result(True, questions=questions, prompt_id=get_prompt(CLARIFY).id)