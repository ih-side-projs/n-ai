"""SPEC3_PLATFORM: platform isolation in the contract."""

from __future__ import annotations

from types import SimpleNamespace

from nai.spec import jobs as spec_jobs
from nai.spec.checkers import check_interfaces, check_platforms


def _contract(components: list[dict], scenarios: list[dict] | None = None) -> dict:
    return {
        "entities": [{"name": "Input", "fields": [{"name": "text", "type": "str"}]}],
        "components": components,
        "scenarios": scenarios or [],
    }


def _codes(findings: list) -> list[str]:
    return [finding.code for finding in findings]


def test_portable_component_may_not_consume_a_macos_component():
    contract = _contract([
        {"name": "app", "provides": [], "consumes": ["keychain-helper"], "platform": "any"},
        {"name": "keychain-helper", "provides": [], "consumes": [], "platform": "macos"},
    ])
    findings = check_platforms(contract)
    assert _codes(findings) == ["SPEC3_PLATFORM"]
    assert "keychain-helper" in findings[0].message
    # check_interfaces surfaces the same finding.
    assert "SPEC3_PLATFORM" in _codes(check_interfaces(contract))


def test_linux_component_may_not_consume_a_macos_component():
    contract = _contract([
        {"name": "daemon", "provides": [], "consumes": ["keychain-helper"], "platform": "linux"},
        {"name": "keychain-helper", "provides": [], "consumes": [], "platform": "macos"},
    ])
    assert _codes(check_platforms(contract)) == ["SPEC3_PLATFORM"]


def test_macos_components_may_consume_macos_components():
    contract = _contract([
        {"name": "app", "provides": [], "consumes": ["keychain-helper"], "platform": "macos"},
        {"name": "keychain-helper", "provides": [], "consumes": [], "platform": "macos"},
    ])
    assert check_platforms(contract) == []


def test_missing_platform_defaults_to_any_and_is_portable():
    contract = _contract([
        {"name": "app", "provides": [], "consumes": ["keychain-helper"]},
        {"name": "keychain-helper", "provides": [], "consumes": [], "platform": "macos"},
    ])
    assert _codes(check_platforms(contract)) == ["SPEC3_PLATFORM"]


def test_invalid_platform_value_is_rejected():
    contract = _contract([
        {"name": "app", "provides": [], "consumes": [], "platform": "windows"},
    ])
    assert _codes(check_platforms(contract)) == ["SPEC3_PLATFORM_INVALID"]


def test_handle_check_interfaces_fails_deterministically_on_platform_violation():
    contract = _contract([
        {"name": "app", "provides": [], "consumes": ["keychain-helper"], "platform": "any"},
        {"name": "keychain-helper", "provides": [], "consumes": [], "platform": "macos"},
    ])
    events: list[tuple] = []
    ctx = SimpleNamespace(
        check=lambda: None,
        node={"id": "n1", "name": "root", "contract": contract},
        project={"id": "p1", "settings": {}},
        job={"id": "j1", "input_ref": {}},
        event=lambda *args, **kwargs: events.append((args, kwargs)),
    )

    result = spec_jobs.handle_check_interfaces(ctx)

    assert result.ok is False
    assert "platform isolation violated" in (result.error or "")
    assert any(args[0] == "spec.platform_violation" for args, _ in events)
