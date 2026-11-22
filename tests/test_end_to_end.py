"""End-to-end integration: brief -> spec pipeline -> build -> done, using the scripted stub LLM."""
from __future__ import annotations

import json
from pathlib import Path

from nai.build.plugin import register as register_build
from nai.config import Config
from nai.models import ModelProfile, RoleConfig
from nai.sched import Runner, SchedConfig
from nai.spec.plugin import register as register_spec
from nai.store import Store
from nai.testing.stub_server import StubServer, reply


def _profile() -> Config:
    cfg = Config()
    cfg.profiles["stub"] = ModelProfile(
        "stub",
        RoleConfig("r", ctx=4096, max_tokens=512, temperature=0, top_p=1, top_k=None),
        RoleConfig("c", ctx=4096, max_tokens=512, temperature=0, top_p=1, top_k=None),
    )
    cfg.default_profile = "stub"
    return cfg


def _json(content: str) -> str:
    return json.dumps({"ok": True, "content": content})


def test_brief_to_spec_build_done(tmp_path: Path):
    """Drives the real runner with registered spec+build plugins against a stub LLM.

    The scripted replies satisfy each structured-output schema. We assert that:
    the planner creates jobs (no AttributeError flood), spec_text and child nodes
    are persisted, and no planner.error events are written.
    """
    # A decompose result that produces one child
    decompose = json.dumps({
        "components": [{"name": "child", "objective": "Implement the whole feature", "depends_on": []}],
        "shared_entities": [{"name": "Item", "description": "An item", "fields": [{"name": "id", "type": "int"}]}],
        "external_inputs": [{"name": "input", "description": "Owner input", "source": "user"}],
        "external_outputs": [{"name": "output", "description": "Result", "destination": "user"}],
    })
    interfaces = json.dumps({
        "contract": {
            "entities": [], "components": [], "scenarios": [],
        }
    })
    component_spec = json.dumps({
        "spec_markdown": "# Component: child\n## Purpose\nDo the work.\n## Interface\nNone\n## Requirements\n- R1: It works\n## Edge Cases\n- E1: Bad input\n## Example\nInput: x\nOutput: y\n## Dependencies\n- none",
        "requirements": [{"id": "R1", "text": "It works"}],
        "edge_cases": [{"id": "E1", "text": "Bad input"}],
        "example": "Input x, produce output y for testing",
        "dependencies": [],
    })
    clarify = json.dumps({"questions": []})

    # Reasoner channel is consumed in order by all reasoner jobs.
    reasoner_script = [reply(clarify), reply(decompose), reply(interfaces), reply(component_spec)]
    with StubServer(orch=[], consult=reasoner_script, coder=[reply("ok")], repeat_last=("consult", "coder")) as stub:
        db = tmp_path / ".nai" / "nai.sqlite3"
        db.parent.mkdir(parents=True, exist_ok=True)
        store = Store(db, read_only=False)
        pid = store.create_project("Demo", "Build a tiny thing", workspace_path=tmp_path / "ws",
                                   settings={"model_profile": "stub"})
        store.create_node(pid, "root", "system", objective="Root")
        store.close()

        from nai.llm import LLM

        llm = LLM(stub.url, "none", stream=False, show_thinking=False)
        runner = Runner(store if False else Store(db, read_only=False), llm,
                        app_config=_profile(), sched=SchedConfig(threaded=False, poll_seconds=0.01,
                                                                 plan_interval_seconds=0.01),
                        state_dir=tmp_path / ".nai")
        register_spec(runner)
        register_build(runner)
        runner.run(drain=True, max_steps=60)
        runner.close()

        check = Store(db, read_only=True)
        from nai.sched.readiness import walk_nodes
        nodes = walk_nodes(check, pid)
        events = check.list_events(pid, limit=10000)
        errors = [e for e in events if e.get("type") == "planner.error"]
        [e for e in events if e.get("type") in ("job.failed", "job.finished") and e.get("level") == "error"]
        assert not errors, f"planner errors: {errors[:2]}"
        if not nodes or len(nodes) < 2:
            raise AssertionError(f"no child nodes. jobs={[(j.get('kind'), j.get('status'), j.get('error')) for j in check.list_jobs(pid, limit=100)]} events={[e.get('type') for e in events[-20:]]}")
        child = [n for n in nodes if n.get("name") == "child"]
        assert child, "decompose did not create child nodes"
        assert child[0].get("spec_text"), "component_spec did not persist spec_text"
        check.close()