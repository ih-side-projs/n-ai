"""Tests for SS-SPEC schemas (SPEC-5)."""
from __future__ import annotations
import json
import pytest
from nai.spec.schemas import (
    get_schema, DECOMPOSE_SCHEMA, SELECT_MAP_SCHEMA, INTERFACES_SCHEMA,
    CHECK_INTERFACES_SCHEMA, COMPONENT_SPEC_SCHEMA, RECONCILE_SCHEMA, CLARIFY_SCHEMA,
    write_schemas_to_disk,
)

def _validate(data: dict, schema: dict):
    try:
        import jsonschema
        jsonschema.validate(data, schema)
    except ImportError:
        for req in schema.get("required", []):
            assert req in data, f"Missing required field: {req}"

def test_all_schemas_present():
    for name in ["decompose", "select_map", "interfaces", "check_interfaces", "component_spec", "reconcile", "clarify"]:
        schema = get_schema(name)
        assert schema is not None
        assert schema.get("title") is not None

def test_decompose_schema_valid():
    valid = {
        "components": [{"name": "auth", "objective": "Handle authentication", "depends_on": []}],
        "shared_entities": [{"name": "User", "description": "A user", "fields": [{"name": "id", "type": "int"}]}],
        "external_inputs": [{"name": "credentials", "description": "Login creds", "source": "user"}],
        "external_outputs": [{"name": "token", "description": "Auth token", "destination": "user"}],
    }
    _validate(valid, DECOMPOSE_SCHEMA)

def test_decompose_schema_rejects_invalid():
    invalid = {"components": [], "shared_entities": [], "external_inputs": []}
    with pytest.raises(Exception):
        _validate(invalid, DECOMPOSE_SCHEMA)
    invalid = {"components": [{"name": "Auth", "objective": "Handle auth", "depends_on": []}], "shared_entities": [], "external_inputs": [], "external_outputs": []}
    with pytest.raises(Exception):
        _validate(invalid, DECOMPOSE_SCHEMA)

def test_interfaces_schema_valid():
    valid = {
        "contract": {
            "entities": [{"name": "User", "fields": [{"name": "id", "type": "int"}]}],
            "components": [{"name": "auth", "provides": [{"name": "login", "signature": "login(u,p) -> User", "returns": "User"}], "consumes": []}],
            "scenarios": [{"id": "S1", "title": "Login", "steps": [{"actor": "auth", "action": "login(u,p)", "expects": "User is returned with a valid session token", "provides": ["User.id"]}]}],
        },
    }
    _validate(valid, INTERFACES_SCHEMA)

def test_check_interfaces_schema_valid():
    valid = {"gaps": [{"scenario_id": "S1", "step_index": 0, "issue": "missing_provider", "missing_element": "User.id", "suggested_fix": "Add provider"}], "all_verified": False}
    _validate(valid, CHECK_INTERFACES_SCHEMA)
    valid2 = {"gaps": [], "all_verified": True}
    _validate(valid2, CHECK_INTERFACES_SCHEMA)

def test_component_spec_schema_valid():
    valid = {"spec_markdown": "# Component: auth\n## Purpose\n...\n## Requirements\n- R1: Login works\n## Edge Cases\n- E1: Bad password\n## Example\nInput: x\nOutput: y\n## Dependencies\n- none", "requirements": [{"id": "R1", "text": "Login works", "testable": True}], "edge_cases": [{"id": "E1", "text": "Bad password"}], "example": "Input: username and password\nOutput: a valid session token", "dependencies": []}
    _validate(valid, COMPONENT_SPEC_SCHEMA)

def test_reconcile_schema_valid():
    valid = {"findings": [{"type": "contradiction", "component": "auth", "description": "Two components claim auth", "severity": "error", "related_components": ["orders"], "suggested_fix": "Merge auth"}]}
    _validate(valid, RECONCILE_SCHEMA)

def test_clarify_schema_valid():
    valid = {"questions": [{"id": "Q1", "question": "What language?", "default_answer": "Python 3.12", "category": "language"}, {"id": "Q2", "question": "Dependencies?", "default_answer": "[]", "category": "dependencies"}]}
    _validate(valid, CLARIFY_SCHEMA)
    too_many = {"questions": [{"id": f"Q{i}", "question": "Q?", "default_answer": "A", "category": "other"} for i in range(6)]}
    with pytest.raises(Exception):
        _validate(too_many, CLARIFY_SCHEMA)

def test_schemas_written_to_disk(tmp_path):
    import nai.spec.schemas as schemas_module
    original_dir = schemas_module._SCHEMAS_DIR
    schemas_module._SCHEMAS_DIR = tmp_path / "schemas"
    write_schemas_to_disk()
    for name in ["decompose", "select_map", "interfaces", "check_interfaces", "component_spec", "reconcile", "clarify"]:
        path = schemas_module._SCHEMAS_DIR / f"{name}.json"
        assert path.is_file()
        json.loads(path.read_text())
    contract_path = schemas_module._SCHEMAS_DIR / "contract.json"
    assert contract_path.is_file()
    json.loads(contract_path.read_text())