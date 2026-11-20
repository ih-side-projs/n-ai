"""Tests for D9 fallback chain and explicit scenario step fields (WP4 fixes)."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from nai.spec.structured import (
    StructuredOutputError,
    _is_rejection,
    _json_schema_to_grammar,
    call_structured_json,
)
from nai.spec.checkers import check_interfaces


SCHEMA = {
    "type": "object",
    "required": ["items"],
    "properties": {"items": {"type": "array", "items": {"type": "string"}}},
}


class FakeClient:
    def __init__(self, responses, reject_modes=()):
        self.responses = list(responses)
        self.reject_modes = set(reject_modes)
        self.calls = []

    def chat(self, role, messages, **kwargs):
        mode = "json_schema" if "response_format" in kwargs else "grammar" if "grammar" in kwargs else "validate_only"
        self.calls.append(mode)
        if mode in self.reject_modes:
            raise RuntimeError(f"unsupported response_format: {mode}")
        content = self.responses.pop(0)
        return SimpleNamespace(message=SimpleNamespace(content=content))


def test_is_rejection():
    assert _is_rejection(RuntimeError("unsupported response_format"))
    assert _is_rejection(ValueError("400 bad request"))
    assert not _is_rejection(RuntimeError("connection refused"))


def test_grammar_contains_required_key():
    grammar = _json_schema_to_grammar(SCHEMA)
    assert "root ::=" in grammar
    assert "items" in grammar


def test_json_schema_success():
    client = FakeClient(['{"items": ["a"]}'])
    assert call_structured_json(client, "reasoner", [], SCHEMA) == {"items": ["a"]}
    assert client.calls == ["json_schema"]


def test_fallback_to_grammar_on_rejection():
    client = FakeClient(['{"items": ["a"]}'], reject_modes={"json_schema"})
    assert call_structured_json(client, "reasoner", [], SCHEMA) == {"items": ["a"]}
    assert client.calls == ["json_schema", "grammar"]


def test_fallback_to_validate_only():
    client = FakeClient(['{"items": ["a"]}'], reject_modes={"json_schema", "grammar"})
    assert call_structured_json(client, "reasoner", [], SCHEMA) == {"items": ["a"]}
    assert client.calls == ["json_schema", "grammar", "validate_only"]


def test_thinking_block_is_stripped():
    client = FakeClient(['<think>maybe</think>{"items": ["a"]}'])
    assert call_structured_json(client, "reasoner", [], SCHEMA) == {"items": ["a"]}


def test_explicit_scenario_fields_are_checked():
    contract = {
        "entities": [{"name": "User", "fields": [{"name": "id", "type": "int"}]}],
        "components": [
            {"name": "auth", "provides": [{"name": "login", "signature": "login()", "returns": "User"}], "consumes": []},
            {"name": "api", "provides": [{"name": "serve", "signature": "serve()"}], "consumes": ["auth"]},
        ],
        "scenarios": [{
            "id": "S1", "title": "Login", "steps": [
                {"caller": "user", "callee": "auth", "call": "login", "fields": ["User.id"]},
                {"caller": "api", "callee": "auth", "call": "nonexistent_function", "fields": []},
            ],
        }],
    }
    findings = check_interfaces(contract)
    codes = {f.code for f in findings}
    assert "SPEC3_MISSING_CALL" in codes  # explicit call caught without regex


def test_explicit_scenario_fields_valid():
    contract = {
        "entities": [{"name": "User", "fields": [{"name": "id", "type": "int"}]}],
        "components": [
            {"name": "auth", "provides": [{"name": "login", "signature": "login()", "returns": "User"}]},
        ],
        "scenarios": [{
            "id": "S1", "title": "Login", "steps": [
                {"caller": "user", "callee": "auth", "call": "login", "fields": ["User.id"]},
            ],
        }],
    }
    errors = [f for f in check_interfaces(contract) if f.severity == "error"]
    assert errors == []


def test_schema_rejects_legacy_step_fields():
    import jsonschema
    from nai.spec.schemas import INTERFACES_SCHEMA
    legacy = {
        "contract": {
            "entities": [{"name": "User", "fields": [{"name": "id", "type": "int"}]}],
            "components": [{"name": "auth", "provides": [], "consumes": []}],
            "scenarios": [{
                "id": "S1", "title": "Login", "steps": [
                    {"actor": "auth", "action": "login()", "expects": "User.id"},
                ],
            }],
        },
    }
    with pytest.raises(Exception):
        jsonschema.validate(legacy, INTERFACES_SCHEMA)
