"""Bridge between SS-BUILD jobs and SS-TOOLS allowlists/execution (BLD-1, BLD-5, TLS-3, TLS-4)."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AllowedToolsBundle:
    allowlist: tuple[str, ...]
    openai_tools: list[dict[str, Any]]
    tool_runner: Callable[[str, dict[str, Any]], str]
    context_section: str


def resolve_node_allowlist(project: dict[str, Any], node: dict[str, Any]) -> tuple[str, ...]:
    """Determine tool allowlist for a node from project/node settings (TLS-3)."""
    settings = project.get("settings") or {}
    per_node = settings.get("tool_allowlists") or {}
    if node["name"] in per_node:
        return tuple(str(x) for x in per_node[node["name"]])
    if node["kind"] in per_node:
        return tuple(str(x) for x in per_node[node["kind"]])
    default_list = settings.get("default_tools") or ()
    return tuple(str(x) for x in default_list)


def build_tools_bundle(
    store: Any,
    project: dict[str, Any],
    node: dict[str, Any],
    workspace_root: Path | None = None,
) -> AllowedToolsBundle:
    """Load only allowlisted tools and enforce allowlist + ERROR: contract on execution (TLS-3, X3)."""
    allowlist = resolve_node_allowlist(project, node)
    allow_set = set(allowlist)

    tool_defs = []
    try:
        tool_defs = list(store.list_tool_defs() or [])
    except Exception:
        tool_defs = []

    registry_mod = None
    try:
        import nai.tools as nai_tools  # type: ignore

        registry_mod = nai_tools
    except Exception:
        registry_mod = None

    filtered_defs = [td for td in tool_defs if td.get("name") in allow_set]
    openai_tools: list[dict[str, Any]] = []
    families: set[str] = set()
    lines: list[str] = []

    for td in filtered_defs:
        name = td["name"]
        desc = td.get("description", "")
        params = td.get("parameters") or {"type": "object", "properties": {}}
        if td.get("family"):
            families.add(str(td["family"]))
        openai_tools.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": desc,
                    "parameters": params,
                },
            }
        )
        lines.append(f"- `{name}`: {desc} (parameters: {json.dumps(params, sort_keys=True)})")

    # Include any SKILL.md for families in allowlist (TLS-4)
    skill_blocks: list[str] = []
    if families and registry_mod is not None and hasattr(registry_mod, "get_family_skill"):
        for fam in sorted(families):
            try:
                skill_md = registry_mod.get_family_skill(fam)
                if skill_md:
                    skill_blocks.append(f"### Tool Family Skill: {fam}\n{skill_md.strip()}")
            except Exception:
                pass

    if not allowlist:
        ctx_section = "## Allowed Tools\n- (no external library tools allowlisted for this job)"
    else:
        body = "\n".join(lines) if lines else "\n".join(f"- `{n}`" for n in allowlist)
        ctx_section = "## Allowed Tools\n" + body
        if skill_blocks:
            ctx_section += "\n\n" + "\n\n".join(skill_blocks)

    def _run_tool(name: str, args: dict[str, Any]) -> str:
        if name not in allow_set:
            return f"ERROR: tool not allowed for this job: {name}"
        if registry_mod is None or not hasattr(registry_mod, "call_tool"):
            return f"ERROR: tool runtime unavailable for: {name}"
        try:
            res = registry_mod.call_tool(
                name,
                args or {},
                workspace_root=str(workspace_root) if workspace_root else None,
            )
            return str(res)
        except Exception as exc:
            return f"ERROR: tool {name} raised {type(exc).__name__}: {exc}"

    return AllowedToolsBundle(
        allowlist=allowlist,
        openai_tools=openai_tools,
        tool_runner=_run_tool,
        context_section=ctx_section,
    )
