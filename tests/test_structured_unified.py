"""Tests for Pass 4 unified structured output."""
from __future__ import annotations

from nai import structured
from nai.spec.structured import _is_rejection


def test_jsonschema_enforces_full_schema():
    schema = {"type": "object", "required": ["items"], "properties": {
        "items": {"type": "array", "minItems": 2, "items": {"type": "string", "pattern": "^[A-Z]+$"}}
    }, "additionalProperties": False}
    errors = structured.validate_json_schema({"items": ["ok", "bad"]}, schema)
    assert len(errors) >= 2  # pattern mismatch on both bad strings
    # minItems is enforced by jsonschema (with a shorter array)
    assert structured.validate_json_schema({"items": ["OK"]}, schema)
    assert structured.validate_json_schema({"items": ["OK", "YES"]}, schema) == []
    assert structured.validate_json_schema({"items": ["OK", "YES"], "extra": 1}, schema)


def test_ambiguous_http_error_is_not_constraint_rejection():
    assert structured.classify_constraint_error(ValueError("400 invalid api key")) is None
    assert structured.classify_constraint_error(ValueError("422 model not found")) is None
    assert structured.classify_constraint_error(RuntimeError("400 unsupported response_format")) == "constraint_rejected"
    assert structured.classify_constraint_error(RuntimeError("grammar not supported")) == "constraint_rejected"
    assert not _is_rejection(ValueError("400 bad request"))


def test_shared_error_class_and_call():
    from nai.build.structured import StructuredOutputError as B
    from nai.spec.structured import StructuredOutputError as S
    assert B is S is structured.StructuredOutputError


def test_grammar_and_fences():
    g = structured.json_schema_to_grammar({"type": "object", "required": ["a"], "properties": {"a": {"type": "string"}}})
    assert "root ::=" in g and "a" in g and "char" in g
    assert structured._strip_fences('<think>x</think>\n```json\n{"a": 1}\n```') == '{"a": 1}'
