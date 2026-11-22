"""Mechanical interface checks (SPEC-3) and spec lint (SPEC-4)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Finding:
    file: str
    line: int
    severity: str
    code: str
    message: str
    def __str__(self) -> str:
        return f"{self.file}:{self.line}: {self.severity.upper()}: [{self.code}] {self.message}"

def _step_caller(step: dict[str, Any]) -> str:
    """SPEC-3: caller is an explicit, required field."""
    return str(step.get("caller") or "")


def _step_callee(step: dict[str, Any]) -> str:
    return str(step.get("callee") or "")


def _step_call(step: dict[str, Any]) -> str:
    """SPEC-3: call is an explicit, required field (function name, no parentheses)."""
    return str(step.get("call") or "").split("(", 1)[0].strip()


def _step_fields(step: dict[str, Any]) -> list[str]:
    """SPEC-3: fields is an explicit, required array of Entity.field references."""
    return [str(f) for f in (step.get("fields") or [])]


def check_interfaces(contract: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    entities = {e["name"]: e for e in contract.get("entities", [])}
    components = {c["name"]: c for c in contract.get("components", [])}
    scenarios = contract.get("scenarios", [])
    # A field is producible when its entity is an external input or some
    # component function that appears as a scenario callee returns that entity.
    external_names = {e.get("name") for e in contract.get("external_inputs", [])}
    used_callees = {_step_callee(s) for sc in scenarios for s in sc.get("steps", [])}
    producible_entities = set(external_names)
    for name in used_callees:
        comp = components.get(name, {})
        for provided in comp.get("provides", []):
            ret = str(provided.get("returns") or "").strip()
            if ret in entities:
                producible_entities.add(ret)
    for scenario in scenarios:
        for step_idx, step in enumerate(scenario.get("steps", [])):
            actor = _step_caller(step)
            if actor not in components and actor not in ("user", "external"):
                findings.append(Finding("contract.json", 0, "error", "SPEC3_UNKNOWN_ACTOR", f"Scenario {scenario['id']} step {step_idx}: unknown actor '{actor}'"))
            callee = _step_callee(step)
            if callee and callee not in components and callee not in ("user", "external"):
                findings.append(Finding("contract.json", 0, "error", "SPEC3_UNKNOWN_CALLEE", f"Scenario {scenario['id']} step {step_idx}: unknown callee '{callee}'"))
            func_name = _step_call(step)
            if callee in components and func_name:
                prov_names = [p["name"] for p in components[callee].get("provides", [])]
                if func_name not in prov_names:
                    findings.append(Finding("contract.json", 0, "error", "SPEC3_MISSING_CALL", f"Scenario {scenario['id']} step {step_idx}: '{callee}' calls '{func_name}' not in provides"))
            for field_ref in _step_fields(step):
                if field_ref.split(".")[0] not in producible_entities:
                    findings.append(Finding("contract.json", 0, "error", "SPEC3_MISSING_PROVIDER", f"Scenario {scenario['id']} step {step_idx}: field '{field_ref}' has no producer"))
    for scenario in scenarios:
        for step_idx, step in enumerate(scenario.get("steps", [])):
            for field in _step_fields(step):
                consumed = False
                for s2 in scenarios:
                    for step2 in s2.get("steps", []):
                        if field in _step_fields(step2):
                            consumed = True
                            break
                    if consumed:
                        break
                if not consumed:
                    is_external = False
                    for ext_out in contract.get("external_outputs", []):
                        if ext_out["name"] == field.split(".")[0]:
                            is_external = True
                            break
                    if not is_external:
                        findings.append(Finding("contract.json", 0, "warning", "SPEC3_UNCONSUMED_FIELD", f"Scenario {scenario['id']} step {step_idx}: produced field '{field}' has no consumer"))
    for scenario in scenarios:
        for step_idx, step in enumerate(scenario.get("steps", [])):
            for field in _step_fields(step):
                entity_name = field.split(".")[0]
                if entity_name not in entities:
                    findings.append(Finding("contract.json", 0, "error", "SPEC3_MISSING_ENTITY", f"Scenario {scenario['id']} step {step_idx}: references unknown entity '{entity_name}'"))
    used_components = set()
    for scenario in scenarios:
        for step in scenario.get("steps", []):
            actor = _step_caller(step)
            if actor in components:
                used_components.add(actor)
            callee = _step_callee(step)
            if callee in components:
                used_components.add(callee)
    for comp_name in components:
        if comp_name not in used_components:
            findings.append(Finding("contract.json", 0, "warning", "SPEC3_UNUSED_COMPONENT", f"Component '{comp_name}' does not appear in any scenario"))
    dep_graph = {name: set(c.get("consumes", [])) for name, c in components.items()}
    if _has_cycle(dep_graph):
        findings.append(Finding("contract.json", 0, "error", "SPEC3_DEPENDENCY_CYCLE", "Dependency cycle detected among components"))
    for name in components:
        if not re.match(r"^[a-z][a-z0-9-]*$", name):
            findings.append(Finding("contract.json", 0, "error", "SPEC3_INVALID_COMPONENT_NAME", f"Component name '{name}' must be lowercase-hyphenated"))
    for name in entities:
        if not re.match(r"^[A-Z][a-zA-Z0-9]*$", name):
            findings.append(Finding("contract.json", 0, "error", "SPEC3_INVALID_ENTITY_NAME", f"Entity name '{name}' must be PascalCase"))
    return findings

def _has_cycle(graph: dict[str, set[str]]) -> bool:
    visited = set()
    rec_stack = set()
    def dfs(node: str) -> bool:
        visited.add(node)
        rec_stack.add(node)
        for neighbor in graph.get(node, []):
            if neighbor not in visited:
                if dfs(neighbor):
                    return True
            elif neighbor in rec_stack:
                return True
        rec_stack.remove(node)
        return False
    return any(dfs(n) for n in graph if n not in visited)


# ---------------------------------------------------------------------------
# SPEC-4: Spec Lint
# ---------------------------------------------------------------------------

VAGUE_WORDS = [
    "fast", "robust", "handles errors", "efficient", "scalable",
    "secure", "user-friendly", "intuitive", "seamless", "performant",
    "optimized", "minimal", "comprehensive", "flexible", "extensible"
]

MAX_REQUIREMENTS = 20
MAX_FUNCTIONS = 15


def lint_spec(spec_text: str, contract: dict[str, Any] | None = None, filepath: str = "spec.md") -> list[Finding]:
    """Lint a component spec markdown (SPEC-4)."""
    findings: list[Finding] = []
    lines = spec_text.splitlines()

    sections_found: set[str] = set()
    current_section = ""
    req_ids: set[str] = set()
    edge_ids: set[str] = set()
    in_example = False
    example_lines: list[str] = []
    dependencies: list[str] = []

    for i, line in enumerate(lines, 1):
        stripped = line.strip()

        if stripped.startswith("## "):
            section_name = stripped[3:].strip().lower()
            sections_found.add(section_name)
            current_section = section_name
            in_example = (section_name == "example")
            continue

        if stripped.startswith("- R") and ":" in stripped:
            match = re.match(r"-\s*(R\d+):\s*(.+)", stripped)
            if match:
                req_id, req_text = match.groups()
                if req_id in req_ids:
                    findings.append(Finding(filepath, i, "error", "SPEC4_DUPLICATE_REQ_ID", f"Duplicate requirement ID: {req_id}"))
                req_ids.add(req_id)
                for vague in VAGUE_WORDS:
                    if vague.lower() in req_text.lower():
                        findings.append(Finding(filepath, i, "warning", "SPEC4_VAGUE_WORD", f"Requirement {req_id} contains vague word: '{vague}'"))

        if stripped.startswith("- E") and ":" in stripped:
            match = re.match(r"-\s*(E\d+):\s*(.+)", stripped)
            if match:
                edge_id, _ = match.groups()
                if edge_id in edge_ids:
                    findings.append(Finding(filepath, i, "error", "SPEC4_DUPLICATE_EDGE_ID", f"Duplicate edge case ID: {edge_id}"))
                edge_ids.add(edge_id)

        if in_example and stripped and not stripped.startswith("#"):
            example_lines.append(stripped)

        if current_section == "dependencies" and stripped.startswith("- "):
            dep = stripped[2:].strip()
            dependencies.append(dep)

    required_sections = {"purpose", "interface", "requirements", "edge cases", "example", "dependencies"}
    for req in required_sections:
        if req not in sections_found:
            findings.append(Finding(filepath, 1, "error", "SPEC4_MISSING_SECTION", f"Missing required section: {req}"))

    if not req_ids:
        findings.append(Finding(filepath, 1, "error", "SPEC4_NO_REQUIREMENTS", "No requirements found (need at least R1)"))

    if len(req_ids) > MAX_REQUIREMENTS:
        findings.append(Finding(filepath, 1, "warning", "SPEC4_TOO_MANY_REQUIREMENTS", f"Too many requirements ({len(req_ids)} > {MAX_REQUIREMENTS})"))

    if not example_lines:
        findings.append(Finding(filepath, 1, "error", "SPEC4_NO_EXAMPLE", "Example section is empty"))

    if contract:
        component_name = _extract_component_name(spec_text)
        all_component_names = {c["name"] for c in contract.get("components", [])}
        if component_name:
            comp_contract = None
            for c in contract.get("components", []):
                if c["name"] == component_name:
                    comp_contract = c
                    break
            if comp_contract:
                contract_deps = set(comp_contract.get("consumes", []))
                for dep in dependencies:
                    if dep not in contract_deps:
                        findings.append(Finding(filepath, 1, "error", "SPEC4_UNDECLARED_DEPENDENCY", f"Dependency '{dep}' not in contract consumes"))
                for dep in contract_deps:
                    if dep not in dependencies:
                        findings.append(Finding(filepath, 1, "warning", "SPEC4_MISSING_DEPENDENCY_IN_SPEC", f"Contract dependency '{dep}' not listed in spec"))
            else:
                # Component not in contract: still validate that declared deps exist.
                for dep in dependencies:
                    if dep not in ("none", "") and dep not in all_component_names:
                        findings.append(Finding(filepath, 1, "error", "SPEC4_UNDECLARED_DEPENDENCY", f"Dependency '{dep}' is not a known component"))

    if contract:
        component_name = _extract_component_name(spec_text)
        if component_name:
            for c in contract.get("components", []):
                if c["name"] == component_name:
                    func_count = len(c.get("provides", []))
                    if func_count > MAX_FUNCTIONS:
                        findings.append(Finding(filepath, 1, "warning", "SPEC4_TOO_MANY_FUNCTIONS", f"Too many functions in contract ({func_count} > {MAX_FUNCTIONS})"))

    return findings


def _extract_component_name(spec_text: str) -> str | None:
    for line in spec_text.splitlines():
        if line.startswith("# Component:"):
            return line.split(":", 1)[1].strip()
    return None


def lint_spec_file(path: Path, contract: dict[str, Any] | None = None) -> list[Finding]:
    text = path.read_text(encoding="utf-8")
    return lint_spec(text, contract, str(path))