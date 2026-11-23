"""Tests for BLD-4 deterministic requirement-to-test traceability."""

from nai.build.traceability import check_traceability


def test_BLD4_traceability_passes_and_prevents_prefix_collisions():
    reqs = [
        {"id": "R1", "text": "Adds numbers", "testable": True},
        {"id": "R10", "text": "Handles overflow", "testable": True},
        {"id": "E1", "text": "Non-testable doc note", "testable": False},
    ]
    # R10 alone must NOT satisfy R1
    missing_r1 = {
        "tests/test_calc.py": (
            "def test_R10_overflow():\n"
            "    assert True\n"
        )
    }
    res_fail = check_traceability(reqs, missing_r1)
    assert not res_fail.passed
    assert res_fail.missing == ["R1"]

    both = {
        "tests/test_calc.py": (
            "def test_R1_add():\n"
            "    assert True\n\n"
            "def test_R10_overflow():\n"
            "    assert True\n"
        )
    }
    res_ok = check_traceability(reqs, both)
    assert res_ok.passed
    assert res_ok.missing == []
    assert "R1" in res_ok.covered and "R10" in res_ok.covered
