"""Tests for X1 constrained JSON validation and 3-round repair loop."""

import json

import pytest

from nai.build.structured import (
    DIAGNOSE_SCHEMA,
    StructuredOutputError,
    call_structured_json,
)
from tests.build.conftest import ScriptedClient


def test_X1_structured_json_repairs_invalid_then_succeeds():
    bad_json = "not json at all"
    wrong_enum = json.dumps({"fault_kind": "bogus", "summary": "s", "revised_brief": "b"})
    valid = json.dumps({"fault_kind": "implementation", "summary": "missing return", "revised_brief": "return value"})

    client = ScriptedClient([bad_json, wrong_enum, valid])
    out = call_structured_json(
        client,
        "reasoner",
        [{"role": "user", "content": "diagnose"}],
        DIAGNOSE_SCHEMA,
        tag="diagnose",
        max_repairs=3,
    )
    assert out["fault_kind"] == "implementation"
    assert len(client.calls) == 3
    # Repair prompt includes validation errors
    assert "failed validation" in client.calls[2]["messages"][-1]["content"]


def test_X1_structured_json_fails_after_3_repair_rounds():
    client = ScriptedClient(["{bad}", "{bad}", "{bad}", "{bad}"])
    with pytest.raises(StructuredOutputError):
        call_structured_json(
            client,
            "reasoner",
            [{"role": "user", "content": "diagnose"}],
            DIAGNOSE_SCHEMA,
            max_repairs=3,
        )
    assert len(client.calls) == 4
