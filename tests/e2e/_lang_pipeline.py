"""Shared end-to-end driver for the compiled-language adapters.

``test_pipeline_swift.py`` is the reference implementation; this module extracts
the language-independent half so TypeScript, Go, Rust, C and C++ each keep a
thin test file that only declares its sources and tests.  Every adapter runs the
real Store, Runner, spec/build plugins, workspace and scripted stub model
server, so the generated project is exercised exactly as a user would.
"""

from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path

from nai.build.plugin import register as register_build
from nai.config import Config
from nai.langs import registry
from nai.models import ModelProfile, RoleConfig
from nai.sched import Runner, SchedConfig
from nai.spec.plugin import register as register_spec
from nai.store import Store
from nai.testing.stub_server import StubServer, call, reply

FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "langs"


def load_fixture(language: str) -> dict:
    """Load ``fixtures/langs/<language>/project.json``."""
    return json.loads((FIXTURE_ROOT / language / "project.json").read_text(encoding="utf-8"))


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
    return _structured(
        {
            "components": [
                {
                    "name": "text-normalizer",
                    "objective": "Normalize whitespace in text",
                    "depends_on": [],
                },
                {
                    "name": "word-counter",
                    "objective": "Count words in text",
                    "depends_on": ["text-normalizer"],
                },
            ],
            "shared_entities": [
                {
                    "name": "Input",
                    "description": "Text input",
                    "fields": [{"name": "text", "type": "str", "description": "Raw text"}],
                }
            ],
            "external_inputs": [{"name": "text", "description": "Raw text", "source": "user"}],
            "external_outputs": [
                {"name": "count", "description": "Word count", "destination": "user"}
            ],
        }
    )


def _contract() -> str:
    return _structured(
        {
            "contract": {
                "entities": [{"name": "Input", "fields": [{"name": "text", "type": "str"}]}],
                "components": [
                    {
                        "name": "text-normalizer",
                        "provides": [
                            {
                                "name": "normalize_text",
                                "signature": "normalize_text(text: str) -> str",
                                "description": "Collapse whitespace",
                            }
                        ],
                        "consumes": [],
                    },
                    {
                        "name": "word-counter",
                        "provides": [
                            {
                                "name": "count_words",
                                "signature": "count_words(text: str) -> int",
                                "description": "Count whitespace-separated words",
                            }
                        ],
                        "consumes": ["text-normalizer"],
                    },
                ],
                "scenarios": [
                    {
                        "id": "S1",
                        "title": "Normalize then count",
                        "steps": [
                            {
                                "caller": "user",
                                "callee": "word-counter",
                                "call": "count_words",
                                "fields": ["Input.text"],
                            }
                        ],
                    }
                ],
            }
        }
    )


def _component_spec(name: str, req: str, marker: str, deps: tuple[str, ...] = ()) -> str:
    dependency_lines = "".join(f"- {dep}\n" for dep in deps)
    return _structured(
        {
            "spec_markdown": (
                f"# Component: {name}\n## Purpose\n{req}\n## Interface\nNone\n"
                f"## Requirements\n- {marker}: {req}\n## Edge Cases\n- E1: Empty text\n"
                f"## Example\nInput: 'a  b'\nOutput: '{marker}'\n"
                f"## Dependencies\n{dependency_lines}"
            ),
            "requirements": [{"id": marker, "text": req, "testable": True}],
            "edge_cases": [{"id": "E1", "text": "Empty text"}],
            "example": "Input 'a  b' produces two normalized tokens",
            "dependencies": list(deps),
        }
    )


def _tests_payload(path: str, code: str, req_id: str) -> str:
    return _structured({"file_path": path, "code": code, "covered_requirements": [req_id]})


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


def _project_settings(fixture: dict) -> dict:
    """The fixture settings, with the language and package name normalised."""
    settings = dict(fixture.get("settings") or {})
    settings.setdefault("language", fixture["language"])
    return settings


def run_language_pipeline(
    tmp_path: Path,
    *,
    language: str,
    fixture: dict,
    sources: dict[str, str],
    tests: dict[str, str],
    integration_name: str = "root-integration",
) -> None:
    """Drive one language's fixture through the real pipeline to ``done``.

    ``sources`` and ``tests`` map a node name to the code the coder authors for
    it.  The adapter decides where each file lives, so the test never hard-codes
    a layout.
    """
    adapter = registry.get(language)
    project = {"name": fixture["name"], "settings": _project_settings(fixture)}
    # The adapter's own layout helpers name the files; the coder writes there.
    source_paths = {name: adapter.source_rel(project, {"name": name}) for name in sources}
    test_paths = {name: adapter.test_rel(project, {"name": name}) for name in tests}

    reasoner = [
        reply(_decompose()),
        reply(_contract()),
        reply(_component_spec("text-normalizer", "Normalize whitespace in text", "R1")),
        reply(
            _component_spec(
                "word-counter", "Count words in text", "R1", deps=("text-normalizer",)
            )
        ),
        reply(_tests_payload(test_paths["text-normalizer"], tests["text-normalizer"], "R1")),
        reply(_tests_payload(test_paths["word-counter"], tests["word-counter"], "R1")),
        reply(_tests_payload(test_paths[integration_name], tests[integration_name], "S1")),
        # D6: handle_integrate opens one reasoner consult before running the coder.
        reply("Integration wiring reviewed for scenario S1."),
    ]

    source_script = []
    for name in ("text-normalizer", "word-counter", integration_name):
        source_script.append(
            reply(calls=[call("write_file", path=source_paths[name], content=sources[name])])
        )
        source_script.append(reply(f"Implemented {name}"))

    with StubServer(consult=reasoner, coder=source_script) as stub:
        from nai.llm import LLM

        db = tmp_path / ".nai" / "nai.sqlite3"
        db.parent.mkdir(parents=True, exist_ok=True)
        store = Store(db, read_only=False)
        pid = store.create_project(
            fixture["name"],
            "Imported spec",
            workspace_path=tmp_path / "ws",
            settings={
                "model_profile": "stub",
                "intake_complete": True,
                "clarified": True,
                **_project_settings(fixture),
            },
        )
        store.create_node(
            pid, "root", "system", spec_text=fixture["brief"], objective="Tiny library root"
        )
        store.close()

        llm = LLM(stub.url, "none", stream=False, show_thinking=False)

        def make_runner() -> Runner:
            runner = Runner(
                Store(db, read_only=False),
                llm,
                app_config=_config(),
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
                writable.transition_node_status(
                    root["id"], "awaiting_approval", note="imported spec review"
                )
            writable.enqueue_command(
                pid, "approve", node_id=root["id"], payload={"stage": "interfaces"}
            )
            for child in children:
                writable.enqueue_command(
                    pid, "approve", node_id=child["id"], payload={"stage": "component-spec"}
                )

        runner = make_runner()
        runner.run(drain=True, max_steps=800)
        runner.close()

        with Store(db, read_only=True) as check:
            project_row = check.get_project(pid)
            nodes = _all_nodes(check, pid)
            assert project_row["status"] == "done", (
                [(n["name"], n["status"]) for n in nodes],
                [
                    (j["kind"], str(j.get("error"))[:1500])
                    for j in check.list_jobs(pid, limit=200)
                    if j.get("error")
                ],
            )
            assert {n["name"] for n in nodes} == {
                "root",
                "text-normalizer",
                "word-counter",
                integration_name,
            }
            assert all(n["status"] == "done" for n in nodes)
            for node in nodes:
                assert node["built_hash"] == node["content_hash"], node["name"]

            jobs = check.list_jobs(pid, limit=10000)
            assert [j for j in jobs if j["status"] != "succeeded"] == [], [
                (j["kind"], j["status"], str(j.get("error"))[:500])
                for j in jobs
                if j["status"] != "succeeded"
            ]
            assert {
                "decompose",
                "interfaces",
                "component_spec",
                "contract_stub",
                "author_tests",
                "implement",
                "verify",
                "integrate",
            } <= {j["kind"] for j in jobs}

            events = check.list_events(pid, limit=10000)
            assert [e for e in events if e["type"] == "planner.error"] == []
            assert [
                e for e in events if e["type"] in {"job.failed", "node.failed", "build.verify_failed"}
            ] == []
            assert any(e["type"] == "project.done" for e in events)

            workspace = Path(project_row["workspace_path"])
            for name, expected in sources.items():
                rel = source_paths[name]
                assert (workspace / rel).read_text(encoding="utf-8") == expected, rel

            expected_tests = {
                test_paths["text-normalizer"]: (tests["text-normalizer"], "R1"),
                test_paths["word-counter"]: (tests["word-counter"], "R1"),
                test_paths[integration_name]: (tests[integration_name], "S1"),
            }
            records = {
                record["file_path"]: record
                for node in nodes
                for record in check.list_test_records(node["id"])
            }
            assert set(records) == set(expected_tests), sorted(records)
            for rel, (authored, req_id) in expected_tests.items():
                record = records[rel]
                assert (
                    record["frozen_hash"] == hashlib.sha1(authored.encode("utf-8")).hexdigest()
                ), rel
                assert record["last_result"] == "pass", rel
                assert req_id in record["requirement_ids"], (rel, record["requirement_ids"])
                on_disk = (workspace / rel).read_text(encoding="utf-8")
                assert on_disk == authored, rel
                mode = stat.S_IMODE((workspace / rel).stat().st_mode)
                assert mode & 0o222 == 0, (rel, oct(mode))

            assert stub.remaining("consult") == 0
            assert stub.remaining("coder") == 0


