"""JSON schemas for SS-SPEC stage outputs (X1)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..langs import registry as _language_registry
from ..langs import support as _language_support
from .platforms import PLATFORMS

_SCHEMAS_DIR = Path(__file__).resolve().parent / "schemas"


def supported_language_ids() -> list[str]:
    """Language ids the contract schema accepts, derived from the registry."""
    ids = _language_support.supported_languages() or _language_registry.supported()
    return ids or [_language_registry.DEFAULT_LANGUAGE]


SUPPORTED_LANGUAGES = supported_language_ids()


def load_schema(name: str) -> dict[str, Any]:
    path = _SCHEMAS_DIR / f"{name}.json"
    if not path.is_file():
        raise FileNotFoundError(f"Schema not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))
DECOMPOSE_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "DecomposeOutput",
    "type": "object",
    "required": ["components", "shared_entities", "external_inputs", "external_outputs"],
    "additionalProperties": False,
    "properties": {
        "components": {"type": "array", "minItems": 1, "maxItems": 10,
            "items": {"type": "object", "required": ["name", "objective", "depends_on"],
                "additionalProperties": False, "properties": {
                    "name": {"type": "string", "pattern": "^[a-z][a-z0-9-]*$"},
                    "objective": {"type": "string", "minLength": 10, "maxLength": 500},
                    "depends_on": {"type": "array", "items": {"type": "string", "pattern": "^[a-z][a-z0-9-]*$"}}
                }}},
        "shared_entities": {"type": "array", "items": {
            "type": "object", "required": ["name", "description"],
            "additionalProperties": False, "properties": {
                "name": {"type": "string", "pattern": "^[A-Z][a-zA-Z0-9]*$"},
                "description": {"type": "string", "minLength": 5},
                "fields": {"type": "array", "items": {
                    "type": "object", "required": ["name", "type"],
                    "additionalProperties": False, "properties": {
                        "name": {"type": "string", "pattern": "^[a-z_][a-zA-Z0-9_]*$"},
                        "type": {"type": "string"}, "description": {"type": "string"}
                    }}}}}},
        "external_inputs": {"type": "array", "items": {
            "type": "object", "required": ["name", "description", "source"],
            "additionalProperties": False, "properties": {
                "name": {"type": "string", "pattern": "^[a-z][a-z0-9-]*$"},
                "description": {"type": "string"},
                "source": {"type": "string", "enum": ["user", "file", "api", "config", "env", "other"]}
            }}},
        "external_outputs": {"type": "array", "items": {
            "type": "object", "required": ["name", "description", "destination"],
            "additionalProperties": False, "properties": {
                "name": {"type": "string", "pattern": "^[a-z][a-z0-9-]*$"},
                "description": {"type": "string"},
                "destination": {"type": "string", "enum": ["user", "file", "api", "db", "ui", "other"]}
            }}},
    }}

SELECT_MAP_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "SelectMapOutput",
    "type": "object",
    "required": ["selected_index", "rationale", "merged_map"],
    "additionalProperties": False,
    "properties": {
        "selected_index": {"type": "integer", "minimum": 0},
        "rationale": {"type": "string", "minLength": 10},
        "merged_map": DECOMPOSE_SCHEMA
    }
}

INTERFACES_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "InterfacesOutput",
    "type": "object",
    "required": ["contract"],
    "additionalProperties": False,
    "properties": {
        "contract": {
            "type": "object",
            "required": ["entities", "components", "scenarios"],
            "additionalProperties": False,
            "properties": {
                "language": {
                    "type": "string",
                    "enum": SUPPORTED_LANGUAGES,
                    "default": "python",
                    "description": "Project language id (one per project); generated from the adapter registry",
                },
                "entities": {"type": "array", "items": {
                    "type": "object", "required": ["name", "fields"],
                    "additionalProperties": False, "properties": {
                        "name": {"type": "string", "pattern": "^[A-Z][a-zA-Z0-9]*$"},
                        "description": {"type": "string"},
                        "fields": {"type": "array", "items": {
                            "type": "object", "required": ["name", "type"],
                            "additionalProperties": False, "properties": {
                                "name": {"type": "string", "pattern": "^[a-z_][a-zA-Z0-9_]*$"},
                                "type": {"type": "string"}, "description": {"type": "string"},
                                "optional": {"type": "boolean", "default": False}
                            }}}}}},
                "components": {"type": "array", "items": {
                    "type": "object", "required": ["name", "provides", "consumes"],
                    "additionalProperties": False, "properties": {
                        "name": {"type": "string", "pattern": "^[a-z][a-z0-9-]*$"},
                        "description": {"type": "string"},
                        "platform": {
                            "type": "string",
                            "enum": list(PLATFORMS),
                            "default": "any",
                            "description": "any|linux|macos; isolate platform-specific code in leaf components",
                        },
                        "provides": {"type": "array", "items": {
                            "type": "object", "required": ["name", "signature"],
                            "additionalProperties": False, "properties": {
                                "name": {"type": "string", "pattern": "^[a-z_][a-zA-Z0-9_]*$"},
                                "signature": {"type": "string"}, "description": {"type": "string"},
                                "returns": {"type": "string"}
                            }}},
                        "consumes": {"type": "array", "items": {"type": "string", "pattern": "^[a-z][a-z0-9-]*$"}}
                    }}},
                "scenarios": {
                    "type": "array", "items": {
                        "type": "object", "required": ["id", "title", "steps"],
                        "additionalProperties": False, "properties": {
                            "id": {"type": "string", "pattern": "^S[0-9]+$"},
                            "title": {"type": "string"},
                            "steps": {"type": "array", "items": {
                                "type": "object", "required": ["caller", "callee", "call", "fields"],
                                "additionalProperties": False, "properties": {
                                    "caller": {"type": "string", "description": "Component id initiating the call, or 'user'/'external'"},
                                    "callee": {"type": "string", "description": "Component id being called, or 'user'/'external'"},
                                    "call": {"type": "string", "description": "Function name on the callee, no parentheses"},
                                    "fields": {
                                        "type": "array",
                                        "description": "Field references passed/returned, e.g. User.id, Order.total",
                                        "items": {"type": "string", "pattern": "^[A-Z][a-zA-Z0-9]*\\.[a-z_][a-zA-Z0-9_]*$"},
                                    },
                                }}}}
                        }}}
            }}
    }
CHECK_INTERFACES_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "CheckInterfacesOutput",
    "type": "object",
    "required": ["gaps", "all_verified"],
    "additionalProperties": False,
    "properties": {
        "gaps": {"type": "array", "items": {
            "type": "object", "required": ["scenario_id", "step_index", "issue", "missing_element"],
            "additionalProperties": False, "properties": {
                "scenario_id": {"type": "string", "pattern": "^S[0-9]+$"},
                "step_index": {"type": "integer", "minimum": 0},
                "issue": {"type": "string", "enum": ["missing_provider", "missing_consumer", "missing_entity", "missing_call", "unused_component"]},
                "missing_element": {"type": "string"}, "suggested_fix": {"type": "string"}
            }}},
        "all_verified": {"type": "boolean"}
    }
}

COMPONENT_SPEC_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "ComponentSpecOutput",
    "type": "object",
    "required": ["spec_markdown", "requirements", "edge_cases", "example"],
    "additionalProperties": False,
    "properties": {
        "spec_markdown": {"type": "string", "minLength": 50},
        "requirements": {"type": "array", "minItems": 1, "items": {
            "type": "object", "required": ["id", "text"],
            "additionalProperties": False, "properties": {
                "id": {"type": "string", "pattern": "^R[0-9]+$"},
                "text": {"type": "string", "minLength": 5},
                "testable": {"type": "boolean", "default": True}
            }}},
        "edge_cases": {"type": "array", "items": {
            "type": "object", "required": ["id", "text"],
            "additionalProperties": False, "properties": {
                "id": {"type": "string", "pattern": "^E[0-9]+$"},
                "text": {"type": "string", "minLength": 5}
            }}},
        "example": {"type": "string", "minLength": 20},
        "dependencies": {"type": "array", "items": {"type": "string", "pattern": "^[a-z][a-z0-9-]*$"}}
    }
}

RECONCILE_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "ReconcileOutput",
    "type": "object",
    "required": ["findings"],
    "additionalProperties": False,
    "properties": {
        "findings": {"type": "array", "items": {
            "type": "object", "required": ["type", "component", "description", "severity"],
            "additionalProperties": False, "properties": {
                "type": {"type": "string", "enum": ["contradiction", "duplication", "missing_responsibility", "inconsistent_dependency"]},
                "component": {"type": "string", "pattern": "^[a-z][a-z0-9-]*$"},
                "description": {"type": "string", "minLength": 10},
                "severity": {"type": "string", "enum": ["error", "warning", "info"]},
                "related_components": {"type": "array", "items": {"type": "string"}},
                "suggested_fix": {"type": "string"}
            }}}}
}

CLARIFY_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "ClarifyOutput",
    "type": "object",
    "required": ["questions"],
    "additionalProperties": False,
    "properties": {
        "questions": {"type": "array", "maxItems": 5, "items": {
            "type": "object", "required": ["id", "question", "default_answer", "category"],
            "additionalProperties": False, "properties": {
                "id": {"type": "string", "pattern": "^Q[0-9]+$"},
                "question": {"type": "string", "minLength": 10},
                "default_answer": {"type": "string"},
                "category": {"type": "string", "enum": ["language", "dependencies", "scope", "interfaces", "nonfunctional", "other"]}
            }}}}
}
INTAKE_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "IntakeOutput",
    "type": "object",
    "required": ["ready_for_design", "questions", "readiness_summary", "refined_brief", "guardrails"],
    "additionalProperties": False,
    "properties": {
        "ready_for_design": {"type": "boolean"},
        "questions": {
            "type": "array", "maxItems": 5, "items": {
                "type": "object", "required": ["id", "question", "why"], "additionalProperties": False,
                "properties": {
                    "id": {"type": "string", "pattern": "^Q[0-9]+$"},
                    "question": {"type": "string", "minLength": 10},
                    "why": {"type": "string", "minLength": 5},
                },
            },
        },
        "readiness_summary": {"type": "string", "minLength": 10},
        "refined_brief": {"type": "string", "minLength": 20},
        "guardrails": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "language": {"type": "string"},
                "interface_type": {"type": "string"},
                "target_users": {"type": "string"},
                "core_workflows": {"type": "string"},
                "non_goals": {"type": "string"},
                "runtime_deployment": {"type": "string"},
                "stack_libraries": {"type": "string"},
                "accessibility_security": {"type": "string"},
                "testing_expectations": {"type": "string"},
            },
        },
    },
}

SCHEMAS = {
    "intake": INTAKE_SCHEMA,
    "decompose": DECOMPOSE_SCHEMA,
    "select_map": SELECT_MAP_SCHEMA,
    "interfaces": INTERFACES_SCHEMA,
    "check_interfaces": CHECK_INTERFACES_SCHEMA,
    "component_spec": COMPONENT_SPEC_SCHEMA,
    "reconcile": RECONCILE_SCHEMA,
    "clarify": CLARIFY_SCHEMA,
}

def get_schema(name: str) -> dict[str, Any]:
    return SCHEMAS.get(name, {})


def write_schemas_to_disk() -> None:
    """Generate schemas/*.json from this module (explicit only, not at import time).

    schemas.py is the single source of truth. The JSON files are build artifacts
    for external consumers and documentation.
    """
    _SCHEMAS_DIR.mkdir(exist_ok=True)
    for name, schema in SCHEMAS.items():
        (_SCHEMAS_DIR / f"{name}.json").write_text(json.dumps(schema, indent=2, sort_keys=True), encoding="utf-8")
    contract_schema = {"$schema": "http://json-schema.org/draft-07/schema#", "title": "Contract", **INTERFACES_SCHEMA["properties"]["contract"]}
    (_SCHEMAS_DIR / "contract.json").write_text(json.dumps(contract_schema, indent=2, sort_keys=True), encoding="utf-8")


if __name__ == "__main__":
    write_schemas_to_disk()
    print(f"Wrote {len(SCHEMAS) + 1} schemas to {_SCHEMAS_DIR}")