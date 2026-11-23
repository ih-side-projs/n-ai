"""Contract/environment change requests and local-only escalation packet handling (BLD-8, ESC-1..4)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .stubs import node_stub_rel_path


@dataclass(frozen=True)
class EscalationPacket:
    project_id: str
    node_id: str
    node_name: str
    markdown: str
    included_files: list[str]


def raise_change_request(
    store: Any,
    project: dict[str, Any],
    node: dict[str, Any],
    reason: str,
    *,
    missing_package: str | None = None,
    details: dict[str, Any] | None = None,
) -> str:
    """Create a contract/environment change request, return parent to draft, mark dependents stale (BLD-8, TENV-1)."""
    project_id = project["id"]
    node_id = node["id"]
    parent_id = node.get("parent_id")

    payload = {
        "node_id": node_id,
        "node_name": node["name"],
        "parent_id": parent_id,
        "reason": reason,
        "missing_package": missing_package,
        "details": details or {},
    }
    md = (
        f"# Change Request for `{node['name']}` ({node_id})\n\n"
        f"- **Reason:** {reason}\n"
        + (f"- **Requested Package (unpinned):** `{missing_package}`\n" if missing_package else "")
        + f"\n## Details\n```json\n{json.dumps(payload, indent=2, sort_keys=True)}\n```\n"
    )
    art_id = store.add_artifact(
        project_id,
        "report",
        md,
        node_id=node_id,
        filename=f"change_request_{node['name']}.md",
    )
    store.record_event(
        project_id,
        "build.contract_change_requested",
        f"Change request from {node['name']}: {reason}",
        node_id=node_id,
        level="warning",
        data={"artifact_id": art_id, **payload},
    )

    # Mark requesting node stale (note: WP2 STO-4 allows any -> stale)
    if node.get("status") != "stale":
        store.transition_node_status(
            node_id,
            "stale",
            note=f"Blocked by contract/env change request: {reason}",
            data={"change_request_artifact_id": art_id},
        )

    # Return parent node to draft via STO-4 valid edge (any -> stale -> draft)
    if parent_id:
        parent = store.get_node(parent_id)
        if parent is not None:
            if parent["status"] != "stale":
                store.transition_node_status(
                    parent_id,
                    "stale",
                    note=f"Child {node['name']} raised contract change request",
                )
            store.transition_node_status(
                parent_id,
                "draft",
                note="Returned to draft for owner approval of contract change request",
            )
            # Mark sibling dependents stale
            for sib in store.list_nodes(project_id, parent_id=parent_id):
                if sib["id"] != node_id and node_id in (sib.get("depends_on") or []):
                    if sib["status"] != "stale":
                        store.transition_node_status(
                            sib["id"],
                            "stale",
                            note=f"Dependency {node['name']} raised change request",
                        )

    return art_id


def resolve_escalation(
    store: Any,
    node_id: str,
    action: str = "retry",
    *,
    note: str = "",
) -> None:
    """Owner resolution of an escalated node (ESC-2, BLD-9)."""
    node = store.get_node(node_id)
    if node is None:
        raise ValueError(f"Unknown node: {node_id}")
    if node["status"] != "escalated":
        raise ValueError(f"Node {node_id} is {node['status']!r}, expected 'escalated'")

    project_id = node["project_id"]
    if action == "reauthor_tests":
        store.add_approval(node_id, "escalation", "approved", note=note or "Approve test re-authoring")
        store.record_event(
            project_id,
            "build.test_reauthor_approved",
            f"Owner approved test re-authoring for {node['name']}",
            node_id=node_id,
            data={"note": note},
        )
    else:
        store.add_approval(node_id, "escalation", "approved", note=note or f"Resolved escalation: {action}")
        store.record_event(
            project_id,
            "build.escalation_resolved",
            f"Owner resolved escalation for {node['name']} ({action})",
            node_id=node_id,
            data={"action": action, "note": note},
        )

    store.transition_node_status(node_id, "ready", note=note or f"Escalation resolved ({action})")


def build_escalation_packet(
    store: Any,
    project_id: str,
    node_id: str,
    workspace_root: Path | None = None,
) -> EscalationPacket:
    """Build a local-only markdown escalation packet on explicit owner request (ESC-3, ESC-4).

    Never transmits any data over the network.
    """
    project = store.get_project(project_id)
    node = store.get_node(node_id)
    if project is None or node is None:
        raise ValueError("Project or node not found")

    parent_contract = None
    if node.get("parent_id"):
        parent = store.get_node(node["parent_id"])
        if parent:
            parent_contract = parent.get("contract")

    contract_data = parent_contract or node.get("contract") or {}
    ws_root = workspace_root or (Path(project["workspace_path"]) if project.get("workspace_path") else None)

    included_files: list[str] = []
    file_sections: list[str] = []

    if ws_root is not None:
        stub_rel = node_stub_rel_path(project, node)
        candidate_paths = [stub_rel]
        for tr in store.list_test_records(node_id):
            if tr.get("file_path"):
                candidate_paths.append(tr["file_path"])
        for rel in dict.fromkeys(candidate_paths):
            fp = ws_root / rel
            if fp.is_file():
                included_files.append(rel)
                file_sections.append(
                    f"### `{rel}`\n```python\n{fp.read_text(encoding='utf-8')}\n```"
                )

    jobs = [j for j in store.list_jobs(project_id, limit=100000) if j.get("node_id") == node_id]
    latest_failure = ""
    latest_diag = ""
    for j in reversed(jobs):
        if not latest_failure and j.get("kind") == "verify" and j.get("status") == "failed":
            ref = j.get("result_ref") or {}
            latest_failure = str(ref.get("output") or j.get("error") or "")
        if not latest_diag and j.get("kind") == "diagnose" and j.get("status") == "succeeded":
            ref = j.get("result_ref") or {}
            latest_diag = json.dumps(ref, indent=2, sort_keys=True)
        if latest_failure and latest_diag:
            break

    sections = [
        f"# Escalation Packet: {project['name']} / {node['name']}",
        f"- **Node ID:** `{node_id}`\n- **Kind:** `{node['kind']}`\n- **Status:** `{node['status']}`",
        f"## Specification\n{node.get('spec_text') or '(empty)'}",
        f"## Contract\n```json\n{json.dumps(contract_data, indent=2, sort_keys=True)}\n```",
        f"## Local Diagnosis\n```json\n{latest_diag or '{}'}\n```",
        f"## Failing Output\n```\n{latest_failure or '(none recorded)'}\n```",
        "## Relevant Files\n" + ("\n\n".join(file_sections) if file_sections else "(none)"),
    ]
    return EscalationPacket(
        project_id=project_id,
        node_id=node_id,
        node_name=node["name"],
        markdown="\n\n".join(sections) + "\n",
        included_files=included_files,
    )


def redact_escalation_packet(
    packet: EscalationPacket,
    *,
    remove_patterns: list[str] | None = None,
    remove_substrings: list[str] | None = None,
) -> EscalationPacket:
    """Apply owner-requested redactions for preview before saving locally (ESC-4)."""
    text = packet.markdown
    for sub in remove_substrings or ():
        if sub:
            text = text.replace(sub, "[REDACTED]")
    for pat in remove_patterns or ():
        if pat:
            text = re.sub(pat, "[REDACTED]", text, flags=re.MULTILINE)
    return EscalationPacket(
        project_id=packet.project_id,
        node_id=packet.node_id,
        node_name=packet.node_name,
        markdown=text,
        included_files=packet.included_files,
    )
