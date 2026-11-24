"""End-to-end Python pipeline: imported spec -> spec -> stubs -> tests -> implement -> verify -> done.

This is the contract test that later refactors must keep green. It uses the real
Store, Runner, spec/build plugins, workspace, and the channel-aware stub model
server from nai.testing.stub_server.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from nai.build.plugin import register as register_build
from nai.config import Config
from nai.models import ModelProfile, RoleConfig
from nai.sched import Runner, SchedConfig
from nai.spec.plugin import register as register_spec
from nai.store import Store
from nai.testing.stub_server import StubServer, call, reply

IMPORTED_SPEC = """# Tiny Library

Build a two-component Python library:

- `text-normalizer` provides `normalize_text(text: str) -> str`.
- `word-counter` provides `count_words(text: str) -> int`.
- Integration scenario S1: normalize text, then count its words.
"""


def _config() -> Config:
    cfg = Config()
    cfg.profiles["stub"] = ModelProfile(
        "stub",
        RoleConfig("r", ctx=4096, max_tokens=512, temperature=0, top_p=1, top_k=None),
        RoleConfig("c", ctx=4096, max_tokens=512, temperature=0, top_p=1, top_k=None),
    )
    cfg.default_profile = "stub"
    return cfg


def _structured(payload: dict) -> str:
    return json.dumps(payload)


def _decompose() -> str:
    return _structured({
        "components": [
            {"name": "text-normalizer", "objective": "Normalize whitespace in text", "depends_on": []},
            {"name": "word-counter", "objective": "Count words in text", "depends_on": []},
        ],
        "shared_entities": [{"name": "Input", "description": "Text input", "fields": [
            {"name": "text", "type": "str", "description": "Raw text"}
        ]}],
        "external_inputs": [{"name": "text", "description": "Raw text", "source": "user"}],
        "external_outputs": [{"name": "count", "description": "Word count", "destination": "user"}],
    })


def _contract() -> str:
    return _structured({
        "contract": {
            "entities": [{"name": "Input", "fields": [{"name": "text", "type": "str"}]}],
            "components": [
                {"name": "text-normalizer", "provides": [{
                    "name": "normalize_text", "signature": "normalize_text(text: str) -> str",
                    "description": "Collapse whitespace"
                }], "consumes": []},
                {"name": "word-counter", "provides": [{
                    "name": "count_words", "signature": "count_words(text: str) -> int",
                    "description": "Count whitespace-separated words"
                }], "consumes": []},
            ],
            "scenarios": [{
                "id": "S1", "title": "Normalize then count",
                "steps": [{"caller": "user", "callee": "word-counter", "call": "count_words", "fields": ["Input.text"]}],
            }],
        }
    })


def _component_spec(name: str, req: str, marker: str) -> str:
    return _structured({
        "spec_markdown": (
            f"# Component: {name}\n## Purpose\n{req}\n## Interface\nNone\n"
            f"## Requirements\n- {marker}: {req}\n## Edge Cases\n- E1: Empty text\n"
            f"## Example\nInput: 'a  b'\nOutput: '{marker}'\n## Dependencies\n"
        ),
        "requirements": [{"id": marker, "text": req, "testable": True}],
        "edge_cases": [{"id": "E1", "text": "Empty text"}],
        "example": "Input 'a  b' produces two normalized tokens",
        "dependencies": [],
    })


def _tests_payload(path: str, code: str, req_id: str) -> str:
    return _structured({
        "file_path": path,
        "code": code,
        "covered_requirements": [req_id],
    })


def _source_script() -> list:
    return [
        reply(calls=[call("write_file", path="src/tiny_lib/text_normalizer.py", content=(
            "def normalize_text(text: str) -> str:\n"
            "    return ' '.join(text.strip().split())\n"
        ))]),
        reply("Implemented normalizer"),
        reply(calls=[call("write_file", path="src/tiny_lib/word_counter.py", content=(
            "def count_words(text: str) -> int:\n"
            "    return len(text.split())\n"
        ))]),
        reply("Implemented counter"),
        reply(calls=[call("write_file", path="src/tiny_lib/root_integration.py", content=(
            "from .text_normalizer import normalize_text\n"
            "from .word_counter import count_words\n\n\n"
            "def integrate(text: str) -> int:\n"
            "    return count_words(normalize_text(text))\n"
        ))]),
        reply("Implemented integration wiring"),
    ]



def test_python_library_pipeline_imported_spec_to_done(tmp_path: Path):
    reasoner = [
        reply(_decompose()),
        reply(_contract()),
        reply(_component_spec("text-normalizer", "Normalize whitespace in text", "R1")),
        reply(_component_spec("word-counter", "Count words in text", "R1")),
        reply(_tests_payload(
            "tests/test_text_normalizer.py",
            "from tiny_lib.text_normalizer import normalize_text\n\n\n"
            "def test_R1_normalize_text():\n    assert normalize_text(' a  b ') == 'a b'\n",
            "R1",
        )),
        reply(_tests_payload(
            "tests/test_word_counter.py",
            "from tiny_lib.word_counter import count_words\n\n\n"
            "def test_R1_count_words():\n    assert count_words('a  b') == 2\n",
            "R1",
        )),
        reply(_tests_payload(
            "tests/test_integration.py",
            "from tiny_lib.root_integration import integrate\n\n\n"
            "def test_S1_pipeline():\n    assert integrate(' a  b ') == 2\n",
            "S1",
        )),
    ]

    with StubServer(consult=reasoner, coder=_source_script(), repeat_last=("consult", "coder")) as stub:
        from nai.llm import LLM

        db = tmp_path / ".nai" / "nai.sqlite3"
        db.parent.mkdir(parents=True, exist_ok=True)
        store = Store(db, read_only=False)
        pid = store.create_project(
            "Tiny Library", "Imported spec", workspace_path=tmp_path / "ws",
            settings={
                "model_profile": "stub",
                "package_name": "tiny_lib",
                "intake_complete": True,
                "clarified": True,
                "target_env": {
                    "type_checker": "none",
                    "packages": ["pytest==9.2.0"],
                    "lint_rules": [],
                },
            },
        )
        store.create_node(pid, "root", "system", spec_text=IMPORTED_SPEC, objective="Tiny library root")
        integration_stub = tmp_path / "ws" / "src" / "tiny_lib" / "root_integration.py"
        integration_stub.parent.mkdir(parents=True, exist_ok=True)
        integration_stub.write_text(
            "def integrate(text: str) -> int:\n"
            "    raise NotImplementedError('integration stub')\n",
            encoding="utf-8",
        )
        store.close()

        llm = LLM(stub.url, "none", stream=False, show_thinking=False)

        def make_runner() -> Runner:
            runner = Runner(
                Store(db, read_only=False), llm, app_config=_config(),
                sched=SchedConfig(threaded=False, poll_seconds=0.01, plan_interval_seconds=0.01),
                state_dir=tmp_path / ".nai",
            )
            register_spec(runner)
            register_build(runner)
            return runner

        runner = make_runner()
        runner.run(drain=True, max_steps=200)
        runner.close()

        with Store(db, read_only=True) as check:
            children = check.list_nodes(pid, parent_id=check.list_nodes(pid, parent_id=None)[0]["id"])
            assert {c["name"] for c in children} == {"text-normalizer", "word-counter"}, (
                [(j["kind"], j["status"], j.get("error")) for j in check.list_jobs(pid, limit=100)],
                [e["type"] for e in check.list_events(pid, limit=100)],
            )
            assert all(c["status"] == "awaiting_approval" for c in children), (
                [(c["name"], c["status"]) for c in children],
                [j.get("error") for j in check.list_jobs(pid, limit=100) if j.get("error")],
            )

        with Store(db, read_only=False) as writable:
            root = writable.list_nodes(pid, parent_id=None)[0]
            if root["status"] == "draft":
                writable.transition_node_status(root["id"], "awaiting_approval", note="imported spec review")
            writable.enqueue_command(pid, "approve", node_id=root["id"], payload={"stage": "interfaces"})
            for child in children:
                writable.enqueue_command(pid, "approve", node_id=child["id"], payload={"stage": "component-spec"})

        runner = make_runner()
        runner.run(drain=True, max_steps=500)
        runner.close()

        with Store(db, read_only=True) as check:
            project = check.get_project(pid)
            nodes = []
            frontier = [None]
            while frontier:
                parent = frontier.pop(0)
                kids = check.list_nodes(pid, parent_id=parent)
                nodes.extend(kids)
                frontier.extend(k["id"] for k in kids)
            by_name = {n["name"]: n for n in nodes}

            assert project["status"] == "done", (
                [(n["name"], n["status"]) for n in nodes],
                [(j["kind"], str(j.get("error"))[:1500]) for j in check.list_jobs(pid, limit=100) if j.get("error")],
            )
            assert by_name["root"]["status"] == "done"
            assert by_name["text-normalizer"]["status"] == "done"
            assert by_name["word-counter"]["status"] == "done"
            assert any(n["kind"] == "integration" and n["status"] == "done" for n in nodes)

            jobs = check.list_jobs(pid, limit=10000)
            succeeded_kinds = {j["kind"] for j in jobs if j["status"] == "succeeded"}
            assert {"decompose", "interfaces", "component_spec", "contract_stub", "author_tests",
                    "implement", "verify", "integrate"} <= succeeded_kinds

            events = check.list_events(pid, limit=10000)
            assert [e for e in events if e["type"] == "planner.error"] == []

            workspace = Path(project["workspace_path"])
            for rel in (
                "src/tiny_lib/__init__.py",
                "src/tiny_lib/text_normalizer.py",
                "src/tiny_lib/word_counter.py",
                "src/tiny_lib/root_integration.py",
                "tests/test_text_normalizer.py",
                "tests/test_word_counter.py",
                "tests/test_integration.py",
            ):
                assert (workspace / rel).is_file(), rel

            integration_id = next(n["id"] for n in nodes if n["kind"] == "integration")
            for node_id in [by_name["text-normalizer"]["id"], by_name["word-counter"]["id"], integration_id]:
                records = check.list_test_records(node_id)
                assert records
                for record in records:
                    content = (workspace / record["file_path"]).read_bytes()
                    assert hashlib.sha1(content).hexdigest() == record["frozen_hash"]
                    assert record["last_result"] == "pass"
