"""v1 llm-client R1-R8, E1-E3, example; CORE-2 (constrained output, usage, swap detection); X6 observers."""
import json

import pytest
from tests.core.conftest import make_client
from nai.llm import LLM, JsonlCallLog, json_schema_format
from nai.testing.stub_server import StubServer, call, reply

SAMPLING = {"temperature": 0.6, "top_p": 0.95, "top_k": 20}
MSGS = [{"role": "user", "content": "hi"}]


def mk(stub, stream, **kw):
    return LLM(stub.url, "none", stream=stream, show_thinking=False, **kw)


@pytest.mark.parametrize("stream", [False, True])
def test_R1_R2_request_shape(stream):
    with StubServer(consult=[reply("a")], orch=[reply("b")]) as s:
        llm = mk(s, stream)
        llm.chat("r", MSGS, SAMPLING)
        llm.chat("r", MSGS, SAMPLING, tools=[{"type": "function", "function": {"name": "t", "parameters": {}}}], max_tokens=77)
        a, b = s.requests
        assert (a["temperature"], a["top_p"], a["top_k"]) == (0.6, 0.95, 20)      # example: extra_body flattened by the SDK
        assert "tools" not in a and "tool_choice" not in a and "max_tokens" not in a
        assert b["tool_choice"] == "auto" and b["max_tokens"] == 77 and b["tools"][0]["function"]["name"] == "t"
        assert bool(a.get("stream")) is stream and (not stream or a["stream_options"] == {"include_usage": True})


def test_R3_client_settings(stub):
    llm = LLM(stub.url, "k", timeout=12.5)
    assert llm.client.max_retries == 1 and llm.client.timeout == 12.5


def test_R4_R6_streaming_reply_usage():
    with StubServer(consult=[reply("hello world", reasoning="thinking it over", prompt_tokens=50, completion_tokens=9)]) as s:
        res = mk(s, True).chat("r", MSGS, SAMPLING)
        m, usage, dt = res                                   # v1-style unpacking
        assert m.content == "hello world" and m.reasoning == "thinking it over" and m.tool_calls is None
        assert (usage.prompt_tokens, usage.completion_tokens, usage.estimated) == (50, 9, False)
        assert dt == res.seconds and res.first_token_seconds is not None


def test_R5_tool_calls_merged_by_index_from_fragments():
    rep = reply(None, calls=[call("write_file", path="a.py", content="x" * 50), call("finish", status="success", summary="ok")])
    with StubServer(orch=[rep]) as s:
        m = mk(s, True).chat("r", MSGS, SAMPLING, tools=[{"type": "function", "function": {"name": "t", "parameters": {}}}]).message
        assert m.content == "" and [c.function.name for c in m.tool_calls] == ["write_file", "finish"]
        assert json.loads(m.tool_calls[0].function.arguments) == {"path": "a.py", "content": "x" * 50}
        assert m.tool_calls[0].id == "call_0" and m.tool_calls[1].id == "call_1"


def test_R7_non_streaming_reply_unmodified():
    with StubServer(consult=[reply(None, prompt_tokens=5, completion_tokens=2)]) as s:
        res = mk(s, False).chat("r", MSGS, SAMPLING)
        assert res.message.content is None and res.usage.prompt_tokens == 5       # E2


@pytest.mark.parametrize("stream", [False, True])
def test_R8_E3_missing_usage_is_estimated(stream):
    with StubServer(consult=[reply("x", prompt_tokens=None)]) as s:
        u = mk(s, stream).chat("r", MSGS, SAMPLING).usage
        assert u.estimated and u.prompt_tokens == int(len(json.dumps(MSGS, ensure_ascii=False)) / 3.5)


def test_R8_length_warning_and_speed_line(capsys):
    with StubServer(consult=[reply("x", finish_reason="length", completion_tokens=4)]) as s:
        mk(s, True).chat("r", MSGS, SAMPLING, tag="orch")
    out = capsys.readouterr().out
    assert "[llm] r: output hit max_tokens (truncated)" in out
    assert "[orch] ↳ r: 4 tokens in " in out and "tok/s incl. load/prompt time)" in out


def test_E1_errors_propagate():
    from openai import APIStatusError
    with StubServer(consult=[reply(error=400)]) as s:
        with pytest.raises(APIStatusError):
            mk(s, False).chat("r", MSGS, SAMPLING)


# --- CORE-2 ------------------------------------------------------------------------------
def test_CORE2_response_format_and_grammar_forwarded():
    schema = {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]}
    with StubServer(consult=[reply('{"a": 1}'), reply("{}")]) as s:
        llm = mk(s, False)
        llm.chat("r", MSGS, SAMPLING, response_format=json_schema_format(schema, "t"))
        llm.chat("r", MSGS, SAMPLING, grammar='root ::= "x"')
        a, b = s.requests
        assert a["response_format"] == {"type": "json_schema", "json_schema": {"name": "t", "strict": True, "schema": schema}}
        assert b["grammar"] == 'root ::= "x"' and "response_format" not in b


def test_CORE2_swap_detected_through_running_endpoint():
    with StubServer(consult=[reply("a")] * 3, coder=[reply("c")]) as s:
        llm = mk(s, False)
        first = llm.chat("r", MSGS, SAMPLING)               # nothing loaded -> load
        second = llm.chat("r", MSGS, SAMPLING)              # already loaded
        other = llm.chat("c", MSGS, SAMPLING)               # different model -> swap
        assert [r.swapped for r in (first, second, other)] == [True, False, True]
        assert {r.swap_method for r in (first, second, other)} == {"running"}


def test_CORE2_swap_detected_by_first_token_latency_when_no_endpoint():
    with StubServer(consult=[reply("a"), reply("b")], running_endpoint=False, swap_delay=0.5) as s:
        llm = mk(s, True, swap_threshold=0.3)
        slow = llm.chat("r", MSGS, SAMPLING)                # model had to "load": slow first token
        fast = llm.chat("r", MSGS, SAMPLING)
        assert (slow.swapped, fast.swapped) == (True, False)
        assert slow.swap_method == "latency" and slow.first_token_seconds >= 0.5
        assert fast.first_token_seconds < 0.3


def test_CORE2_swap_unknown_without_endpoint_and_streaming():
    with StubServer(consult=[reply("a")], running_endpoint=False) as s:
        r = mk(s, False).chat("r", MSGS, SAMPLING)
        assert (r.swapped, r.swap_method) == (False, "none")


def test_CORE2_running_probe_backs_off_after_failure():
    with StubServer(consult=[reply("a")] * 2, running_endpoint=False) as s:
        llm = mk(s, False)
        assert llm.running_models() is None and llm._probe_retry_at > 0
        s.running_endpoint = True
        assert llm.running_models() is None                 # still backing off, no request made


# --- X6 ----------------------------------------------------------------------------------
def test_X6_observer_gets_full_record_and_failures(tmp_path):
    with StubServer(consult=[reply("answer", prompt_tokens=7, completion_tokens=3), reply(error=400)]) as s:
        llm = mk(s, False)
        recs = []
        llm.observers += [recs.append, JsonlCallLog(tmp_path / "x" / "calls.jsonl"), lambda r: 1 / 0]   # bad observer is harmless
        llm.chat("r", MSGS, SAMPLING, tag="consult:a", meta={"role": "reasoner", "prompts": ["p@1"]})
        with pytest.raises(Exception):
            llm.chat("r", MSGS, SAMPLING)
    ok, bad = recs
    assert (ok.model, ok.tag, ok.prompt_tokens, ok.completion_tokens, ok.swapped) == ("r", "consult:a", 7, 3, True)
    assert ok.request["messages"] == MSGS and ok.response["content"] == "answer" and ok.meta["prompts"] == ["p@1"]
    assert bad.error and bad.response is None
    lines = (tmp_path / "x" / "calls.jsonl").read_text().splitlines()
    assert len(lines) == 2 and json.loads(lines[0])["response"]["content"] == "answer"


def test_modelclient_records_role_and_uses_profile(stub):
    stub.scripts["coder"] = [reply("done")]
    client = make_client(stub)
    recs = []
    client.llm.observers.append(recs.append)
    client.chat("coder", MSGS, tag="coder")
    req = stub.requests[0]
    assert req["model"] == "c" and req["temperature"] == 0.7 and req["top_p"] == 0.8 and req["repeat_penalty"] == 1.05
    assert req["max_tokens"] == 1000 and recs[0].meta["role"] == "coder"
