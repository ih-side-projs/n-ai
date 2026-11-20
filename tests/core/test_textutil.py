"""v1 text-utils R1-R5, E1-E4, example."""
from types import SimpleNamespace as NS

from nai.textutil import msg_to_dict, parse_args, preview, strip_think, truncate


def test_R1_strip_think_variants():
    assert strip_think("a<think>x\ny</think>b") == "ab"
    assert strip_think("  <think>t</think>  hi \n") == "hi"
    assert strip_think(None) == "" and strip_think("") == ""


def test_E1_strip_think_edge():
    assert strip_think("a</think>b") == "b"
    assert strip_think("x<think>unfinished") == "x"


def test_R2_truncate_noop_and_cut():
    assert truncate("abc", 3) == "abc"
    s = "".join(str(i % 10) for i in range(100))
    out = truncate(s, 40)
    assert out == s[:10] + "\n... [60 chars truncated] ...\n" + s[-30:]


def test_R2_example():
    out = truncate("x" * 20000)
    assert out == "x" * 3000 + "\n... [8000 chars truncated] ...\n" + "x" * 9000


def test_R3_parse_args():
    assert parse_args(None) == {} and parse_args("  ") == {}
    assert parse_args('{"a": 1}') == {"a": 1}
    assert parse_args("{bad") is None


def test_E2_parse_args_non_object():
    assert parse_args("[1, 2]") is None


def test_R4_preview():
    assert preview("read_file", {"path": "a.py"}) == "a.py"
    assert preview("write_file", {"path": "a.py", "content": "xyz"}) == "a.py (3 chars)"
    assert preview("edit_file", {}) == "?"
    long = preview("run_command", {"command": "é" * 500})
    assert long.endswith("…") and len(long) == 201
    assert "é" in preview("run_command", {"command": "é"})   # non-ASCII kept


def test_E3_preview_write_empty():
    assert preview("write_file", {}) == "? (0 chars)"


def test_R5_msg_to_dict():
    tc = NS(id="", function=NS(name="f", arguments=""))
    tc2 = NS(id="abc", function=NS(name="g", arguments='{"a":1}'))
    d = msg_to_dict(NS(content="<think>x</think>ok", tool_calls=[tc, tc2]))
    assert d["role"] == "assistant" and d["content"] == "ok"
    assert d["tool_calls"][0]["id"].startswith("call_") and len(d["tool_calls"][0]["id"]) == 13
    assert d["tool_calls"][0]["function"]["arguments"] == "{}"
    assert d["tool_calls"][1] == {"id": "abc", "type": "function", "function": {"name": "g", "arguments": '{"a":1}'}}


def test_E4_msg_to_dict_none_content():
    d = msg_to_dict(NS(content=None, tool_calls=None))
    assert d == {"role": "assistant", "content": ""}
