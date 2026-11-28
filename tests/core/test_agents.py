"""v1 coder-agent, consult-threads, session-state, orchestrator-loop requirements (R/E ids per component) on ModelClient."""

import pytest

from nai import promptstore, session
from nai.coder import Coder
from nai.consult import ConsultThreads
from nai.orchestrator import Orchestrator
from nai.policy import JobContext, PathPolicy
from nai.testing.stub_server import StubServer, call, reply
from nai.workspace import Workspace
from tests.core.conftest import make_client


def W(tmp_path, **kw):
    return Workspace(tmp_path / "w", git=False, **kw)


# ---------------- coder-agent -----------------------------------------------------------
def test_coder_example_R3_R8(tmp_path):
    with StubServer(coder=[reply(calls=[call("write_file", path="calc.py", content="def add(a,b): return a+b\n")]), reply("Done")]) as s:
        ws = W(tmp_path)
        ws.touched.add("stale")
        out = Coder(make_client(s), ws, 5).run("write add(a, b) in calc.py", [])
    assert out == "Done\n\nFiles written/edited by coder: calc.py"


def test_coder_R1_R2_tools_and_first_messages(tmp_path):
    with StubServer(coder=[reply("x")] * 4) as s:
        ws = W(tmp_path)
        ws.write_file("have.py", "A=1")
        c = Coder(make_client(s), ws, 5, allow_bash=False)
        c.run("task", ["have.py", "new.py", "../../etc/passwd"])
        c2 = Coder(make_client(s), ws, 5, allow_bash=True)
        c2.run("t", [])
    r1, r1b, _r2 = s.on("coder")[0], s.on("coder")[1], s.on("coder")[2]
    assert [t["function"]["name"] for t in r1["tools"]] == ["read_file", "write_file", "edit_file", "list_files"]
    assert "run_command" in [t["function"]["name"] for t in s.on("coder")[2]["tools"]]
    assert r1["messages"][0]["content"] == promptstore.get("coder_system").text
    user = r1["messages"][1]["content"]
    assert user.startswith("## Task\ntask\n\n## Context files (current contents)\n### have.py\n```\nA=1\n```\n")
    assert "### new.py\n```\n(file does not exist yet)\n```" in user
    assert "(cannot read: path escapes workspace: ../../etc/passwd)" in user       # E1
    assert user.endswith("\n## Workspace tree\nhave.py")
    assert r1b["messages"][-1]["content"].startswith("You have not written any files.")     # R5 nudge


def test_coder_R4_unknown_tool_and_bad_args(tmp_path):
    bad = reply(calls=[call("run_command", command="ls"), call("nope")])
    bad["calls"].append({"name": "write_file", "arguments": "{broken"})
    with StubServer(coder=[bad, reply(calls=[call("write_file", path="a", content="1")]), reply("fin")]) as s:
        # the stub JSON-encodes arguments, so a str argument arrives as a JSON string, not an object: invalid for the tool layer
        s.scripts["coder"][0]["calls"][2] = {"name": "write_file", "arguments": "{broken"}
        out = Coder(make_client(s), W(tmp_path), 6).run("t", [])
    tools = [m["content"] for m in s.on("coder")[1]["messages"] if m["role"] == "tool"]
    assert tools[0] == "ERROR: unknown tool 'run_command'. Available: edit_file, list_files, read_file, write_file"
    assert tools[1].startswith("ERROR: unknown tool 'nope'")
    assert out.endswith("Files written/edited by coder: a")


def test_coder_E2_nudged_once_then_ends(tmp_path):
    with StubServer(coder=[reply("here is code"), reply("still just text")]) as s:
        out = Coder(make_client(s), W(tmp_path), 5).run("t", [])
    assert out == "still just text\n\nFiles written/edited by coder: none" and len(s.on("coder")) == 2


def test_coder_R5_empty_summary_R7_step_limit_R6_context(tmp_path):
    with StubServer(coder=[reply(calls=[call("write_file", path="a", content="1")]), reply("")]) as s:
        assert Coder(make_client(s), W(tmp_path), 5).run("t", []).startswith("(no summary)")
    with StubServer(coder=[reply(calls=[call("list_files")])] * 3) as s:
        assert Coder(make_client(s), W(tmp_path), 3).run("t", []).startswith("[coder hit its step limit]")
    with StubServer(coder=[reply(calls=[call("write_file", path="a", content="1")], prompt_tokens=950)]) as s:
        out = Coder(make_client(s, ctx=1000), W(tmp_path), 5).run("t", [])
        assert out.startswith("[coder stopped: context nearly full; task may be incomplete, split it into smaller tasks]")
        assert len(s.on("coder")) == 1


def test_coder_E3_server_error_propagates(tmp_path):
    from openai import APIStatusError
    with StubServer(coder=[reply(error=400)]) as s:
        with pytest.raises(APIStatusError):
            Coder(make_client(s), W(tmp_path), 5).run("t", [])


def test_coder_respects_job_path_policy(tmp_path):
    script = [reply(calls=[call("write_file", path="tests/t.py", content="x"), call("write_file", path="src/a.py", content="y")]), reply("ok")]
    with StubServer(coder=script) as s:
        ws = W(tmp_path)
        with ws.job_context(JobContext("n", "implement", policy=PathPolicy(writable=("src/**",), readonly=("tests/**",)))):
            out = Coder(make_client(s), ws, 5).run("t", [])
    tools = [m["content"] for m in s.on("coder")[1]["messages"] if m["role"] == "tool"]
    assert tools[0] == "ERROR: path not writable for this job: tests/t.py" and tools[1].startswith("wrote src/a.py")
    assert out.endswith("coder: src/a.py")


def test_coder_extra_tools_dispatch(tmp_path):
    extra = {"type": "function", "function": {"name": "make_pdf", "description": "d", "parameters": {"type": "object", "properties": {}}}}
    with StubServer(coder=[reply(calls=[call("make_pdf", title="T")]), reply("ok"), reply("ok")]) as s:
        seen = []
        Coder(make_client(s), W(tmp_path), 5, extra_tools=[extra], tool_runner=lambda n, a: seen.append((n, a)) or "made x.pdf").run("t", [])
    assert seen == [("make_pdf", {"title": "T"})]
    assert [m["content"] for m in s.on("coder")[1]["messages"] if m["role"] == "tool"] == ["made x.pdf"]


# ---------------- consult-threads ----------------------------------------------------------
def test_consult_example_R1_R3_R4_E2(tmp_path):
    with StubServer(consult=[reply("<think>hm</think>first"), reply("")]) as s:
        ws = W(tmp_path)
        ct = ConsultThreads(make_client(s), ws)
        assert ct.consult("architect", "design?") == "first"
        assert ct.consult("architect", "again?") == "(empty response)"
    second = s.on("consult")[1]["messages"]
    assert [m["role"] for m in second] == ["system", "user", "assistant", "user"] and second[2]["content"] == "first"
    assert second[0]["content"] == promptstore.get("consult_system").text
    assert "tools" not in s.on("consult")[0] and s.on("consult")[0]["max_tokens"] == 1000 and s.on("consult")[0]["model"] == "r"


def test_consult_R1_reset_E1_and_R2_files(tmp_path):
    with StubServer(consult=[reply("a")] * 3) as s:
        ws = W(tmp_path)
        ws.write_file("x.py", "X=1")
        ct = ConsultThreads(make_client(s), ws)
        ct.consult("t", "m1", ["x.py", "nofile.py", "../../etc/passwd"])
        ct.consult("t", "m2", reset=True)
    first = s.on("consult")[0]["messages"][1]["content"]
    assert first.startswith("m1\n### x.py\n```\nX=1\n```\n")
    assert "(file does not exist)" in first and "(cannot read: path escapes workspace: ../../etc/passwd)" in first
    assert len(s.on("consult")[1]["messages"]) == 2


def test_consult_R5_prunes_oldest_exchanges(tmp_path):
    with StubServer(consult=[reply(f"a{i}", prompt_tokens=100) for i in range(3)] + [reply("a3", prompt_tokens=900)]) as s:
        ct = ConsultThreads(make_client(s, ctx=1000), W(tmp_path))
        for i in range(4):
            ct.consult("t", f"q{i}")
    conv = ct.convos["t"]       # system + 4 exchanges = 9 messages; after the pruning call: system + q2,a2,q3,a3
    assert [m["content"] for m in conv[1:]] == ["q2", "a2", "q3", "a3"] and conv[0]["role"] == "system"


# ---------------- session-state ---------------------------------------------------------------
def test_session_R1_R2_spec_message():
    assert session.spec_message("Build X", []).startswith("# SPECIFICATION\n\nBuild X\n\n---\nThe spec is also saved")
    m = session.spec_message("S", ["pytest -q", "npm run build"])
    assert "\n\nREQUIRED VERIFICATION: before finish(success), each of these commands must have exited 0 after your last file change:\n- pytest -q\n- npm run build\n\n---\n" in m
    msgs = session.initial_messages("Build X", [])
    assert [m["role"] for m in msgs] == ["system", "user"] and msgs[0]["content"] == promptstore.get("orch_system").text


def test_session_R3_R4_E1_E3_save_load(tmp_path, capsys):
    p = tmp_path / "state.json"
    session.save(p, "spec", [{"role": "user", "content": "é"}], {"c": []}, 3)
    assert not p.with_suffix(".tmp").exists()
    assert session.load(p) == ("spec", [{"role": "user", "content": "é"}], {"c": []}, 3)
    assert "[sys] resumed at step 3 with 1 messages" in capsys.readouterr().out
    with pytest.raises(FileNotFoundError):
        session.load(tmp_path / "missing.json")


def test_session_R5_plan_and_R6_R7_compact(tmp_path):
    ws = W(tmp_path, test_cmds=["pytest"])
    assert session.write_plan(ws.root, "- [ ] a") == "plan saved" and (ws.root / ".agent/plan.md").read_text() == "- [ ] a"
    ws.write_file("a.py", "1")
    msgs = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "old"}]
    with StubServer(orch=[], consult=[reply("<think>x</think>SUMMARY")]) as s:
        out = session.compact(make_client(s), msgs, "SPEC", ws, 777)
    req = s.on("consult")[0]
    assert req["messages"][-1] == {"role": "user", "content": promptstore.get("compact").text} and "tools" not in req
    assert len(out) == 2 and out[0] == msgs[0]
    u = out[1]["content"]
    assert u.startswith(session.spec_message("SPEC", ["pytest"]) + "\n\n---\nCONTEXT RESET. Your own summary of progress so far:\nSUMMARY")
    assert u.endswith("Current plan (.agent/plan.md):\n- [ ] a\n\nCurrent file tree:\na.py\n\nContinue the work. Re-read files as needed.")


def test_session_R6_blank_summary_and_no_plan(tmp_path):
    ws = W(tmp_path)
    with StubServer(consult=[reply("  ")]) as s:
        u = session.compact(make_client(s), [{"role": "system", "content": "S"}], "SP", ws, 1)[1]["content"]
    assert "(no summary produced)" in u and "(no plan written yet)" in u


# ---------------- orchestrator-loop ---------------------------------------------------------------
def orch_for(tmp_path, s, **kw):
    client = make_client(s, **{k: kw.pop(k) for k in ("ctx",) if k in kw})
    ws = Workspace(tmp_path / "w", git=False)
    coder = Coder(client, ws, 10)
    return Orchestrator(client, ws, coder, **kw), ws


def test_orch_example_five_steps(tmp_path):
    script = [reply(calls=[call("consult_reasoner", conversation_id="architect", message="design?")]),
              reply(calls=[call("delegate_to_coder", task="make hello", context_files=["nope.py"])]),
              reply(calls=[call("finish", status="success", summary="too early")]),
              reply(calls=[call("run_command", command="echo '1 passed' # pytest -q")]),
              reply(calls=[call("finish", status="success", summary="verified")])]
    with StubServer(orch=script, consult=[reply("Use one module.")],
                    coder=[reply(calls=[call("write_file", path="hello.py", content="x=1\n")]), reply("Done.")]) as s:
        o, ws = orch_for(tmp_path, s)
        o.start("SPEC")
        assert o.run() == ("success", "verified") and o.step == 5
    tool_msgs = [m["content"] for m in s.on("orch")[-1]["messages"] if m["role"] == "tool"]
    assert tool_msgs[0] == "Use one module." and "Files written/edited by coder: hello.py" in tool_msgs[1]
    assert tool_msgs[2].startswith("REJECTED: No passing verification since your last file change.")
    assert tool_msgs[2].endswith("If the project genuinely cannot be tested, call finish with status 'partial' and explain why.")
    assert [t["function"]["name"] for t in s.on("orch")[0]["tools"]] == \
        ["read_file", "write_file", "edit_file", "list_files", "run_command", "delegate_to_coder", "consult_reasoner", "update_plan", "finish"]


def test_orch_R4_nudges_then_blocked_and_reset(tmp_path):
    with StubServer(orch=[reply("thinking")] * 4) as s:
        o, _ = orch_for(tmp_path, s)
        o.start("S")
        assert o.run() == ("blocked", "Orchestrator stopped issuing tool calls.")
        assert len(s.on("orch")) == 4 and s.on("orch")[1]["messages"][-1]["content"].startswith("Continue: call a tool")
    with StubServer(orch=[reply("t"), reply(calls=[call("update_plan", content="p")]), reply("t"), reply("t"), reply("t"),
                          reply(calls=[call("finish", status="blocked", summary="x")])]) as s:
        o, ws = orch_for(tmp_path, s)
        o.start("S")
        assert o.run() == ("blocked", "x") and (ws.root / ".agent/plan.md").read_text() == "p"      # the counter was reset


def test_orch_R5_handle_errors(tmp_path):
    with StubServer() as s:
        o, ws = orch_for(tmp_path, s)
        def h(name, args):
            return o.handle({"id": "1", "function": {"name": name, "arguments": args}})
        assert h("read_file", "[1]") == "ERROR: tool arguments were not a valid JSON object; retry"
        assert h("finish", '{"nope": 1}').startswith("ERROR: bad arguments for finish:")
        assert h("whatever", "{}") == "ERROR: unknown tool 'whatever'"                  # E3
        assert h("read_file", '{"path": "../x"}') == "ERROR: ValueError: path escapes workspace: ../x"
        assert h("update_plan", '{"content": "c"}') == "plan saved"


def test_orch_R6_delegate_checkpoint_message(tmp_path):
    with StubServer(coder=[reply("ok")] * 4) as s:
        o, ws = orch_for(tmp_path, s)
        msgs = []
        ws.checkpoint = msgs.append
        o.delegate("  first line that is long " + "x" * 100 + "\nsecond")
        o.delegate("  ")
    assert msgs[0].startswith("coder: first line that is long") and len(msgs[0]) == len("coder: ") + 80 and msgs[1] == "coder: task"


def test_orch_R7_finish_downgrade_on_sixth(tmp_path):
    with StubServer(orch=[reply(calls=[call("finish", status="success", summary="done")])], repeat_last=["orch"]) as s:
        o, _ = orch_for(tmp_path, s, max_steps=20)
        o.start("S")
        status, summary = o.run()
    assert status == "partial" and summary == "[downgraded from 'success': could not be verified] done" and o.step == 6
    results = [m["content"] for m in s.on("orch")[-1]["messages"] if m["role"] == "tool"]
    assert len(results) == 5 and all(r.startswith("REJECTED:") for r in results)


def test_orch_R2_R8_compaction_and_max_steps_E1(tmp_path):
    script = [reply(calls=[call("list_files")], prompt_tokens=800), reply(calls=[call("list_files")], prompt_tokens=100)]
    with StubServer(orch=script, consult=[reply("SUM")]) as s:
        o, ws = orch_for(tmp_path, s, ctx=1000, max_steps=2)
        o.start("S")
        assert o.run() == ("partial", "Stopped after reaching max steps (2). Use --resume to continue.")
        assert len(s.on("consult")) == 1                                          # compaction used the no-tools call
        assert len(s.on("orch")[1]["messages"]) == 2                              # reset to system + one user message
    # E1: resume with a stored step >= max_steps ends at once
    with StubServer() as s2:
        o2, _ = orch_for(tmp_path, s2, max_steps=2)
        o2.load()
        assert o2.step == 2 and o2.run()[1].startswith("Stopped after reaching max steps (2)") and s2.requests == []


def test_orch_E4_server_error_propagates_without_saving_step(tmp_path):
    from openai import APIStatusError
    with StubServer(orch=[reply(error=400)]) as s:
        o, ws = orch_for(tmp_path, s)
        o.start("S")
        with pytest.raises(APIStatusError):
            o.run()
    assert not (ws.root / ".agent/state.json").exists()


def test_orch_prompt_versions_recorded_on_calls(tmp_path):
    with StubServer(orch=[reply(calls=[call("finish", status="blocked", summary="x")])]) as s:
        o, _ = orch_for(tmp_path, s)
        recs = []
        o.client.llm.observers.append(recs.append)
        o.start("S")
        o.run()
    assert recs[0].meta["prompts"] == ["orch_system@1"] and recs[0].meta["role"] == "reasoner"


# ---------------- prompts (X10) ---------------------------------------------------------------------
@pytest.mark.parametrize("name,needles", [
    ("orch_system", ["delegate_to_coder", "consult_reasoner", "update_plan", "non-interactive", "'success' is only accepted", "requirement ids", "R1, E1"]),
    ("coder_system", ["no placeholders", "outside the scope", "WITHOUT a tool call"]),
    ("consult_system", ["no tools", "decision-oriented"]),
    ("compact", ["consultant conversation ids", "do not call any tools"]),
])
def test_prompts_versioned_and_keep_v1_commitments(name, needles):
    p = promptstore.get(name)
    assert p.version == "1" and p.id == f"{name}@1"
    for n in needles:
        assert n in p.text


def test_prompts_match_v1_source_verbatim():
    import re
    src = open("/mnt/user-data/uploads/devswarm.py").read() if __import__("os").path.exists("/mnt/user-data/uploads/devswarm.py") else None
    if src is None:
        pytest.skip("v1 source not available")
    for const, name in (("ORCH_SYSTEM", "orch_system"), ("CODER_SYSTEM", "coder_system"), ("CONSULT_SYSTEM", "consult_system"), ("COMPACT_PROMPT", "compact")):
        body = re.search(rf'^{const} = """(.*?)"""', src, re.S | re.M).group(1)
        assert promptstore.get(name).text == body


def test_prompt_header_errors(tmp_path, monkeypatch):
    from nai import promptstore as ps
    monkeypatch.setitem(ps._CACHE, "x", ps.Prompt("x", "2", "t"))
    assert ps.get("x").id == "x@2"
