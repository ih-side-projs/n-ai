"""Backward-compatible re-exports of the unified structured-output helper."""
from nai.structured import (
    ConstraintRejected,
    StructuredOutputError,
    call_structured_json,
    classify_constraint_error,
    json_schema_format,
    validate_json_schema,
)
from nai.structured import (
    json_schema_to_grammar as _json_schema_to_grammar,
)


# Alias kept for older tests/importers
def _is_rejection(exc):
    return classify_constraint_error(exc) == "constraint_rejected"

__all__ = [
    "StructuredOutputError", "ConstraintRejected", "call_structured_json",
    "classify_constraint_error", "_is_rejection", "json_schema_format",
    "_json_schema_to_grammar", "validate_json_schema",
]
