"""End-to-end Swift pipeline: imported spec -> spec -> stubs -> tests -> implement -> verify -> done.

Uses the real Store, Runner, spec/build plugins, workspace and the scripted stub
model server.  Skipped unless a Swift toolchain is installed, because the whole
point is that SwiftPM and XCTest accept the generated package; CI installs
Swift in its own job.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import stat
from pathlib import Path

import pytest

from nai.build.plugin import register as register_build
from nai.config import Config
from nai.models import ModelProfile, RoleConfig
from nai.sched import Runner, SchedConfig
from nai.spec.plugin import register as register_spec
from nai.store import Store
from nai.testing.stub_server import StubServer, call, reply

pytestmark = pytest.mark.skipif(shutil.which("swift") is None, reason="swift toolchain not installed")

FIXTURE = json.loads(
    (Path(__file__).resolve().parent / "fixtures" / "langs" / "swift" / "project.json").read_text(
        encoding="utf-8"
    )
)
IMPORTED_SPEC = FIXTURE["brief"]

NORMALIZER_SOURCE = (
    "public func normalizeText(_ text: String) -> String {\n"
    "    text.split(whereSeparator: \\.isWhitespace).joined(separator: \" \")\n"
    "}\n"
)
WORD_COUNTER_SOURCE = (
    "public func countWords(_ text: String) -> Int {\n"
    "    text.split(whereSeparator: \\.isWhitespace).count\n"
    "}\n"
)
INTEGRATION_SOURCE = (
    "import TextNormalizer\n"
    "import WordCounter\n\n"
    "public func integrate() -> Int {\n"
    "    countWords(normalizeText(\" a  b \"))\n"
    "}\n"
)

NORMALIZER_TEST = (
    "import XCTest\n"
    "@testable import TextNormalizer\n\n"
    "final class TextNormalizerTests: XCTestCase {\n"
    "    func test_R1_normalizeText() throws {\n"
    "        XCTAssertEqual(normalizeText(\" a  b \"), \"a b\")\n"
    "    }\n"
    "}\n"
)
WORD_COUNTER_TEST = (
    "import XCTest\n"
    "@testable import WordCounter\n\n"
    "final class WordCounterTests: XCTestCase {\n"
    "    func test_R1_countWords() throws {\n"
    "        XCTAssertEqual(countWords(\"a  b\"), 2)\n"
    "    }\n"
    "}\n"
)
INTEGRATION_TEST = (
    "import XCTest\n"
    "@testable import RootIntegration\n\n"
    "final class RootIntegrationTests: XCTestCase {\n"
    "    func test_S1_pipeline() throws {\n"
    "        XCTAssertEqual(integrate(), 2)\n"
    "    }\n"
    "}\n"
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
    return _structured({"file_path": path, "code": code, "covered_requirements": [req_id]})


def _source_script() -> list:
    return [
        reply(calls=[call("write_file", path="Sources/TextNormalizer/TextNormalizer.swift", content=NORMALIZER_SOURCE)]),
        reply("Implemented normalizer"),
        reply(calls=[call("write_file", path="Sources/WordCounter/WordCounter.swift", content=WORD_COUNTER_SOURCE)]),
        reply("Implemented counter"),
        reply(calls=[call("write_file", path="Sources/RootIntegration/RootIntegration.swift", content=INTEGRATION_SOURCE)]),
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


def test_swift_package_pipeline_imported_spec_to_done(tmp_path: Path):
    reasoner = [
        reply(_decompose()),
        reply(_contract()),
        reply(_component_spec("text-normalizer", "Normalize whitespace in text", "R1")),
        reply(_component_spec("word-counter", "Count words in text", "R1")),
        reply(_tests_payload("Tests/TextNormalizerTests/TextNormalizerTests.swift", NORMALIZER_TEST, "R1")),
        reply(_tests_payload("Tests/WordCounterTests/WordCounterTests.swift", WORD_COUNTER_TEST, "R1")),
        reply(_tests_payload("Tests/RootIntegrationTests/RootIntegrationTests.swift", INTEGRATION_TEST, "S1")),
        # D6: handle_integrate opens one reasoner consult before running the coder.
        reply("Integration wiring reviewed for scenario S1."),
    ]

    with StubServer(consult=reasoner, coder=_source_script()) as stub:
        from nai.llm import LLM

        db = tmp_path / ".nai" / "nai.sqlite3"
        db.parent.mkdir(parents=True, exist_ok=True)
        store = Store(db, read_only=False)
        pid = store.create_project(
            "Tiny Library", "Imported spec", workspace_path=tmp_path / "ws",
            settings={
                "model_profile": "stub",
                "intake_complete": True,
                "clarified": True,
                **FIXTURE["settings"],
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
            root = check.list_nodes(pid, parent_id=None)[0]
            children = check.list_nodes(pid, parent_id=root["id"])
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
        runner.run(drain=True, max_steps=800)
        runner.close()


        with Store(db, read_only=True) as check:
            project = check.get_project(pid)
            nodes = _all_nodes(check, pid)
            assert project["status"] == "done", (
                [(n["name"], n["status"]) for n in nodes],
                [(j["kind"], str(j.get("error"))[:1500]) for j in check.list_jobs(pid, limit=200) if j.get("error")],
            )
            assert {n["name"] for n in nodes} == {
                "root", "text-normalizer", "word-counter", "root-integration",
            }
            assert all(n["status"] == "done" for n in nodes)
            for node in nodes:
                assert node["built_hash"] == node["content_hash"], node["name"]

            jobs = check.list_jobs(pid, limit=10000)
            assert [j for j in jobs if j["status"] != "succeeded"] == [], [
                (j["kind"], j["status"], str(j.get("error"))[:500]) for j in jobs if j["status"] != "succeeded"
            ]
            assert {"decompose", "interfaces", "component_spec", "contract_stub", "author_tests",
                    "implement", "verify", "integrate"} <= {j["kind"] for j in jobs}

            events = check.list_events(pid, limit=10000)
            assert [e for e in events if e["type"] == "planner.error"] == []
            assert [e for e in events if e["type"] in {"job.failed", "node.failed", "build.verify_failed"}] == []
            assert any(e["type"] == "project.done" for e in events)

            workspace = Path(project["workspace_path"])
            for rel, expected in {
                "Sources/TextNormalizer/TextNormalizer.swift": NORMALIZER_SOURCE,
                "Sources/WordCounter/WordCounter.swift": WORD_COUNTER_SOURCE,
                "Sources/RootIntegration/RootIntegration.swift": INTEGRATION_SOURCE,
            }.items():
                assert (workspace / rel).read_text(encoding="utf-8") == expected, rel
            assert (workspace / "Package.swift").is_file()
            assert (workspace / "Sources" / "TinyLibraryCore" / "Entities.swift").is_file()
            manifest = (workspace / "Package.swift").read_text(encoding="utf-8")
            for target in ("TextNormalizer", "RootIntegration", "RootIntegrationTests"):
                assert f'name: "{target}"' in manifest

            expected_tests = {
                "Tests/TextNormalizerTests/TextNormalizerTests.swift": (NORMALIZER_TEST, "R1"),
                "Tests/WordCounterTests/WordCounterTests.swift": (WORD_COUNTER_TEST, "R1"),
                "Tests/RootIntegrationTests/RootIntegrationTests.swift": (INTEGRATION_TEST, "S1"),
            }
            records = {
                record["file_path"]: record
                for node in nodes
                for record in check.list_test_records(node["id"])
            }
            assert set(records) == set(expected_tests), sorted(records)
            for rel, (authored, req_id) in expected_tests.items():
                record = records[rel]
                assert record["frozen_hash"] == hashlib.sha1(authored.encode("utf-8")).hexdigest(), rel
                assert record["last_result"] == "pass", rel
                assert req_id in record["requirement_ids"], (rel, record["requirement_ids"])
                on_disk = (workspace / rel).read_text(encoding="utf-8")
                assert on_disk == authored, rel
                mode = stat.S_IMODE((workspace / rel).stat().st_mode)
                assert mode & 0o222 == 0, (rel, oct(mode))

            assert stub.remaining("consult") == 0
            assert stub.remaining("coder") == 0

