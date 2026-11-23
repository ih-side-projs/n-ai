"""Tests for BLD-2 deterministic contract stub generation and BLD-1 signature extraction."""

from nai.build.stubs import extract_stub_signatures, generate_contract_stubs


def test_BLD2_generate_contract_stubs_and_extract_signatures():
    project = {"id": "p1", "name": "mini-ledger", "settings": {}}
    node = {"id": "n1", "name": "ledger-core", "kind": "leaf"}
    contract = {
        "entities": [
            {
                "name": "Entry",
                "fields": [
                    {"name": "account", "type": "str", "required": True, "description": "Account code"},
                    {"name": "memo", "type": "str", "required": False, "description": "Optional note"},
                ],
            }
        ],
        "components": [
            {
                "name": "ledger-core",
                "provides": [
                    {
                        "signature": "record_entry(entry: Entry) -> str",
                        "description": "Persist a ledger entry.",
                        "input_entities": ["Entry"],
                        "output_entities": ["str"],
                    }
                ],
            }
        ],
    }

    res = generate_contract_stubs(project, node, contract)
    paths = {f.rel_path: f.content for f in res.files}
    assert "src/mini_ledger/entities.py" in paths
    assert "src/mini_ledger/ledger_core.py" in paths
    stub_src = paths["src/mini_ledger/ledger_core.py"]
    assert "def record_entry(entry: Entry) -> str:" in stub_src
    assert "raise NotImplementedError" in stub_src

    # Verify BLD-1 signature extraction strips implementation bodies
    implemented = stub_src.replace('raise NotImplementedError("Contract stub for record_entry")', 'secret_impl = 42\n    return "ok"')
    sigs_only = extract_stub_signatures(implemented)
    assert "def record_entry(entry: Entry) -> str:" in sigs_only
    assert "secret_impl" not in sigs_only
