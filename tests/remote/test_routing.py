"""Routing: macos components and macos integration subtrees go to a worker."""

from __future__ import annotations

import pytest

from nai.remote.errors import RemoteError
from nai.remote.routing import (
    MACOS,
    node_platform,
    remote_enabled,
    route_for,
    subtree_has_platform,
    target_for,
)


def _project(store, tmp_path, *, enabled: bool = True) -> dict:
    settings = {"remote": {"enabled": True}} if enabled else {}
    pid = store.create_project("routed", "brief", workspace_path=tmp_path / "ws", settings=settings)
    return store.get_project(pid)


def test_platform_comes_from_the_parent_component_list(store, tmp_path) -> None:
    project = _project(store, tmp_path)
    root = store.create_node(
        project["id"], "root", "system",
        contract={"components": [
            {"name": "keychain-helper", "platform": "macos"},
            {"name": "parser", "platform": "any"},
        ]},
    )
    mac = store.create_node(project["id"], "keychain-helper", "leaf", parent_id=root)
    plain = store.create_node(project["id"], "parser", "leaf", parent_id=root)
    assert node_platform(store, store.get_node(mac)) == MACOS
    assert node_platform(store, store.get_node(plain)) == "any"
    assert node_platform(store, None) == "any"


def test_a_node_contract_can_declare_its_own_platform(store, tmp_path) -> None:
    project = _project(store, tmp_path)
    node = store.create_node(project["id"], "root", "system", contract={"platform": "macos"})
    assert node_platform(store, store.get_node(node)) == MACOS


def test_subtree_detects_a_macos_descendant(store, tmp_path) -> None:
    project = _project(store, tmp_path)
    root = store.create_node(project["id"], "root", "system", contract={"components": []})
    mid = store.create_node(project["id"], "mid", "component", parent_id=root)
    leaf = store.create_node(project["id"], "leaf", "leaf", parent_id=mid,
                             contract={"platform": "macos"})
    assert subtree_has_platform(store, project["id"], mid, MACOS) is True
    assert subtree_has_platform(store, project["id"], root, MACOS) is True
    assert subtree_has_platform(store, project["id"], leaf, MACOS) is True
    other = store.create_node(project["id"], "other", "leaf", parent_id=root)
    assert subtree_has_platform(store, project["id"], other, MACOS) is False


def test_route_requires_remote_to_be_enabled(store, tmp_path) -> None:
    disabled = _project(store, tmp_path, enabled=False)
    assert remote_enabled(disabled) is False
    node = store.create_node(disabled["id"], "root", "system", contract={"platform": "macos"})
    remote, reason = route_for(disabled, store.get_node(node), "verify", store=store)
    assert remote is False and "disabled" in reason


def test_route_matrix(store, tmp_path) -> None:
    project = _project(store, tmp_path)
    root = store.create_node(project["id"], "root", "system",
                             contract={"components": [{"name": "mac-bit", "platform": "macos"},
                                                      {"name": "portable", "platform": "any"}]})
    mac = store.get_node(store.create_node(project["id"], "mac-bit", "leaf", parent_id=root))
    portable = store.get_node(store.create_node(project["id"], "portable", "leaf", parent_id=root))
    integration = store.get_node(store.create_node(project["id"], "root-integration", "integration",
                                                   parent_id=root))

    assert route_for(project, mac, "verify", store=store)[0] is True
    assert route_for(project, mac, "integrate", store=store)[0] is True  # subtree is macos
    assert route_for(project, portable, "verify", store=store)[0] is False
    # The integration node's tests exercise the parent's subtree, so its verify
    # is remote too when a sibling is macos.
    assert route_for(project, integration, "integrate", store=store)[0] is True
    assert route_for(project, integration, "verify", store=store)[0] is True
    assert route_for(project, mac, "author_tests", store=store)[0] is True
    assert route_for(project, mac, "decompose", store=store)[0] is False


def test_route_for_an_integration_node_in_a_portable_subtree(store, tmp_path) -> None:
    project = _project(store, tmp_path)
    root = store.create_node(project["id"], "root", "system",
                             contract={"components": [{"name": "portable", "platform": "any"}]})
    store.create_node(project["id"], "portable", "leaf", parent_id=root)
    integration = store.get_node(store.create_node(project["id"], "root-integration", "integration",
                                                   parent_id=root))
    assert route_for(project, integration, "integrate", store=store)[0] is False
    assert route_for(project, integration, "verify", store=store)[0] is False


def test_target_for_returns_local_when_not_routed(store, tmp_path) -> None:
    project = _project(store, tmp_path, enabled=False)
    node = store.get_node(store.create_node(project["id"], "root", "system"))
    local = object()
    assert target_for(project, node, "verify", "verify", store=store, local_target=local) is local


def test_target_for_requires_a_remote_factory_when_routed(store, tmp_path) -> None:
    project = _project(store, tmp_path)
    node = store.get_node(store.create_node(project["id"], "root", "system",
                                            contract={"platform": "macos"}))
    with pytest.raises(RemoteError, match="needs a worker"):
        target_for(project, node, "verify", "verify", store=store)
    sentinel = object()
    assert target_for(project, node, "verify", "verify", store=store,
                      make_remote=lambda recipe: sentinel) is sentinel
