"""Deterministic context pack assembly and context-budget checking (BLD-1, X2)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .stubs import extract_stub_signatures, node_stub_rel_path
from .tenv import TargetEnv, estimate_tokens, format_env_block, load_api_notes

DEFAULT_STYLE_GUIDE = (
    "- Follow PEP 8 and PEP 484 type annotations.\n"
    "- Write clean docstrings, avoid deprecated stdlib/library APIs (ruff UP, B).\n"
    "- Keep functions focused, deterministic where possible, and handle edge cases explicitly."
)


@dataclass(frozen=True)
class ContextPack:
    text: str
    estimated_tokens: int
    max_tokens: int
    fits: bool
    sections: dict[str, str]


def _format_entities_touched(
    node: dict[str, Any],
    parent_contract: dict[str, Any] | None,
) -> str:
    contract = parent_contract or node.get("contract") or {}
    entities = list(contract.get("entities") or [])
    if not entities:
        return "## Shared Entities\n(none)"

    # Filter to entities referenced by this component's provides/consumes or spec_text
    referenced: set[str] = set()
    for comp in contract.get("components") or []:
        if comp.get("name") == node["name"]:
            for prov in comp.get("provides") or []:
                referenced.update(str(x) for x in (prov.get("input_entities") or ()))
                referenced.update(str(x) for x in (prov.get("output_entities") or ()))
            for cons in comp.get("consumes") or []:
                if isinstance(cons, dict):
                    referenced.update(str(x) for x in (cons.get("entities") or ()))

    spec_text = node.get("spec_text") or ""
    selected = [
        e
        for e in entities
        if not referenced or e.get("name") in referenced or str(e.get("name", "")) in spec_text
    ]
    if not selected:
        selected = entities

    return "## Shared Entities\n```json\n" + json.dumps(selected, indent=2, sort_keys=True) + "\n```"


def _format_dependency_stubs(
    store: Any,
    project: dict[str, Any],
    node: dict[str, Any],
    parent_contract: dict[str, Any] | None,
    workspace_root: Path | None,
) -> str:
    dep_ids = list(node.get("depends_on") or [])
    if not dep_ids:
        return "## Direct Dependency Interfaces\n(none)"

    blocks: list[str] = []
    contract = parent_contract or {}
    comp_map = {c.get("name"): c for c in (contract.get("components") or []) if c.get("name")}

    for dep_id in dep_ids:
        dep = store.get_node(dep_id)
        if dep is None:
            continue
        dep_name = dep["name"]
        stub_sig_text = ""
        if workspace_root is not None:
            stub_path = workspace_root / node_stub_rel_path(project, dep)
            if stub_path.is_file():
                # Strictly signatures and docstrings only, NEVER implementations (BLD-1)
                stub_sig_text = extract_stub_signatures(stub_path.read_text(encoding="utf-8"))

        if stub_sig_text:
            blocks.append(f"### Dependency `{dep_name}` (`{node_stub_rel_path(project, dep)}`)\n```python\n{stub_sig_text}\n```")
        elif dep_name in comp_map:
            provides = comp_map[dep_name].get("provides") or []
            blocks.append(
                f"### Dependency `{dep_name}` (from frozen contract)\n```json\n"
                + json.dumps(provides, indent=2, sort_keys=True)
                + "\n```"
            )
        else:
            blocks.append(f"### Dependency `{dep_name}`\nObjective: {dep.get('objective', '')}")

    return "## Direct Dependency Interfaces (Signatures Only)\n\n" + "\n\n".join(blocks)


def assemble_context_pack(
    store: Any,
    project: dict[str, Any],
    node: dict[str, Any],
    tenv: TargetEnv,
    coder_ctx_limit: int,
    *,
    parent_contract: dict[str, Any] | None = None,
    workspace_root: Path | None = None,
    tools_section: str = "",
) -> ContextPack:
    """Assemble deterministic context pack for a build job and enforce context cap (BLD-1)."""
    settings = project.get("settings") or {}
    max_fraction = float(settings.get("context_pack_max_fraction", 0.5))
    max_tokens = max(1, int(coder_ctx_limit * max_fraction))

    env_block = format_env_block(tenv)
    api_notes = (
        load_api_notes(workspace_root, tenv, node.get("spec_text") or "", parent_contract)
        if workspace_root is not None
        else ""
    )
    spec_block = (
        f"## Component Specification: `{node['name']}` ({node['kind']})\n"
        f"Objective: {node.get('objective') or ''}\n\n"
        f"{node.get('spec_text') or ''}"
    ).strip()
    entities_block = _format_entities_touched(node, parent_contract)
    deps_block = _format_dependency_stubs(store, project, node, parent_contract, workspace_root)
    style_guide = "## Style Guide\n" + str(settings.get("style_guide") or DEFAULT_STYLE_GUIDE)
    tools_block = tools_section.strip() or "## Allowed Tools\n- (none)"

    ordered_sections = {
        "environment": env_block,
        "api_notes": api_notes,
        "spec": spec_block,
        "entities": entities_block,
        "dependencies": deps_block,
        "style_guide": style_guide,
        "tools": tools_block,
    }
    full_text = "\n\n".join(part for part in ordered_sections.values() if part)
    tokens = estimate_tokens(full_text)

    return ContextPack(
        text=full_text,
        estimated_tokens=tokens,
        max_tokens=max_tokens,
        fits=(tokens <= max_tokens),
        sections=ordered_sections,
    )
