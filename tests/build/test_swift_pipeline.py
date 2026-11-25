"""Swift handler wiring without a Swift toolchain.

A fake workspace answers ``swift build`` / ``swift test`` from the generated
tree, so the real handlers, the real Swift adapter and the real ``Package.swift``
regeneration run end to end.  The real-toolchain test is
``tests/e2e/test_pipeline_swift.py`` (skipped where Swift is absent).
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

from nai.build.jobs import handle_author_tests, handle_contract_stub, handle_implement, handle_verify
from tests.build.conftest import FakeContext, FakeStore, FakeWorkspace, ScriptedClient

_TARGET = re.compile(r'\.(?:testTarget|target)\(name: "([^"]+)"')

ADD_SOURCE = "public func add(_ a: Int, _ b: Int) -> Int {\n    a + b\n}\n"
ADD_TEST = (
    "import XCTest\n"
    "@testable import Adder\n\n"
    "final class AdderTests: XCTestCase {\n"
    "    func test_R1_add() throws {\n"
    "        XCTAssertEqual(add(2, 3), 5)\n"
    "    }\n"
    "}\n"
)


class _FakeSwiftWorkspace(FakeWorkspace):
    """Answer SwiftPM commands from the tree; no toolchain required."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.commands: list[list[str]] = []

    def _declared(self) -> list[str]:
        text = (self.root / "Package.swift").read_text(encoding="utf-8")
        return _TARGET.findall(text)

    def run_argv(self, argv, *, phase: str = "run", env_overrides=None, network=None):  # type: ignore[override]
        argv = [str(part) for part in argv]
        self.commands.append(argv)
        if not (self.root / "Package.swift").is_file():
            return 1, "error: no Package.swift"
        if argv[:2] == ["swift", "build"]:
            for target in self._declared():
                if not (self.root / "Sources" / target).is_dir() and not (self.root / "Tests" / target).is_dir():
                    return 1, f"error: target '{target}' has no sources"
            return 0, "Build complete! (0.10s)"
        if argv[:2] == ["swift", "test"]:
            name = argv[argv.index("--filter") + 1] if "--filter" in argv else ""
            for path in sorted((self.root / "Sources").rglob("*.swift")):
                if 'fatalError("not implemented")' in path.read_text(encoding="utf-8"):
                    return 132, (
                        f"Test Case '{name}.test_R1_add' started.\n"
                        f"Fatal error: not implemented: file {path}, line 4"
                    )
            tests = [p for p in (self.root / "Tests").rglob("*.swift") if name and name in p.name]
            if not tests:
                return 0, "Executed 0 tests, with 0 failures (0 unexpected) in 0.000 (0.000) seconds"
            if self._ctx and getattr(self._ctx, "test_command", None) == shlex.join(argv):
                self._verified_cmds.add(shlex.join(argv))
            return 0, (
                f"Test Case '{name}.test_R1_add' passed (0.001 seconds).\n"
                "\t Executed 1 test, with 0 failures (0 unexpected) in 0.001 (0.002) seconds"
            )
        return 0, ""


def _setup(tmp_path: Path, *, platform: str = "any"):
    store = FakeStore(tmp_path)
    settings = {"language": "swift", "package_name": "Calc", "target_env": {"type_checker": "none"}}
    pid = store.create_project("calc", workspace_path=str(tmp_path), settings=settings)
    contract = {
        "entities": [{"name": "Input", "fields": [{"name": "a", "type": "int"}, {"name": "b", "type": "int"}]}],
        "components": [
            {
                "name": "adder",
                "platform": platform,
                "provides": [{"name": "add", "signature": "add(a: int, b: int) -> int",
                              "description": "Add two integers."}],
            }
        ],
        "scenarios": [{"id": "S1", "title": "Add two integers end-to-end",
                       "steps": [{"caller": "user", "callee": "adder", "call": "add", "fields": ["a", "b"]}]}],
    }
    root_id = store.create_node(pid, "calc-sys", "system", contract=contract)
    store.transition_node_status(root_id, "awaiting_approval")
    store.transition_node_status(root_id, "approved")
    store.add_approval(root_id, "interfaces", "approved")
    leaf_id = store.create_node(pid, "adder", "leaf", parent_id=root_id, objective="Add two integers",
                                spec_text="- R1: add(a, b) returns a + b.")
    store.replace_requirements(leaf_id, [{"id": "R1", "text": "add(a, b) returns a + b", "testable": True}])
    store.transition_node_status(leaf_id, "awaiting_approval")
    store.transition_node_status(leaf_id, "approved")
    store.transition_node_status(leaf_id, "ready")
    return store, pid, root_id, leaf_id


def _ctx(store: FakeStore, ws: FakeWorkspace, pid: str, node_id: str, kind: str, client=None) -> FakeContext:
    job = store.create_job(pid, kind, "coder", node_id=node_id)
    return FakeContext(store.jobs[job], store.get_node(node_id), store.get_project(pid), store, ws, client)


def test_swift_leaf_pipeline_without_a_toolchain(tmp_path: Path, monkeypatch) -> None:
    store, pid, _root, leaf = _setup(tmp_path)
    ws = _FakeSwiftWorkspace(tmp_path)

    assert handle_contract_stub(_ctx(store, ws, pid, leaf, "contract_stub"))["ok"]
    manifest = (tmp_path / "Package.swift").read_text(encoding="utf-8")
    assert 'name: "Adder"' in manifest and 'name: "CalcCore"' in manifest
    assert "AdderTests" not in manifest  # no test file yet
    stub = (tmp_path / "Sources" / "Adder" / "Adder.swift").read_text(encoding="utf-8")
    assert "public func add(_ a: Int, _ b: Int) -> Int {" in stub
    assert 'fatalError("not implemented")' in stub
    assert (tmp_path / "Sources" / "CalcCore" / "Entities.swift").is_file()

    payload = json.dumps({
        "file_path": "Tests/AdderTests/AdderTests.swift",
        "code": ADD_TEST,
        "covered_requirements": ["R1"],
    })
    ctx_tests = _ctx(store, ws, pid, leaf, "author_tests", ScriptedClient([payload]))
    assert handle_author_tests(ctx_tests)["ok"]
    manifest = (tmp_path / "Package.swift").read_text(encoding="utf-8")
    assert '.testTarget(name: "AdderTests", dependencies: ["Adder"])' in manifest
    assert store.list_test_records(leaf)[0]["last_result"] == "fail"

    def fake_coder(ctx, task, context_files, allow_bash, tools_bundle):
        ctx.ws.call("write_file", {"path": "Sources/Adder/Adder.swift", "content": ADD_SOURCE})
        return "Implemented add"

    monkeypatch.setattr("nai.build.jobs._run_coder", fake_coder)
    assert handle_implement(_ctx(store, ws, pid, leaf, "implement"))["ok"]
    assert (tmp_path / "Sources" / "Adder" / "Adder.swift").read_text(encoding="utf-8") == ADD_SOURCE

    result = handle_verify(_ctx(store, ws, pid, leaf, "verify"))
    assert result["ok"], result
    assert store.get_node(leaf)["status"] == "done"
    assert store.list_test_records(leaf)[0]["last_result"] == "pass"



def test_verify_rejects_a_run_that_executed_no_tests(tmp_path: Path, monkeypatch) -> None:
    import hashlib

    store, pid, _root, leaf = _setup(tmp_path)
    ws = _FakeSwiftWorkspace(tmp_path)
    assert handle_contract_stub(_ctx(store, ws, pid, leaf, "contract_stub"))["ok"]
    # A frozen record whose file the filter cannot match: SwiftPM exits 0 with
    # "Executed 0 tests", which must not count as verification.
    test_rel = "Tests/AdderTests/Other.swift"
    path = tmp_path / test_rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(ADD_TEST, encoding="utf-8")
    store.upsert_test_record(leaf, test_rel, ["R1"], hashlib.sha1(ADD_TEST.encode("utf-8")).hexdigest(),
                             last_result="fail")

    def coder(ctx, task, context_files, allow_bash, tools_bundle):
        ctx.ws.call("write_file", {"path": "Sources/Adder/Adder.swift", "content": ADD_SOURCE})
        return "Implemented"

    monkeypatch.setattr("nai.build.jobs._run_coder", coder)
    assert handle_implement(_ctx(store, ws, pid, leaf, "implement"))["ok"]
    result = handle_verify(_ctx(store, ws, pid, leaf, "verify"))
    assert not result["ok"]
    assert "No tests ran" in (result.get("output") or result.get("error") or "")


def _author_and_implement(tmp_path: Path, monkeypatch, *, platform: str, source: str):
    store, pid, _root, leaf = _setup(tmp_path, platform=platform)
    ws = _FakeSwiftWorkspace(tmp_path)
    assert handle_contract_stub(_ctx(store, ws, pid, leaf, "contract_stub"))["ok"]
    payload = json.dumps({"file_path": "Tests/AdderTests/AdderTests.swift", "code": ADD_TEST,
                          "covered_requirements": ["R1"]})
    assert handle_author_tests(_ctx(store, ws, pid, leaf, "author_tests", ScriptedClient([payload])))["ok"]

    def coder(ctx, task, context_files, allow_bash, tools_bundle):
        ctx.ws.call("write_file", {"path": "Sources/Adder/Adder.swift", "content": source})
        return "Implemented"

    monkeypatch.setattr("nai.build.jobs._run_coder", coder)
    assert handle_implement(_ctx(store, ws, pid, leaf, "implement"))["ok"]
    return store, pid, leaf, ws


def test_verify_fails_when_a_portable_node_imports_swiftui(tmp_path: Path, monkeypatch) -> None:
    store, pid, leaf, ws = _author_and_implement(
        tmp_path, monkeypatch, platform="any", source="import SwiftUI\n\n" + ADD_SOURCE
    )
    result = handle_verify(_ctx(store, ws, pid, leaf, "verify"))
    assert not result["ok"]
    assert "imports SwiftUI" in (result.get("output") or result.get("error") or "")


def test_verify_fails_when_a_linux_node_imports_uikit(tmp_path: Path, monkeypatch) -> None:
    store, pid, leaf, ws = _author_and_implement(
        tmp_path, monkeypatch, platform="linux", source="import UIKit\n\n" + ADD_SOURCE
    )
    result = handle_verify(_ctx(store, ws, pid, leaf, "verify"))
    assert not result["ok"]
    assert "imports UIKit" in (result.get("output") or result.get("error") or "")


def test_macos_nodes_may_import_swiftui(tmp_path: Path, monkeypatch) -> None:
    store, pid, leaf, ws = _author_and_implement(
        tmp_path, monkeypatch, platform="macos", source="import SwiftUI\n\n" + ADD_SOURCE
    )
    result = handle_verify(_ctx(store, ws, pid, leaf, "verify"))
    assert result["ok"], result
    assert store.get_node(leaf)["status"] == "done"

