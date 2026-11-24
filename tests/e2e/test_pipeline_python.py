"""End-to-end Python pipeline: imported spec -> spec -> stubs -> tests -> implement -> verify -> done.

This is the contract test that later refactors must keep green. It uses the real
Store, Runner, spec/build plugins, workspace, and the channel-aware stub model
server from nai.testing.stub_server.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
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

NORMALIZER_SOURCE = (
    "def normalize_text(text: str) -> str:\n"
    "    return ' '.join(text.strip().split())\n"
)
WORD_COUNTER_SOURCE = (
    "def count_words(text: str) -> int:\n"
    "    return len(text.split())\n"
)
INTEGRATION_SOURCE = (
    "from .text_normalizer import normalize_text\n"
    "from .word_counter import count_words\n\n\n"
    "def integrate(text: str) -> int:\n"
    "    return count_words(normalize_text(text))\n"
)

NORMALIZER_TEST = (
    "from tiny_lib.text_normalizer import normalize_text\n\n\n"
    "def test_R1_normalize_text():\n    assert normalize_text(' a  b ') == 'a b'\n"
)
WORD_COUNTER_TEST = (
    "from tiny_lib.word_counter import count_words\n\n\n"
    "def test_R1_count_words():\n    assert count_words('a  b') == 2\n"
)
INTEGRATION_TEST = (
    "from tiny_lib.root_integration import integrate\n\n\n"
    "def test_S1_pipeline():\n    assert integrate(' a  b ') == 2\n"
)


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
        reply(calls=[call("write_file", path="src/tiny_lib/text_normalizer.py", content=NORMALIZER_SOURCE)]),
        reply("Implemented normalizer"),
        reply(calls=[call("write_file", path="src/tiny_lib/word_counter.py", content=WORD_COUNTER_SOURCE)]),
        reply("Implemented counter"),
        reply(calls=[call("write_file", path="src/tiny_lib/root_integration.py", content=INTEGRATION_SOURCE)]),
        reply("Implemented integration wiring"),
    ]



def _all_nodes(store: Store, project_id: str) -> list[dict]:
    """Breadth-first traversal of the project tree."""
    nodes: list[dict] = []
    frontier: list[str | None] = [None]
    while frontier:
        parent = frontier.pop(0)
        kids = store.list_nodes(project_id, parent_id=parent)
        nodes.extend(kids)
        frontier.extend(k["id"] for k in kids)
    return nodes


def test_python_library_pipeline_imported_spec_to_done(tmp_path: Path):
    reasoner = [
        reply(_decompose()),
        reply(_contract()),
        reply(_component_spec("text-normalizer", "Normalize whitespace in text", "R1")),
        reply(_component_spec("word-counter", "Count words in text", "R1")),
        reply(_tests_payload("tests/test_text_normalizer.py", NORMALIZER_TEST, "R1")),
        reply(_tests_payload("tests/test_word_counter.py", WORD_COUNTER_TEST, "R1")),
        reply(_tests_payload("tests/test_integration.py", INTEGRATION_TEST, "S1")),
        # D6: handle_integrate opens one reasoner consult before running the coder.
        reply("Integration wiring reviewed for scenario S1."),
    ]

    # No repeat_last: every scripted reply must be consumed, so a missing stage
    # cannot silently reuse an earlier response.
    with StubServer(consult=reasoner, coder=_source_script()) as stub:
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
            nodes = _all_nodes(check, pid)
            by_name = {n["name"]: n for n in nodes}

            assert project["status"] == "done", (
                [(n["name"], n["status"]) for n in nodes],
                [(j["kind"], str(j.get("error"))[:1500]) for j in check.list_jobs(pid, limit=100) if j.get("error")],
            )

            # Final node statuses: the whole tree, not just the named components.
            assert {n["name"] for n in nodes} == {
                "root", "text-normalizer", "word-counter", "root-integration",
            }, [(n["name"], n["kind"], n["status"]) for n in nodes]
            assert all(n["status"] == "done" for n in nodes), [
                (n["name"], n["status"]) for n in nodes
            ]
            assert by_name["root"]["kind"] == "system"
            assert by_name["text-normalizer"]["kind"] == "component"
            assert by_name["word-counter"]["kind"] == "component"
            integration = by_name["root-integration"]
            assert integration["kind"] == "integration"
            assert integration["parent_id"] == by_name["root"]["id"]
            assert set(integration["depends_on"]) == {
                by_name["text-normalizer"]["id"], by_name["word-counter"]["id"],
            }
            # BLD-10: every done node was built from the content it currently has.
            for node in nodes:
                assert node["built_hash"] == node["content_hash"], node["name"]

            jobs = check.list_jobs(pid, limit=10000)
            not_succeeded = [
                (j["kind"], j["status"], str(j.get("error"))[:500])
                for j in jobs
                if j["status"] != "succeeded"
            ]
            assert not_succeeded == [], not_succeeded
            succeeded_kinds = {j["kind"] for j in jobs}
            assert {"decompose", "interfaces", "component_spec", "contract_stub", "author_tests",
                    "implement", "verify", "integrate"} <= succeeded_kinds

            events = check.list_events(pid, limit=10000)
            assert [e for e in events if e["type"] == "planner.error"] == []
            assert [e for e in events if e["type"] in {"job.failed", "node.failed", "build.verify_failed"}] == []
            assert any(e["type"] == "project.done" for e in events)

            workspace = Path(project["workspace_path"])
            expected_sources = {
                "src/tiny_lib/text_normalizer.py": NORMALIZER_SOURCE,
                "src/tiny_lib/word_counter.py": WORD_COUNTER_SOURCE,
                "src/tiny_lib/root_integration.py": INTEGRATION_SOURCE,
            }
            for rel, expected in expected_sources.items():
                assert (workspace / rel).read_text(encoding="utf-8") == expected, rel
            assert (workspace / "src/tiny_lib/__init__.py").is_file()
            assert (workspace / "src/tiny_lib/entities.py").is_file()

            expected_tests = {
                "tests/test_text_normalizer.py": (NORMALIZER_TEST, "R1"),
                "tests/test_word_counter.py": (WORD_COUNTER_TEST, "R1"),
                "tests/test_integration.py": (INTEGRATION_TEST, "S1"),
            }
            records_by_path = {
                record["file_path"]: (node, record)
                for node in nodes
                for record in check.list_test_records(node["id"])
            }
            assert set(records_by_path) == set(expected_tests), sorted(records_by_path)
            for rel, (authored, req_id) in expected_tests.items():
                _node, record = records_by_path[rel]
                expected_hash = hashlib.sha1(authored.encode("utf-8")).hexdigest()
                assert record["frozen_hash"] == expected_hash, rel
                assert record["last_result"] == "pass", rel
                assert req_id in record["requirement_ids"], (rel, record["requirement_ids"])
                # Frozen tests unchanged: on-disk bytes still match both the
                # authored source and the hash recorded at authoring time.
                on_disk = (workspace / rel).read_text(encoding="utf-8")
                assert on_disk == authored, rel
                assert hashlib.sha1(on_disk.encode("utf-8")).hexdigest() == record["frozen_hash"], rel
                if os.name != "nt":
                    mode = stat.S_IMODE((workspace / rel).stat().st_mode)
                    assert mode & 0o222 == 0, (rel, oct(mode))

            # Every scripted model reply was consumed: no stage was skipped and
            # no unexpected call silently reused an earlier response.
            assert stub.remaining("consult") == 0
            assert stub.remaining("coder") == 0
