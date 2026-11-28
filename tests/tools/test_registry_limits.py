"""Tests for Pass 3 tool execution hardening."""
from __future__ import annotations

from nai.tools.registry import get_registry
from nai.tools.sandbox import Sandbox, get_current_sandbox


def test_per_call_sandbox_is_isolated(tmp_path):
    """call_tool sets a contextvar sandbox without mutating module globals."""
    import threading

    registry = get_registry()
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    (root_a / "x.json").write_text('{"root": "a"}', encoding="utf-8")
    (root_b / "x.json").write_text('{"root": "b"}', encoding="utf-8")

    results = {}

    def run(key, sb):
        results[key] = registry.call_tool("read_json", {"path": "x.json"}, sandbox=sb)

    t1 = threading.Thread(target=run, args=("a", Sandbox(root_a)))
    t2 = threading.Thread(target=run, args=("b", Sandbox(root_b)))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert '"a"' in results["a"] and '"b"' not in results["a"]
    assert '"b"' in results["b"] and '"a"' not in results["b"]
    assert get_current_sandbox() is None  # no leaked global


def test_registry_validates_argument_types(tmp_path):
    registry = get_registry()
    sb = Sandbox(tmp_path)
    r = registry.call_tool("read_csv", {"path": 123}, sandbox=sb)
    assert r.startswith("ERROR:") and "must be a string" in r
    r = registry.call_tool("read_csv", {"path": "x", "bogus": 1}, sandbox=sb)
    assert r.startswith("ERROR:") and "Unexpected parameter" in r


def test_registry_enforces_output_limit(tmp_path):
    import json

    registry = get_registry()
    sb = Sandbox(tmp_path, max_output_bytes=50)
    # write a larger JSON file then read it; result must be truncated
    (tmp_path / "big.json").write_text(json.dumps({"x": "y" * 200}), encoding="utf-8")
    result = registry.call_tool("read_json", {"path": "big.json"}, sandbox=sb)
    assert "output truncated" in result
