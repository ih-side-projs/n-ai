"""Tests for BLD-8 change requests and ESC-1..4 local escalation packets & redaction."""

from pathlib import Path

from nai.build.escalation import (
    build_escalation_packet,
    raise_change_request,
    redact_escalation_packet,
    resolve_escalation,
)
from tests.build.conftest import FakeStore


def test_BLD8_change_request_returns_parent_to_draft_and_marks_dependents_stale(tmp_path: Path):
    store = FakeStore(tmp_path)
    pid = store.create_project("app", workspace_path=str(tmp_path))
    parent = store.create_node(pid, "sys", "system")
    store.transition_node_status(parent, "awaiting_approval")
    store.transition_node_status(parent, "approved")
    store.transition_node_status(parent, "ready")

    c1 = store.create_node(pid, "core", "leaf", parent_id=parent)
    c2 = store.create_node(pid, "consumer", "leaf", parent_id=parent, depends_on=(c1,))
    for cid in (c1, c2):
        store.transition_node_status(cid, "awaiting_approval")
        store.transition_node_status(cid, "approved")
        store.transition_node_status(cid, "ready")

    art_id = raise_change_request(
        store,
        store.get_project(pid),
        store.get_node(c1),
        "Need unpinned package yaml",
        missing_package="pyyaml",
    )
    assert art_id
    assert store.get_node(parent)["status"] == "draft"
    assert store.get_node(c1)["status"] == "stale"
    assert store.get_node(c2)["status"] == "stale"


def test_ESC1_to_ESC4_packet_build_redaction_and_resolution(tmp_path: Path):
    store = FakeStore(tmp_path)
    pid = store.create_project("app", workspace_path=str(tmp_path))
    nid = store.create_node(pid, "worker", "leaf", spec_text="- R1: Process API_KEY=SECRET123")
    store.transition_node_status(nid, "awaiting_approval")
    store.transition_node_status(nid, "approved")
    store.transition_node_status(nid, "ready")
    store.transition_node_status(nid, "running")
    store.transition_node_status(nid, "failed")
    store.transition_node_status(nid, "escalated")

    pkt = build_escalation_packet(store, pid, nid, workspace_root=tmp_path)
    assert "SECRET123" in pkt.markdown

    redacted = redact_escalation_packet(pkt, remove_substrings=["SECRET123"])
    assert "SECRET123" not in redacted.markdown
    assert "[REDACTED]" in redacted.markdown

    resolve_escalation(store, nid, action="retry", note="Fixed spec")
    assert store.get_node(nid)["status"] == "ready"
