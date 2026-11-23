"""Backward-compatible re-exports of the unified structured-output helper."""
from nai.structured import (
    StructuredOutputError,
    call_structured_json,
    json_schema_format,
    validate_json_schema,
)

AUTHOR_TESTS_SCHEMA = {
    "type": "object",
    "required": ["file_path", "code", "covered_requirements"],
    "properties": {
        "file_path": {"type": "string", "minLength": 1},
        "code": {"type": "string", "minLength": 1},
        "covered_requirements": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "string"},
    },
    "additionalProperties": False,
}
DIAGNOSE_SCHEMA = {
    "type": "object",
    "required": ["fault_kind", "summary", "revised_brief"],
    "properties": {
        "fault_kind": {"type": "string", "enum": ["implementation", "spec", "test", "contract", "environment"]},
        "summary": {"type": "string", "minLength": 1},
        "revised_brief": {"type": "string"},
        "missing_package": {"type": ["string", "null"]},
        "affected_requirements": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": False,
}
REVIEW_SCHEMA = {
    "type": "object",
    "required": ["approved", "mismatches"],
    "properties": {
        "approved": {"type": "boolean"},
        "summary": {"type": "string"},
        "mismatches": {"type": "array", "items": {
            "type": "object",
            "required": ["kind", "requirement_id", "description", "suggested_test_name"],
            "properties": {
                "kind": {"type": "string", "enum": ["missing_behavior", "wrong_behavior", "contract_mismatch", "edge_case"]},
                "requirement_id": {"type": "string"},
                "description": {"type": "string"},
                "suggested_test_name": {"type": "string"},
            },
            "additionalProperties": False,
        }},
    },
    "additionalProperties": False,
}

__all__ = [
    "StructuredOutputError", "call_structured_json",
    "json_schema_format", "validate_json_schema",
    "AUTHOR_TESTS_SCHEMA", "DIAGNOSE_SCHEMA", "REVIEW_SCHEMA",
]
