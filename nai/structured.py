"""Unified structured-output helper for nai (X1, D9)."""
from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


class StructuredOutputError(RuntimeError):
    """Raised when a model fails structured output after all repairs."""


def validate_json_schema(instance: Any, schema: dict[str, Any]) -> list[str]:
    """Validate using jsonschema; return human-readable errors."""
    try:
        import jsonschema
        cls = jsonschema.validators.validator_for(schema)
        cls.check_schema(schema)
        errors = sorted(cls(schema).iter_errors(instance), key=lambda e: list(e.path))
        return [f"$.{'.'.join(str(p) for p in e.path)}: {e.message}" if e.path else f"$: {e.message}"
                for e in errors]
    except ImportError:
        return _basic_validate(instance, schema)


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "null":
        return value is None
    return True


def _basic_validate(data: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Minimal fallback validator (still checks minItems, pattern, etc.)."""
    errors: list[str] = []
    t = schema.get("type")
    types = t if isinstance(t, list) else ([t] if t else [])
    if types and not any(_matches_type(data, x) for x in types):
        return [f"{path}: expected {t}, got {type(data).__name__}"]
    if "enum" in schema and data not in schema["enum"]:
        errors.append(f"{path}: not in enum")
    if isinstance(data, str):
        n = schema.get("minLength")
        if n is not None and len(data) < n:
            errors.append(f"{path}: shorter than minLength {n}")
        n = schema.get("maxLength")
        if n is not None and len(data) > n:
            errors.append(f"{path}: longer than maxLength {n}")
        if "pattern" in schema and not re.search(schema["pattern"], data):
            errors.append(f"{path}: pattern mismatch")
    if isinstance(data, (int, float)) and not isinstance(data, bool):
        if "minimum" in schema and data < schema["minimum"]:
            errors.append(f"{path}: below minimum")
        if "maximum" in schema and data > schema["maximum"]:
            errors.append(f"{path}: above maximum")
    if isinstance(data, list):
        if "minItems" in schema and len(data) < schema["minItems"]:
            errors.append(f"{path}: fewer than minItems")
        if "maxItems" in schema and len(data) > schema["maxItems"]:
            errors.append(f"{path}: more than maxItems")
        if "items" in schema:
            for i, item in enumerate(data):
                errors.extend(_basic_validate(item, schema["items"], f"{path}[{i}]"))
    if isinstance(data, dict):
        for r in schema.get("required", []):
            if r not in data:
                errors.append(f"{path}: missing required {r}")
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for k in data:
                if k not in props:
                    errors.append(f"{path}: unexpected key {k}")
        for k, sub in props.items():
            if k in data:
                errors.extend(_basic_validate(data[k], sub, f"{path}.{k}"))
    return errors


def json_schema_format(schema: dict[str, Any], name: str = "result") -> dict[str, Any]:
    """Json schema format."""
    return {"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}}


def json_schema_to_grammar(schema: dict[str, Any]) -> str:
    """Json schema to grammar."""
    def emit(node: dict[str, Any]) -> str:
        """Emit."""
        t = node.get("type")
        if "enum" in node:
            return "(" + " | ".join(json.dumps(v) for v in node["enum"]) + ")"
        if t == "object":
            out = ['"{"']
            props = node.get("properties", {})
            required = set(node.get("required", []))
            for i, (key, sub) in enumerate(props.items()):
                rule = emit(sub)
                if key not in required:
                    rule = f"({rule})?"
                out.append(f'"{"," if i else ""}{json.dumps(key)}":" {rule}')
            out.append('"}"')
            return " ".join(out)
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


class ConstraintRejected(Exception):
    """Server rejected a constraint mode (D9 fallback signal)."""


def classify_constraint_error(exc: BaseException) -> str | None:
    """Bare 400/422 is ambiguous; only classify when a constraint feature is named."""
    text = f"{type(exc).__name__}: {exc}"
    lower = text.lower()
    terms = ("response_format", "json_schema", "json schema", "grammar",
             "unsupported parameter", "unknown parameter", "unexpected keyword",
             "not supported", "unsupported", "not implemented")
    return "constraint_rejected" if any(term in lower for term in terms) else None


def _strip_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^<think>.*?</think>\s*", "", text, flags=re.DOTALL)
    m = re.match(r"^```(?:json)?\s*\n(.*?)\n```$", text, re.DOTALL)
    return (m.group(1) if m else text).strip()


def call_structured_json(
    client: Any,
    role: str,
    messages: list[dict[str, Any]],
    schema: dict[str, Any],
    *,
    tag: str | None = None,
    meta: dict[str, Any] | None = None,
    max_tokens: int | None = None,
    sampling: dict[str, Any] | None = None,
    max_repairs: int = 3,
    validator=None,
) -> dict[str, Any]:
    """Call a model with constrained JSON, D9 fallback chain and repair loop.

    Mode order: JSON-schema `response_format` → GBNF `grammar` → validate-only.
    A constraint mode rejected by the server is switched immediately.
    """
    modes = [
        ("json_schema", {"response_format": json_schema_format(schema, tag or "result")}),
        ("grammar", {"grammar": json_schema_to_grammar(schema)}),
        ("validate_only", {}),
    ]
    idx = 0
    conv = list(messages)
    for attempt in range(max_repairs + 1):
        label, kwargs = modes[idx]
        attempt_messages = list(conv)
        if label == "validate_only":
            attempt_messages.append({
                "role": "user",
                "content": "Output ONLY a JSON object matching this schema, no prose or fences:\n"
                           + json.dumps(schema, indent=2),
            })
        try:
            res = client.chat(role, attempt_messages, tag=tag, meta=meta,
                              max_tokens=max_tokens, sampling=sampling, **kwargs)
        except Exception as exc:
            if classify_constraint_error(exc) and idx < len(modes) - 1:
                logger.warning("Server rejected %s; falling back to %s: %s", label, modes[idx + 1][0], exc)
                idx += 1
                continue
            raise StructuredOutputError(f"model call failed: {type(exc).__name__}: {exc}") from exc

        content = getattr(getattr(res, "message", None), "content", None)
        if content is None and isinstance(res, tuple):
            content = getattr(res[0], "content", str(res[0]))
        errors: list[str] = []
        parsed: Any = None
        try:
            parsed = json.loads(_strip_fences(content or ""))
            errors = validator(parsed) if validator else validate_json_schema(parsed, schema)
        except json.JSONDecodeError as exc:
            errors = [f"Invalid JSON: {exc}"]
        if not errors:
            return parsed
        if attempt == max_repairs:
            break
        conv.append({"role": "assistant", "content": content or ""})
        conv.append({"role": "user", "content": "The previous response failed validation: " + "; ".join(errors[:5])
                     + "\nReturn only corrected JSON matching the schema."})
    raise StructuredOutputError(f"failed structured output after {max_repairs + 1} attempts")