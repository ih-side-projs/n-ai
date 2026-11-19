"""Structured output with JSON schema validation and repair loop (X1)."""

from __future__ import annotations

import json
import logging
from typing import Any

from .schemas import get_schema

logger = logging.getLogger(__name__)


class StructuredOutputError(Exception):
    """Raised when structured output fails after all repair attempts."""
    pass


def call_structured_json(
    client: Any,
    role: str,
    messages: list[dict[str, Any]],
    schema: dict[str, Any],
    tag: str | None = None,
    meta: dict[str, Any] | None = None,
    max_repairs: int = 3,
) -> dict[str, Any]:
    """Call model with JSON schema constrained output, with validation and repair loop.

    Uses llama-server's response_format (JSON schema) where supported,
    falls back to grammar-constrained, then to validate-and-repair.
    """
    # Use the schema directly for response_format
    response_format = {"type": "json_schema", "json_schema": {"name": "output", "schema": schema, "strict": True}}

    for attempt in range(max_repairs + 1):
        try:
            res = client.chat(
                role,
                messages,
                response_format=response_format,
                tag=tag,
                meta=meta,
            )
            content = res.message.content
            if not content:
                raise ValueError("Empty response")

            parsed = json.loads(content)

            # Validate against schema
            _validate_against_schema(parsed, schema)

            return parsed

        except json.JSONDecodeError as e:
            logger.warning(f"JSON decode error on attempt {attempt + 1}: {e}")
            if attempt == max_repairs:
                raise StructuredOutputError(f"Failed to parse JSON after {max_repairs + 1} attempts: {e}")
            # Add repair message
            messages.append({"role": "assistant", "content": content if 'content' in locals() else ""})
            messages.append({"role": "user", "content": f"The previous response was not valid JSON. Error: {e}. Please output only valid JSON matching the schema."})

        except Exception as e:
            logger.warning(f"Validation error on attempt {attempt + 1}: {e}")
            if attempt == max_repairs:
                raise StructuredOutputError(f"Failed validation after {max_repairs + 1} attempts: {e}")
            messages.append({"role": "assistant", "content": content if 'content' in locals() else ""})
            messages.append({"role": "user", "content": f"The previous response failed validation: {e}. Please output only valid JSON matching the schema."})

    raise StructuredOutputError("Max repair attempts exceeded")


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