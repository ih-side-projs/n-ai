"""Tests for derived design/implementation workflow phases."""
from __future__ import annotations

import pytest

from nai.workflow import (
    DESIGN,
    IMPLEMENTATION,
    INFO_GATHERING,
    filter_tree_to_phase,
    is_info_gathering,
    job_phase,
    node_phase,
    phase_summary,
    project_phase_summary,
)


class TestNodePhase:
    @pytest.mark.parametrize("status", ["draft", "awaiting_approval", "approved"])
    def test_design_statuses(self, status):
        assert node_phase({"status": status}) == DESIGN

    @pytest.mark.parametrize("status", ["ready", "running", "verifying", "done", "failed", "escalated", "blocked"])
    def test_implementation_statuses(self, status):
        assert node_phase({"status": status}) == IMPLEMENTATION

    def test_stale_uses_latest_design_job(self):
        jobs = [
            {"kind": "implement", "created_at": "2026-01-01T00:00:00Z"},
            {"kind": "component_spec", "created_at": "2026-01-02T00:00:00Z"},
        ]
        assert node_phase({"status": "stale"}, jobs) == DESIGN

    def test_stale_uses_latest_implementation_job(self):
        jobs = [
            {"kind": "component_spec", "created_at": "2026-01-01T00:00:00Z"},
            {"kind": "verify", "created_at": "2026-01-02T00:00:00Z"},
        ]
        assert node_phase({"status": "stale"}, jobs) == IMPLEMENTATION

    def test_stale_defaults_to_design(self):
        assert node_phase({"status": "stale"}) == DESIGN


class TestJobPhase:
    def test_job_phase_mapping(self):
        assert job_phase("decompose") == DESIGN
        assert job_phase("implement") == IMPLEMENTATION
        assert job_phase("unknown") is None


class TestPhaseSummary:
    def test_empty_project_is_design(self):
        s = phase_summary([])
        assert s["phase"] == DESIGN
        assert s["total"] == 0

    def test_all_design(self):
        nodes = [{"id": "a", "status": "draft"}, {"id": "b", "status": "awaiting_approval"}]
        s = phase_summary(nodes)
        assert s["phase"] == DESIGN
        assert s["design_count"] == 2
        assert s["implementation_count"] == 0

    def test_all_implementation(self):
        nodes = [{"id": "a", "status": "running"}, {"id": "b", "status": "done"}]
        s = phase_summary(nodes)
        assert s["phase"] == IMPLEMENTATION

    def test_overlap_is_mixed(self):
        nodes = [{"id": "a", "status": "draft"}, {"id": "b", "status": "running"}]
        s = phase_summary(nodes)
        assert s["phase"] == "mixed"
        assert s["per_node"] == {"a": DESIGN, "b": IMPLEMENTATION}


class TestFilterTree:
    def test_filter_keeps_matching_nodes_and_context(self):
        tree = [{
            "id": "root", "status": "approved", "children": [
                {"id": "a", "status": "draft", "children": []},
                {"id": "b", "status": "running", "children": []},
            ],
        }]
        per_node = {"root": DESIGN, "a": DESIGN, "b": IMPLEMENTATION}
        design = filter_tree_to_phase(tree, DESIGN, per_node)
        assert [n["id"] for n in design[0]["children"]] == ["a"]
        impl = filter_tree_to_phase(tree, IMPLEMENTATION, per_node)
        assert [n["id"] for n in impl[0]["children"]] == ["b"]
        assert impl[0]["phase_context"] is True
        assert "phase" in design[0]


class TestInfoGathering:
    def test_structured_project_starts_in_info_gathering(self):
        project = {"settings": {"intake": {"description": "x"}, "intake_complete": False}}
        nodes = [{"id": "root", "status": "draft"}]
        summary = project_phase_summary(nodes, {}, project)
        assert summary["phase"] == INFO_GATHERING
        assert summary["phase_label"] == "Info Gathering"
        assert is_info_gathering(project) is True

    def test_complete_intake_falls_back_to_node_phase(self):
        project = {"settings": {"intake": {"description": "x"}, "intake_complete": True}}
        summary = project_phase_summary([{"id": "root", "status": "draft"}], {}, project)
        assert summary["phase"] == DESIGN
        assert summary["phase_label"] == "Design"
        assert is_info_gathering(project) is False

    def test_legacy_project_without_intake_is_not_info_gathering(self):
        assert is_info_gathering({"settings": {}}) is False

