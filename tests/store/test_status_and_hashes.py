import pytest

from nai.store import DependencyCycleError, DependencyError, InvalidTransition, content_hash


def _mark_done(store, node_id):
    for status in ("awaiting_approval", "approved", "ready", "running", "verifying", "done"):
        store.transition_node_status(node_id, status)


def test_status_machine_accepts_documented_happy_path_and_records_built_hash(store, project):
    node = store.create_node(project, "root", "system", spec_text="initial")
    _mark_done(store, node)
    row = store.get_node(node)
    assert row["status"] == "done"
    assert row["built_hash"] == row["content_hash"]
    events = store.list_events(project, node_id=node)
    assert [e["data"].get("to") for e in events if e["type"] == "node.status_changed"] == [
        "awaiting_approval", "approved", "ready", "running", "verifying", "done"
    ]


def test_invalid_transition_is_audited_even_though_it_raises(store, project):
    node = store.create_node(project, "root", "system")
    with pytest.raises(InvalidTransition, match="draft -> done"):
        store.transition_node_status(node, "done")
    rejected = [e for e in store.list_events(project, node_id=node) if e["type"] == "node.transition_rejected"]
    assert len(rejected) == 1
    assert rejected[0]["level"] == "warning"
    assert rejected[0]["data"] == {"from": "draft", "to": "done", "note": ""}
    assert store.get_node(node)["status"] == "draft"


def test_spec_change_marks_done_node_and_transitive_dependents_stale(store, project):
    root = store.create_node(project, "root", "system", contract={"version": 1})
    first = store.create_node(project, "first", "leaf", parent_id=root, spec_text="A v1")
    second = store.create_node(project, "second", "leaf", parent_id=root, spec_text="B", depends_on=[first])
    _mark_done(store, first)
    _mark_done(store, second)
    first_hash = store.get_node(first)["content_hash"]
    second_hash = store.get_node(second)["content_hash"]

    store.update_node_spec(first, "A v2")

    first_row = store.get_node(first)
    second_row = store.get_node(second)
    assert first_row["status"] == "stale"
    # Amended §4: only a dependency's *interface* participates in the hash.
    # A spec-text-only edit must not invalidate the dependent.
    assert second_row["status"] == "done"
    assert first_row["content_hash"] != first_hash
    assert second_row["content_hash"] == second_hash
    assert first_row["built_hash"] == first_hash
    assert second_row["built_hash"] == second_hash


def test_parent_contract_change_stales_children(store, project):
    root = store.create_node(project, "root", "system", contract={"entities": []})
    child = store.create_node(project, "child", "component", parent_id=root, spec_text="child spec")
    _mark_done(store, child)
    previous = store.get_node(child)["content_hash"]
    store.update_node_contract(root, {"entities": [{"name": "Thing"}]})
    assert store.get_node(child)["status"] == "stale"
    assert store.get_node(child)["content_hash"] != previous


def test_hashing_is_canonical_and_dependency_order_is_explicit():
    left = content_hash("spec", {"b": 2, "a": 1}, ["h1", "h2"], {"parent": True})
    right = content_hash("spec", {"a": 1, "b": 2}, ["h1", "h2"], {"parent": True})
    reordered = content_hash("spec", {"a": 1, "b": 2}, ["h2", "h1"], {"parent": True})
    assert left == right
    assert left != reordered


def test_dependency_cycle_rolls_back_and_non_sibling_dependency_is_rejected(store, project):
    root = store.create_node(project, "root", "system")
    a = store.create_node(project, "a", "leaf", parent_id=root)
    b = store.create_node(project, "b", "leaf", parent_id=root)
    store.set_node_dependencies(a, [b])
    with pytest.raises(DependencyCycleError):
        store.set_node_dependencies(b, [a])
    assert store.get_node(b)["depends_on"] == []

    # A node under a different project cannot be a dependency either.
    other_project = store.create_project("other")
    other_parent = store.create_node(other_project, "other-root", "system")
    other_child = store.create_node(other_project, "other-child", "leaf", parent_id=other_parent)
    with pytest.raises(DependencyError, match="siblings"):
        store.set_node_dependencies(a, [other_child])


def test_hash_recompute_is_idempotent(store, project):
    root = store.create_node(project, "root", "system")
    before = store.get_node(root)["content_hash"]
    result = store.recompute_project_hashes(project)
    assert result[root] == before
    assert store.get_node(root)["status"] == "draft"
