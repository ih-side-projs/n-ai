"""Tests for SS-SPEC checkers (SPEC-3, SPEC-4)."""
from __future__ import annotations
import pytest
from nai.spec.checkers import check_interfaces, lint_spec

def test_check_interfaces_pass(sample_contract):
    findings = check_interfaces(sample_contract)
    errors = [f for f in findings if f.severity == "error"]
    assert len(errors) == 0

def test_check_interfaces_missing_provider(sample_contract):
    contract = sample_contract.copy()
    contract["scenarios"][0]["steps"].append({"caller": "orders", "callee": "orders", "call": "ship", "fields": ["MissingEntity.tracking"]})
    findings = check_interfaces(contract)
    errors = [f for f in findings if f.severity == "error" and f.code == "SPEC3_MISSING_PROVIDER"]
    assert len(errors) > 0

def test_check_interfaces_missing_call(sample_contract):
    contract = sample_contract.copy()
    contract["scenarios"][0]["steps"][0]["call"] = "nonexistent"
    findings = check_interfaces(contract)
    errors = [f for f in findings if f.severity == "error" and f.code == "SPEC3_MISSING_CALL"]
    assert len(errors) > 0

def test_check_interfaces_unknown_caller(sample_contract):
    contract = sample_contract.copy()
    contract["scenarios"][0]["steps"][0]["caller"] = "unknown_component"
    findings = check_interfaces(contract)
    errors = [f for f in findings if f.severity == "error" and f.code == "SPEC3_UNKNOWN_ACTOR"]
    assert len(errors) > 0

def test_check_interfaces_missing_entity(sample_contract):
    contract = sample_contract.copy()
    contract["scenarios"][0]["steps"][0]["fields"] = ["NonExistent.field"]
    findings = check_interfaces(contract)
    errors = [f for f in findings if f.severity == "error" and f.code == "SPEC3_MISSING_ENTITY"]
    assert len(errors) > 0

def test_check_interfaces_unused_component(sample_contract):
    contract = sample_contract.copy()
    contract["components"].append({"name": "unused", "provides": [], "consumes": []})
    findings = check_interfaces(contract)
    warnings = [f for f in findings if f.severity == "warning" and f.code == "SPEC3_UNUSED_COMPONENT"]
    assert len(warnings) > 0

def test_check_interfaces_dependency_cycle():
    contract = {"entities": [], "components": [{"name": "a", "provides": [], "consumes": ["b"]}, {"name": "b", "provides": [], "consumes": ["a"]}], "scenarios": [], "external_inputs": [], "external_outputs": []}
    findings = check_interfaces(contract)
    errors = [f for f in findings if f.severity == "error" and f.code == "SPEC3_DEPENDENCY_CYCLE"]
    assert len(errors) > 0

def test_check_interfaces_invalid_names():
    contract = {"entities": [{"name": "user", "fields": []}], "components": [{"name": "Auth", "provides": [], "consumes": []}], "scenarios": [], "external_inputs": [], "external_outputs": []}
    findings = check_interfaces(contract)
    errors = [f for f in findings if f.severity == "error"]
    codes = {f.code for f in errors}
    assert "SPEC3_INVALID_ENTITY_NAME" in codes
    assert "SPEC3_INVALID_COMPONENT_NAME" in codes
def test_lint_spec_pass(sample_spec_markdown, sample_contract):
    findings = lint_spec(sample_spec_markdown, sample_contract)
    errors = [f for f in findings if f.severity == "error"]
    assert len(errors) == 0

def test_lint_spec_missing_section(sample_contract):
    spec = "# Component: test\n## Purpose\nOnly purpose\n"
    findings = lint_spec(spec, sample_contract)
    errors = [f for f in findings if f.severity == "error" and f.code == "SPEC4_MISSING_SECTION"]
    assert len(errors) > 0

def test_lint_spec_no_requirements(sample_contract):
    spec = "# Component: test\n## Purpose\nP\n## Interface\nI\n## Requirements\n\n## Edge Cases\n- E1: x\n## Example\nEx\n## Dependencies\n- none"
    findings = lint_spec(spec, sample_contract)
    errors = [f for f in findings if f.severity == "error" and f.code == "SPEC4_NO_REQUIREMENTS"]
    assert len(errors) > 0

def test_lint_spec_duplicate_req_id(sample_contract):
    spec = "# Component: test\n## Requirements\n- R1: First\n- R1: Duplicate\n## Edge Cases\n- E1: x\n## Example\nEx\n## Dependencies\n- none"
    findings = lint_spec(spec, sample_contract)
    errors = [f for f in findings if f.severity == "error" and f.code == "SPEC4_DUPLICATE_REQ_ID"]
    assert len(errors) > 0

def test_lint_spec_vague_word(sample_contract):
    spec = "# Component: test\n## Requirements\n- R1: Make it fast and robust\n## Edge Cases\n- E1: x\n## Example\nEx\n## Dependencies\n- none"
    findings = lint_spec(spec, sample_contract)
    warnings = [f for f in findings if f.severity == "warning" and f.code == "SPEC4_VAGUE_WORD"]
    assert len(warnings) > 0

def test_lint_spec_no_example(sample_contract):
    spec = "# Component: test\n## Requirements\n- R1: x\n## Edge Cases\n- E1: y\n## Example\n\n## Dependencies\n- none"
    findings = lint_spec(spec, sample_contract)
    errors = [f for f in findings if f.severity == "error" and f.code == "SPEC4_NO_EXAMPLE"]
    assert len(errors) > 0

def test_lint_spec_undeclared_dependency(sample_contract):
    spec = "# Component: test\n## Requirements\n- R1: x\n## Edge Cases\n- E1: y\n## Example\nEx\n## Dependencies\n- nonexistent"
    findings = lint_spec(spec, sample_contract)
    errors = [f for f in findings if f.severity == "error" and f.code == "SPEC4_UNDECLARED_DEPENDENCY"]
    assert len(errors) > 0

def test_lint_spec_too_many_requirements(sample_contract):
    reqs = "\n".join(f"- R{i}: Requirement {i}" for i in range(1, 22))
    spec = f"# Component: test\n## Requirements\n{reqs}\n## Edge Cases\n- E1: y\n## Example\nEx\n## Dependencies\n- none"
    findings = lint_spec(spec, sample_contract)
    warnings = [f for f in findings if f.severity == "warning" and f.code == "SPEC4_TOO_MANY_REQUIREMENTS"]
    assert len(warnings) > 0

def test_lint_spec_too_many_functions(sample_contract):
    contract = sample_contract.copy()
    contract["components"][0]["provides"] = [{"name": f"func{i}", "signature": f"func{i}()", "returns": "void"} for i in range(20)]
    spec = "# Component: auth\n## Requirements\n- R1: x\n## Edge Cases\n- E1: y\n## Example\nEx\n## Dependencies\n- none"
    findings = lint_spec(spec, contract)
    warnings = [f for f in findings if f.severity == "warning" and f.code == "SPEC4_TOO_MANY_FUNCTIONS"]
    assert len(warnings) > 0