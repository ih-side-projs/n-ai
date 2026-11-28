"""End-to-end tests for WP3 plugin registration, SS-BUILD jobs, BLD-7 integration, and BLD-12 acceptance."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

from nai.build.jobs import (
    handle_author_tests,
    handle_contract_stub,
    handle_diagnose,
    handle_implement,
    handle_integrate,
    handle_review,
    handle_verify,
)
from nai.build.plugin import _can_fast_verify, plan, register
from tests.build.conftest import FakeContext, FakeStore, FakeWorkspace, ScriptedClient


class _HostPyWorkspace(FakeWorkspace):
    """FakeWorkspace that rewrites a fresh venv python -m pytest call to host sys.executable for fast unit tests."""

    def run_command(self, cmd: str) -> tuple[int, str]:
        requested = cmd
        if "-m pytest" in cmd:
            after_pytest = cmd.split("-m pytest", 1)[1].strip()
            cmd = f"PYTHONPATH=src {sys.executable} -m pytest {after_pytest}"
        elif " -c " in cmd and ".nai/venv" in cmd:
            after_c = cmd.split(" -c ", 1)[1]
            cmd = f"{sys.executable} -c {after_c}"
        rc, out = super().run_command(cmd)
        if rc == 0 and self._ctx and getattr(self._ctx, "test_command", None) == requested:
            self._verified_cmds.add(requested)
        return rc, out

    def run_argv(self, argv, *, phase="run", env_overrides=None, network=None):
        """Rewrite a fresh-venv python to the host interpreter, like run_command does."""
        import shlex

        argv = [str(a) for a in argv]
        requested = shlex.join(argv)
        if argv and argv[0].endswith("python") and ("-m" in argv or "-c" in argv):
            argv[0] = sys.executable
        # A fresh target venv has pytest but no third-party plugins.  The host
        # interpreter has pytest-asyncio, whose own deprecation warning would
        # otherwise be escalated by the pipeline's -W error policy.
        merged = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", **(env_overrides or {})}
        rc, out = super().run_argv(argv, phase=phase, env_overrides=merged, network=network)
        if rc == 0 and self._ctx and getattr(self._ctx, "test_command", None) == requested:
            self._verified_cmds.add(requested)
        return rc, out


def _setup_project_and_leaf(tmp_path: Path, *, extra_settings: dict | None = None):
    store = FakeStore(tmp_path)
    settings = {
        "target_env": {"type_checker": "none"},
        **(extra_settings or {}),
    }
    pid = store.create_project("calc", workspace_path=str(tmp_path), settings=settings)
    contract = {
        "entities": [],
        "components": [
            {
                "name": "adder",
                "provides": [
                    {
                        "signature": "add(a: int, b: int) -> int",
                        "description": "Add two integers.",
                    }
                ],
            }
        ],
        "scenarios": [
            {
                "id": "S1",
                "title": "Add two integers end-to-end",
                "steps": [{"caller": "user", "callee": "adder", "call": "add", "fields": ["a", "b"]}],
            }
        ],
    }
    root_id = store.create_node(pid, "calc-sys", "system", contract=contract)
    store.transition_node_status(root_id, "awaiting_approval")
    store.transition_node_status(root_id, "approved")
    store.add_approval(root_id, "interfaces", "approved")

    leaf_id = store.create_node(
        pid,
        "adder",
        "leaf",
        parent_id=root_id,
        objective="Add two integers",
        spec_text="- R1: add(a, b) returns a + b.",
    )
    store.replace_requirements(leaf_id, [{"id": "R1", "text": "add(a, b) returns a + b", "testable": True}])
    store.transition_node_status(leaf_id, "awaiting_approval")
    store.transition_node_status(leaf_id, "approved")
    store.transition_node_status(leaf_id, "ready")
    return store, pid, root_id, leaf_id


def test_WP3_register_wires_all_build_handlers_and_planner():
    registered: dict[str, tuple[object, bool]] = {}
    planners: list[object] = []

    runner = SimpleNamespace(
        registry=SimpleNamespace(register=lambda k, fn, uses_model=True: registered.__setitem__(k, (fn, uses_model))),
        register_planner=lambda fn: planners.append(fn),
    )
    register(runner)

    assert set(registered) == {
        "contract_stub",
        "author_tests",
        "implement",
        "verify",
        "integrate",
        "diagnose",
        "review",
    }
    assert registered["contract_stub"][1] is False
    assert registered["verify"][1] is False
    assert registered["implement"][1] is True
    assert planners == [plan]


def test_BLD2_BLD3_BLD5_BLD6_full_leaf_pipeline(tmp_path: Path, monkeypatch):
    store, pid, _root_id, leaf_id = _setup_project_and_leaf(tmp_path)
    ws = _HostPyWorkspace(tmp_path)

    # 1. contract_stub (BLD-2)
    j_stub = store.create_job(pid, "contract_stub", "coder", node_id=leaf_id)
    ctx_stub = FakeContext(store.jobs[j_stub], store.get_node(leaf_id), store.get_project(pid), store, ws, None)
    res_stub = handle_contract_stub(ctx_stub)
    assert res_stub["ok"]
    assert (tmp_path / "src/calc/adder.py").is_file()
    assert "NotImplementedError" in (tmp_path / "src/calc/adder.py").read_text(encoding="utf-8")

    # 2. author_tests (BLD-3, BLD-4) - tests must fail against NotImplementedError stubs
    test_payload = json.dumps(
        {
            "file_path": "tests/test_adder.py",
            "code": (
                "from calc.adder import add\n\n"
                "def test_R1_adds_two_ints():\n"
                "    assert add(2, 3) == 5\n"
            ),
            "covered_requirements": ["R1"],
        }
    )
    j_tests = store.create_job(pid, "author_tests", "reasoner", node_id=leaf_id)
    ctx_tests = FakeContext(
        store.jobs[j_tests], store.get_node(leaf_id), store.get_project(pid), store, ws, ScriptedClient([test_payload])
    )
    res_tests = handle_author_tests(ctx_tests)
    assert res_tests["ok"]
    records = store.list_test_records(leaf_id)
    assert len(records) == 1
    assert records[0]["last_result"] == "fail"

    # 3. implement (BLD-5, CORE-3) - coder writes src/calc/adder.py and cannot write tests/**
    def fake_run_coder(ctx, task, context_files, allow_bash, tools_bundle):
        # Verify PathPolicy blocks writing frozen test file
        err = ctx.ws.call("write_file", {"path": "tests/test_adder.py", "content": "def test_R1_x(): assert True\n"})
        assert err.startswith("ERROR: path not writable")
        ok = ctx.ws.call(
            "write_file",
            {"path": "src/calc/adder.py", "content": "def add(a: int, b: int) -> int:\n    return a + b\n"},
        )
        assert not ok.startswith("ERROR:")
        return "Implemented add"

    monkeypatch.setattr("nai.build.jobs._run_coder", fake_run_coder)
    j_impl = store.create_job(pid, "implement", "coder", node_id=leaf_id)
    ctx_impl = FakeContext(store.jobs[j_impl], store.get_node(leaf_id), store.get_project(pid), store, ws, None)
    res_impl = handle_implement(ctx_impl)
    assert res_impl["ok"]
    assert store.get_node(leaf_id)["attempts"] == 1
    assert ws.checkpoints

    # 4. verify (BLD-6, TENV-2) -> transitions node to done
    j_ver = store.create_job(pid, "verify", "coder", node_id=leaf_id)
    ctx_ver = FakeContext(store.jobs[j_ver], store.get_node(leaf_id), store.get_project(pid), store, ws, None)
    res_ver = handle_verify(ctx_ver)
    assert res_ver["ok"]
    assert store.get_node(leaf_id)["status"] == "done"
    assert store.list_test_records(leaf_id)[0]["last_result"] == "pass"


def test_BLD5_protected_hash_tamper_fails_implement(tmp_path: Path, monkeypatch):
    store, pid, _root_id, leaf_id = _setup_project_and_leaf(tmp_path)
    ws = _HostPyWorkspace(tmp_path)
    test_file = tmp_path / "tests" / "test_adder.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("def test_R1_x():\n    assert False\n", encoding="utf-8")

    def tamper_coder(ctx, task, context_files, allow_bash, tools_bundle):
        # Simulate shell-side bypass directly modifying the frozen test file on disk
        test_file.write_text("def test_R1_x():\n    assert True\n", encoding="utf-8")
        return "tampered"

    monkeypatch.setattr("nai.build.jobs._run_coder", tamper_coder)
    j_impl = store.create_job(pid, "implement", "coder", node_id=leaf_id)
    ctx_impl = FakeContext(store.jobs[j_impl], store.get_node(leaf_id), store.get_project(pid), store, ws, None)
    res = handle_implement(ctx_impl)
    assert not res["ok"]
    assert store.get_node(leaf_id)["status"] == "failed"


def test_BLD6_frozen_test_hash_mismatch_fails_verify(tmp_path: Path):
    store, pid, _root_id, leaf_id = _setup_project_and_leaf(tmp_path)
    ws = _HostPyWorkspace(tmp_path)
    test_file = tmp_path / "tests" / "test_adder.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("def test_R1_x():\n    assert True\n", encoding="utf-8")
    store.upsert_test_record(leaf_id, "tests/test_adder.py", ["R1"], "wrong_hash")
    store.set_node_test_command(leaf_id, "pytest -q tests/test_adder.py")

    j_ver = store.create_job(pid, "verify", "coder", node_id=leaf_id)
    ctx_ver = FakeContext(store.jobs[j_ver], store.get_node(leaf_id), store.get_project(pid), store, ws, None)
    res = handle_verify(ctx_ver)
    assert not res["ok"]
    assert "hash mismatch" in res["error"]
    assert store.get_node(leaf_id)["status"] == "failed"


def test_BLD7_BLD12_bottom_up_integration_and_final_acceptance(tmp_path: Path, monkeypatch):
    store, pid, root_id, leaf_id = _setup_project_and_leaf(
        tmp_path,
        extra_settings={"test_cmds": [f"{sys.executable} -c \"print('acceptance ok')\""]},
    )
    ws = _HostPyWorkspace(tmp_path)

    # Mark leaf done and write working implementation
    (tmp_path / "src/calc").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src/calc/__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "src/calc/adder.py").write_text("def add(a: int, b: int) -> int:\n    return a + b\n", encoding="utf-8")
    store.transition_node_status(leaf_id, "running")
    store.transition_node_status(leaf_id, "verifying")
    store.transition_node_status(leaf_id, "done")

    pc = SimpleNamespace(
        project=store.get_project(pid),
        store=store,
        active_jobs=lambda nid: [
            j for j in store.list_jobs(pid) if j["node_id"] == nid and j["status"] in ("queued", "running")
        ],
    )

    # Planner creates integration node for root and queues author_tests for S1
    plan(pc)
    int_nodes = [n for n in store.list_nodes(pid, parent_id=root_id) if n["kind"] == "integration"]
    assert len(int_nodes) == 1
    int_id = int_nodes[0]["id"]
    queued = store.list_jobs(pid, status="queued")
    assert queued[0]["kind"] == "author_tests" and queued[0]["node_id"] == int_id

    # Write scenario test manually (simulating frozen S1 test) and mark integrate succeeded
    scen_path = tmp_path / "tests" / "test_calc_sys_integration.py"
    scen_path.parent.mkdir(parents=True, exist_ok=True)
    scen_code = "from calc.adder import add\n\ndef test_S1_end_to_end():\n    assert add(4, 5) == 9\n"
    scen_path.write_text(scen_code, encoding="utf-8")
    store.upsert_test_record(
        int_id,
        "tests/test_calc_sys_integration.py",
        ["S1"],
        hashlib.sha1(scen_code.encode("utf-8")).hexdigest(),
    )
    store.set_node_test_command(int_id, "pytest -q tests/test_calc_sys_integration.py")
    store.update_job(queued[0]["id"], status="succeeded")

    monkeypatch.setattr("nai.build.jobs._run_coder", lambda *a, **kw: "wired")
    j_int = store.create_job(pid, "integrate", "coder", node_id=int_id)
    ctx_int = FakeContext(store.jobs[j_int], store.get_node(int_id), store.get_project(pid), store, ws, None)
    assert handle_integrate(ctx_int)["ok"]

    j_ver = store.create_job(pid, "verify", "coder", node_id=int_id)
    ctx_ver = FakeContext(store.jobs[j_ver], store.get_node(int_id), store.get_project(pid), store, ws, None)
    res_ver = handle_verify(ctx_ver)
    assert res_ver["ok"]
    assert res_ver["final_acceptance"] is True
    assert store.get_node(int_id)["status"] == "done"
    assert store.get_node(root_id)["status"] == "done"
    assert store.get_project(pid)["status"] == "done"


def test_BLD9_diagnose_test_fault_escalates_and_BLD11_review_creates_test_request(tmp_path: Path):
    store, pid, _root_id, leaf_id = _setup_project_and_leaf(tmp_path)
    ws = _HostPyWorkspace(tmp_path)

    # Diagnose returns test fault -> escalates for owner approval before re-authoring
    diag_reply = json.dumps(
        {
            "fault_kind": "test",
            "summary": "Frozen test asserts wrong return type",
            "revised_brief": "",
            "missing_package": None,
            "affected_requirements": ["R1"],
        }
    )
    j_diag = store.create_job(pid, "diagnose", "reasoner", node_id=leaf_id, input_ref={"failure_output": "AssertionError"})
    ctx_diag = FakeContext(store.jobs[j_diag], store.get_node(leaf_id), store.get_project(pid), store, ws, ScriptedClient([diag_reply]))
    res_diag = handle_diagnose(ctx_diag)
    assert res_diag["ok"]
    assert store.get_node(leaf_id)["status"] == "escalated"
    assert any(e["type"] == "build.test_reauthor_requested" for e in store.list_events(pid, node_id=leaf_id))

    # BLD-11 review mismatch enqueues author_tests (never edits code directly)
    store.transition_node_status(leaf_id, "ready")
    store.transition_node_status(leaf_id, "running")
    store.transition_node_status(leaf_id, "verifying")
    store.transition_node_status(leaf_id, "done")
    review_reply = json.dumps(
        {
            "approved": False,
            "summary": "Missing negative input check",
            "mismatches": [
                {
                    "kind": "edge_case",
                    "requirement_id": "R1",
                    "description": "Negative integers not covered",
                    "suggested_test_name": "test_R1_negative_ints",
                }
            ],
        }
    )
    j_rev = store.create_job(pid, "review", "reasoner", node_id=leaf_id)
    ctx_rev = FakeContext(store.jobs[j_rev], store.get_node(leaf_id), store.get_project(pid), store, ws, ScriptedClient([review_reply]))
    res_rev = handle_review(ctx_rev)
    assert res_rev["ok"]


def test_fast_verify_requires_matching_built_hash_and_existing_tests(tmp_path: Path):
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_sample.py").write_text("def test_ok(): pass\n", encoding="utf-8")
    store = SimpleNamespace(list_test_records=lambda _nid: [{"file_path": "tests/test_sample.py"}])

    stale_node = {"id": "n1", "built_hash": "old", "content_hash": "new"}
    assert _can_fast_verify(store, stale_node, tmp_path) is False

    unchanged_node = {"id": "n2", "built_hash": "same", "content_hash": "same"}
    assert _can_fast_verify(store, unchanged_node, tmp_path) is True
