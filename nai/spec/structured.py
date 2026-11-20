"""Structured output with JSON schema validation and repair loop (X1, D9)."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from .schemas import get_schema

logger = logging.getLogger(__name__)

# D9: phrases that indicate the server rejected a structured-output request
_REJECTION_PATTERNS = (
    "response_format", "json_schema", "grammar", "not supported", "unsupported",
    "invalid request", "bad request", "unknown field", "unexpected keyword",
    "400", "422", "not implemented",
)


class StructuredOutputError(Exception):
    """Raised when structured output fails after all repair attempts."""
    pass


def _is_rejection(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(p in text for p in _REJECTION_PATTERNS)


def _json_schema_to_grammar(schema: dict[str, Any]) -> str:
    """Build a conservative GBNF grammar from a JSON schema (D9 grammar fallback)."""
    counter = [0]

    def emit(node: dict[str, Any]) -> str:
        t = node.get("type")
        if "enum" in node:
            return "(" + " | ".join(json.dumps(v) for v in node["enum"]) + ")"
        if t == "object":
            parts = ['"{"']
            props = node.get("properties", {})
            required = set(node.get("required", []))
            for i, (key, sub) in enumerate(props.items()):
                sep = "" if i == 0 else '","'
                sub_rule = emit(sub)
                if key not in required:
                    sub_rule = f"({sub_rule})?"
                parts.append(f'"{sep}{json.dumps(key)}":" {sub_rule}')
            parts.append('"}"')
            return " ".join(parts)
        if t == "array":
            item = emit(node.get("items", {}))
            return f'"[" ({item} ("," {item})*)? "]"'
        if t == "string":
            return '"\\"" char* "\\""'
        if t == "integer":
            return '("-"? [0-9]+)'
        if t == "number":
            return '("-"? [0-9]+ ("." [0-9]+)?)'
        if t == "boolean":
            return "(true | false)"
        if t == "null":
            return "null"
        return '"\\"" char* "\\""'

    return "root ::= " + emit(schema) + '\nchar ::= [^"\\\\] | "\\\\" .'


def call_structured_json(
    client: Any,
    role: str,
    messages: list[dict[str, Any]],
    schema: dict[str, Any],
    tag: str | None = None,
    meta: dict[str, Any] | None = None,
    max_repairs: int = 3,
) -> dict[str, Any]:
    """Call model with constrained output using the D9 fallback chain.

    Order: JSON-schema response_format -> GBNF grammar -> validate-and-repair.
    A rejected constraint mode is switched immediately (never retried with the
    same unsupported request). Reasoning-only responses are never parsed as JSON.
    """
    response_format = {"type": "json_schema", "json_schema": {"name": "output", "schema": schema, "strict": True}}
    grammar = _json_schema_to_grammar(schema)
    modes: list[tuple[str, dict[str, Any]]] = [
        ("json_schema", {"response_format": response_format}),
        ("grammar", {"grammar": grammar}),
        ("validate_only", {}),
    ]
    mode_index = 0
    last_error: Exception | None = None

    for attempt in range(max_repairs + 1):
        label, kwargs = modes[mode_index]
        attempt_messages = list(messages)
        if label == "validate_only":
            attempt_messages.append({
                "role": "user",
                "content": "Output ONLY a JSON object matching this schema, no prose or markdown fences:\n"
                           + json.dumps(schema, indent=2),
            })
        try:
            res = client.chat(role, attempt_messages, tag=tag, meta=meta, **kwargs)
        except Exception as exc:
            if _is_rejection(exc) and mode_index < len(modes) - 1:
                logger.warning("Server rejected %s (%s); falling back to %s", label, exc, modes[mode_index + 1][0])
                mode_index += 1
                continue
            raise

        content = res.message.content
        if not content:
            raise StructuredOutputError("Empty response content (reasoning-only response?)")

        cleaned = content.strip()
        cleaned = re.sub(r"^<think>.*?</think>\s*", "", cleaned, flags=re.DOTALL)
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned)

        try:
            parsed = json.loads(cleaned)
            _validate_against_schema(parsed, schema)
            return parsed
        except Exception as exc:
            last_error = exc
            logger.warning("Structured output %s attempt %d failed: %s", label, attempt + 1, exc)
            if attempt == max_repairs:
                break

        messages.append({"role": "assistant", "content": content})
        messages.append({
            "role": "user",
            "content": f"The previous response was not valid JSON matching the schema. Error: {last_error}. "
                       "Output ONLY the corrected JSON object, no prose.",
        })

    raise StructuredOutputError(
        f"Failed to get valid structured output after {max_repairs + 1} attempts: {last_error}"
    )


def _validate_against_schema(data: Any, schema: dict[str, Any]) -> None:
    """Validate data against JSON schema (basic validation)."""
    # Use jsonschema if available, otherwise basic checks
    try:
        import jsonschema
        jsonschema.validate(data, schema)
    except ImportError:
        # Basic validation without jsonschema
        _basic_validate(data, schema)


def _basic_validate(data: Any, schema: dict[str, Any], path: str = "") -> None:
    """Basic schema validation without jsonschema dependency."""
    if schema.get("type") == "object":
        if not isinstance(data, dict):
            raise ValueError(f"{path}: expected object, got {type(data).__name__}")
        required = schema.get("required", [])
        for field in required:
            if field not in data:
                raise ValueError(f"{path}: missing required field '{field}'")
        properties = schema.get("properties", {})
        for key, value in data.items():
            if key in properties:
                _basic_validate(value, properties[key], f"{path}.{key}")
            elif not schema.get("additionalProperties", True):
                raise ValueError(f"{path}: additional property '{key}' not allowed")

    elif schema.get("type") == "array":
        if not isinstance(data, list):
            raise ValueError(f"{path}: expected array, got {type(data).__name__}")
        items_schema = schema.get("items", {})
        for i, item in enumerate(data):
            _basic_validate(item, items_schema, f"{path}[{i}]")
        min_items = schema.get("minItems")
        if min_items is not None and len(data) < min_items:
            raise ValueError(f"{path}: array too short (min {min_items})")
        max_items = schema.get("maxItems")
        if max_items is not None and len(data) > max_items:
            raise ValueError(f"{path}: array too long (max {max_items})")

    elif schema.get("type") == "string":
        if not isinstance(data, str):
            raise ValueError(f"{path}: expected string, got {type(data).__name__}")
        pattern = schema.get("pattern")
        if pattern:
            import re
            if not re.match(pattern, data):
                raise ValueError(f"{path}: string does not match pattern {pattern}")
        min_len = schema.get("minLength")
        if min_len is not None and len(data) < min_len:
            raise ValueError(f"{path}: string too short (min {min_len})")

    elif schema.get("type") == "integer":
        if not isinstance(data, int) or isinstance(data, bool):
            raise ValueError(f"{path}: expected integer, got {type(data).__name__}")
        minimum = schema.get("minimum")
        if minimum is not None and data < minimum:
            raise ValueError(f"{path}: integer below minimum {minimum}")

    elif schema.get("type") == "boolean":
        if not isinstance(data, bool):
            raise ValueError(f"{path}: expected boolean, got {type(data).__name__}")

    # Check enum
    if "enum" in schema:
        if data not in schema["enum"]:
            raise ValueError(f"{path}: value not in enum {schema['enum']}")

    # Check const
    if "const" in schema:
        if data != schema["const"]:
            raise ValueError(f"{path}: value does not match const {schema['const']}")