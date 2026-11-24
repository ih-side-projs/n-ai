"""SS-BUILD job handlers executed by the WP3 scheduler."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .context_pack import assemble_context_pack
from .escalation import raise_change_request
from .prompts import get_prompt
from .structured import (
    AUTHOR_TESTS_SCHEMA,
    DIAGNOSE_SCHEMA,
    REVIEW_SCHEMA,
    StructuredOutputError,
    call_structured_json,
)
from .stubs import (
    entities_rel_path,
    generate_contract_stubs,
    node_stub_rel_path,
    project_package_name,
    slugify_module,
    validate_stub_imports,
)
from .tenv import (
    TargetEnv,
    TargetEnvManager,
    detect_stale_api,
    format_env_block,
    parse_target_env,
)
from .tools_bridge import build_tools_bundle
from .traceability import check_traceability


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_job_result(ok: bool, **kwargs: Any) -> Any:
    try:
        from nai.sched import JobResult  # type: ignore

        return JobResult.success(**kwargs) if ok else JobResult.failure(kwargs.pop("error", "failed"), **kwargs)
    except Exception:
        return {"ok": ok, **kwargs}


def _unpack_cmd_result(res: Any) -> tuple[int, str]:
    """Adapt WP1 Workspace.run_command return value across tuple / object / dict / str."""
    if isinstance(res, tuple) and len(res) >= 2:
        return int(res[0]), str(res[1])
    if isinstance(res, dict):
        rc = res.get("exit_code", res.get("returncode", 0))
        out = res.get("output", res.get("stdout", ""))
        return int(rc), str(out)
    if hasattr(res, "exit_code") or hasattr(res, "returncode"):
        rc = getattr(res, "exit_code", getattr(res, "returncode", 0))
        out = getattr(res, "output", None)
        if out is None:
            out = (getattr(res, "stdout", "") or "") + (getattr(res, "stderr", "") or "")
        return int(rc), str(out)
    text = str(res)
    m = re.search(r"(?:exit(?:ed)?(?:\s+with)?(?:\s+code)?[:\s]+)(-?\d+)", text, re.IGNORECASE)
    if m:
        return int(m.group(1)), text
    if text.startswith("ERROR:"):
        return 1, text
    return 0, text


def _workspace_root(ctx: Any) -> Path:
    if ctx.ws is not None and getattr(ctx.ws, "root", None) is not None:
        return Path(ctx.ws.root)
    wp = (ctx.project or {}).get("workspace_path")
    if not wp:
        raise RuntimeError("Project has no workspace_path configured")
    return Path(wp)


def _parent_contract(store: Any, node: dict[str, Any]) -> dict[str, Any] | None:
    if not node.get("parent_id"):
        return node.get("contract")
    parent = store.get_node(node["parent_id"])
    return parent.get("contract") if parent else node.get("contract")


def _safe_rel_test_path(raw_path: str, node: dict[str, Any]) -> str:
    cleaned = raw_path.replace("\\", "/").lstrip("/")
    parts = [p for p in cleaned.split("/") if p and p != "."]
    if not parts or ".." in parts:
        return f"tests/test_{slugify_module(node['name'])}.py"
    if parts[0] != "tests":
        parts = ["tests", *parts]
    if not parts[-1].endswith(".py"):
        parts[-1] += ".py"
    return "/".join(parts)


def _ensure_running(store: Any, node: dict[str, Any]) -> dict[str, Any]:
    if node["status"] == "ready":
        store.transition_node_status(node["id"], "running", note="Starting build job")
        return store.get_node(node["id"]) or node
    return node


def _fail_node(store: Any, node_id: str, reason: str, data: dict[str, Any] | None = None) -> None:
    cur = store.get_node(node_id)
    if cur is None:
        return
    if cur["status"] == "ready":
        store.transition_node_status(node_id, "running", note="Transitioned before failure")
        cur = store.get_node(node_id) or cur
    if cur["status"] in ("running", "verifying"):
        store.transition_node_status(node_id, "failed", note=reason, data=data)


def _job_context(node_id: str, job_kind: str, attempt: int, policy: Any, test_command: str | None = None) -> Any:
    from nai.workspace import JobContext

    return JobContext(
        node_id=node_id, job_kind=job_kind, attempt=attempt,
        policy=policy, test_command=test_command,
    )


def _path_policy(writable: tuple[str, ...], readonly: tuple[str, ...]) -> Any:
    from nai.workspace import PathPolicy

    return PathPolicy(writable=writable, readonly=readonly)


def _write_via_ws(ws: Any, ws_root: Path, rel_path: str, content: str) -> None:
    if ws is not None and hasattr(ws, "call"):
        res = ws.call("write_file", {"path": rel_path, "content": content})
        if isinstance(res, str) and res.startswith("ERROR:"):
            raise RuntimeError(res)
        target = ws_root / rel_path
        if target.exists():
            return
    target = ws_root / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# BLD-2: contract_stub
# ---------------------------------------------------------------------------


def handle_contract_stub(ctx: Any) -> Any:
    """Generate and validate importable contract stubs from frozen parent contract (BLD-2)."""
    ctx.check()
    project = ctx.project
    node = _ensure_running(ctx.store, ctx.node)
    ws_root = _workspace_root(ctx)
    parent_contract = _parent_contract(ctx.store, node)

    tenv = parse_target_env(project, ws_root)
    tenv_mgr = TargetEnvManager()
    tenv_mgr.ensure_env(ws_root, tenv)
    py_exe = str(tenv_mgr.venv_python(ws_root, tenv))

    stub_res = generate_contract_stubs(project, node, parent_contract)
    writable = tuple(f.rel_path for f in stub_res.files)
    policy = _path_policy(writable=writable, readonly=("tests/**", ".nai/**"))

    with ctx.ws.job_context(_job_context(node["id"], "contract_stub", ctx.job.get("attempt", 1), policy)):
        for sf in stub_res.files:
            _write_via_ws(ctx.ws, ws_root, sf.rel_path, sf.content)
            ctx.artifact("code", sf.content, Path(sf.rel_path).name)

        def _run(cmd: str) -> tuple[int, str]:
            return _unpack_cmd_result(ctx.ws.run_command(cmd))

        ok, out = validate_stub_imports(ws_root, stub_res.module_import, _run, python_exe=py_exe)
        if not ok:
            _fail_node(ctx.store, node["id"], f"Contract stub import failed: {out}")
            return _make_job_result(False, error=f"Stub import failed: {out}", output=out)

        if hasattr(ctx.ws, "checkpoint_job"):
            ctx.ws.checkpoint_job()

    ctx.event(
        "build.contract_stub_ready",
        f"Generated contract stubs for {node['name']}",
        data={"files": list(writable), "module": stub_res.module_import},
    )
    return _make_job_result(
        True,
        content_hash=node.get("content_hash"),
        files=list(writable),
        module=stub_res.module_import,
    )


# ---------------------------------------------------------------------------
# BLD-3 & BLD-4: author_tests
# ---------------------------------------------------------------------------


def _scenario_requirements_for_integration(parent_contract: dict[str, Any] | None) -> list[dict[str, Any]]:
    scenarios = list((parent_contract or {}).get("scenarios") or [])
    reqs: list[dict[str, Any]] = []
    for idx, sc in enumerate(scenarios, 1):
        sid = str(sc.get("id") or f"S{idx}")
        title = str(sc.get("title") or "")
        reqs.append({"id": sid, "text": title, "testable": True})
    return reqs


def handle_author_tests(ctx: Any) -> Any:
    """Author frozen requirement or scenario tests from spec/contract only (BLD-3, BLD-4, BLD-7)."""
    ctx.check()
    project = ctx.project
    node = _ensure_running(ctx.store, ctx.node)
    ws_root = _workspace_root(ctx)
    parent_contract = _parent_contract(ctx.store, node)

    tenv = parse_target_env(project, ws_root)
    tenv_mgr = TargetEnvManager()
    tenv_mgr.ensure_env(ws_root, tenv)

    is_integration = node.get("kind") == "integration"
    if is_integration:
        requirements = _scenario_requirements_for_integration(parent_contract)
    else:
        requirements = list(ctx.store.list_requirements(node["id"]) or [])

    if not requirements:
        requirements = [{"id": "R1", "text": node.get("objective") or node["name"], "testable": True}]

    prompt = get_prompt("author_tests")
    pkg = project_package_name(project)
    mod = slugify_module(node["name"])
    default_test_path = f"tests/test_{mod}.py"

    # Never include implementation source; only spec, contract interfaces, and TENV-4/5
    user_payload = {
        "node_name": node["name"],
        "node_kind": node["kind"],
        "suggested_test_path": default_test_path,
        "import_module": f"{pkg}.{mod}",
        "objective": node.get("objective") or "",
        "spec_text": node.get("spec_text") or "",
        "requirements": requirements,
        "contract": parent_contract or node.get("contract") or {},
    }
    messages = [
        {"role": "system", "content": prompt.text},
        {
            "role": "user",
            "content": (
                f"{format_env_block(tenv)}\n\n"
                "Author pytest tests for this component/integration node.\n"
                f"Context JSON:\n```json\n{json.dumps(user_payload, indent=2, sort_keys=True)}\n```"
            ),
        },
    ]

    def _validate_tests_payload(data: dict[str, Any]) -> list[str]:
        errs: list[str] = []
        rel_path = _safe_rel_test_path(data["file_path"], node)
        code = data["code"]
        try:
            ast.parse(code, filename=rel_path)
        except SyntaxError as exc:
            errs.append(f"Python syntax error at line {exc.lineno}: {exc.msg}")
            return errs
        trace = check_traceability(requirements, {rel_path: code})
        if not trace.passed:
            if trace.missing:
                errs.append(
                    "Missing test functions for requirement/scenario ids: "
                    + ", ".join(trace.missing)
                )
            errs.extend(trace.syntax_errors)
        return errs

    meta = {"job_id": ctx.job["id"], "prompts": [prompt.id]}
    try:
        parsed = call_structured_json(
            ctx.client,
            "reasoner",
            messages,
            AUTHOR_TESTS_SCHEMA,
            validator=_validate_tests_payload,
            tag="author_tests",
            meta=meta,
            max_repairs=3,
        )
    except StructuredOutputError as exc:
        _fail_node(ctx.store, node["id"], str(exc))
        return _make_job_result(False, error=str(exc))

    test_rel = _safe_rel_test_path(parsed["file_path"], node)
    raw_test_cmd = f"pytest -q {test_rel}"
    wrapped_cmd = tenv_mgr.wrap_test_command(ws_root, tenv, raw_test_cmd)
    env_cmd = f"PYTHONPATH=src {wrapped_cmd}" if os.name != "nt" else wrapped_cmd

    # Validate tests RUN and FAIL against NotImplementedError stubs (BLD-3)
    policy = _path_policy(writable=(test_rel,), readonly=("src/**", ".nai/**"))
    with ctx.ws.job_context(_job_context(node["id"], "author_tests", ctx.job.get("attempt", 1), policy)):
        # Ensure editable if re-authoring
        abs_test = ws_root / test_rel
        if abs_test.exists():
            os.chmod(abs_test, stat.S_IRUSR | stat.S_IWUSR)

        code = parsed["code"]
        for stub_round in range(4):
            ctx.check()
            _write_via_ws(ctx.ws, ws_root, test_rel, code)
            rc, out = _unpack_cmd_result(ctx.ws.run_command(env_cmd))
            # Must fail against stubs, and must not fail due to syntax/collection error in the test file
            bad_test_syntax = (
                "SyntaxError" in out
                or "IndentationError" in out
                or "ERROR collecting" in out
            )
            # BLD-3: a valid failing test must fail *because the stub is not
            # implemented*. assert False / NameError are not acceptable.
            fails_on_stub = "NotImplementedError" in out
            if rc != 0 and not bad_test_syntax and fails_on_stub:
                break

            if stub_round == 3:
                reason = (
                    "Authored tests passed against NotImplementedError stubs"
                    if rc == 0
                    else f"Authored tests had collection/syntax errors: {out}"
                )
                _fail_node(ctx.store, node["id"], reason)
                return _make_job_result(False, error=reason, output=out)

            repair_msg = (
                "Tests passed against NotImplementedError stubs (exit 0); tests must fail against stubs!"
                if rc == 0
                else f"Tests failed collection/syntax check:\n{out}"
            )
            messages.append({"role": "assistant", "content": json.dumps(parsed)})
            messages.append({"role": "user", "content": repair_msg + "\nReturn corrected JSON."})
            parsed = call_structured_json(
                ctx.client,
                "reasoner",
                messages,
                AUTHOR_TESTS_SCHEMA,
                validator=_validate_tests_payload,
                tag="author_tests_repair",
                meta=meta,
                max_repairs=3,
            )
            code = parsed["code"]
            test_rel = _safe_rel_test_path(parsed["file_path"], node)

        # Freeze tests: compute sha1, make read-only on disk, record TestRecord & test_command (BLD-3, CORE-3)
        frozen_hash = hashlib.sha1(code.encode("utf-8")).hexdigest()
        if abs_test.exists():
            os.chmod(abs_test, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)

        req_ids = [str(r["id"]) for r in requirements if r.get("testable", True)]
        ctx.store.set_node_test_command(node["id"], raw_test_cmd)
        ctx.store.upsert_test_record(
            node["id"],
            test_rel,
            req_ids,
            frozen_hash,
            last_result="fail",
            last_run_at=_utc_now(),
        )
        ctx.artifact("test", code, Path(test_rel).name)
        if hasattr(ctx.ws, "checkpoint_job"):
            ctx.ws.checkpoint_job()

    ctx.event(
        "build.tests_frozen",
        f"Frozen tests at {test_rel} covering {len(req_ids)} requirements",
        data={"file_path": test_rel, "frozen_hash": frozen_hash, "requirement_ids": req_ids},
    )
    return _make_job_result(
        True,
        file_path=test_rel,
        frozen_hash=frozen_hash,
        requirement_ids=req_ids,
        content_hash=node.get("content_hash"),
    )


# ---------------------------------------------------------------------------
# BLD-5: implement
# ---------------------------------------------------------------------------


def _run_coder(
    ctx: Any,
    task: str,
    context_files: list[str],
    allow_bash: bool,
    tools_bundle: Any,
) -> str:
    from nai.coder import Coder  # type: ignore

    settings = (ctx.project or {}).get("settings") or {}
    max_steps = int(settings.get("coder_max_steps", 20))
    coder = Coder(
        ctx.client,
        ctx.ws,
        max_steps,
        allow_bash=allow_bash,
        extra_tools=tools_bundle.openai_tools or None,
        tool_runner=tools_bundle.tool_runner if tools_bundle.allowlist else None,
    )
    return coder.run(task, context_files)


def handle_implement(ctx: Any) -> Any:
    """Run Coder loop on a single component with path policy and context pack (BLD-1, BLD-5)."""
    ctx.check()
    project = ctx.project
    node = _ensure_running(ctx.store, ctx.node)
    ws_root = _workspace_root(ctx)
    parent_contract = _parent_contract(ctx.store, node)

    tenv = parse_target_env(project, ws_root)
    TargetEnvManager().ensure_env(ws_root, tenv)
    tools_bundle = build_tools_bundle(ctx.store, project, node, ws_root)

    coder_ctx = 32768
    if ctx.profile is not None and getattr(ctx.profile, "coder", None) is not None:
        coder_ctx = int(ctx.profile.coder.ctx)

    pack = assemble_context_pack(
        ctx.store,
        project,
        node,
        tenv,
        coder_ctx,
        parent_contract=parent_contract,
        workspace_root=ws_root,
        tools_section=tools_bundle.context_section,
    )
    ctx.artifact("report", pack.text, f"context_pack_{node['name']}_implement.md")

    if not pack.fits:
        msg = (
            f"Context pack for {node['name']} ({pack.estimated_tokens} tokens) exceeds "
            f"budget ({pack.max_tokens} tokens); node needs further decomposition (BLD-1)."
        )
        ctx.event(
            "build.needs_decomposition",
            msg,
            level="warning",
            data={"estimated_tokens": pack.estimated_tokens, "max_tokens": pack.max_tokens},
        )
        _fail_node(ctx.store, node["id"], msg, data={"needs_decomposition": True})
        return _make_job_result(False, error=msg, needs_decomposition=True)

    # Path policy: node may write ONLY its own source files; tests & .nai are strictly read-only
    stub_rel = node_stub_rel_path(project, node)
    pkg = project_package_name(project)
    mod = slugify_module(node["name"])
    writable = (stub_rel, f"src/{pkg}/{mod}/**")
    readonly = ("tests/**", ".nai/**", "conftest.py", "pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini", entities_rel_path(project))
    policy = _path_policy(writable=writable, readonly=readonly)

    input_ref = ctx.job.get("input_ref") or {}
    retry_feedback = input_ref.get("retry_feedback") or ""
    diag_brief = input_ref.get("revised_brief") or ""

    prompt = get_prompt("implement")
    task_parts = [prompt.text, pack.text, f"Target writable file: `{stub_rel}`."]
    if diag_brief:
        task_parts.append(f"## Diagnostic Brief (BLD-9)\n{diag_brief}")
    if retry_feedback:
        task_parts.append(f"## Previous Failure & Lint Findings\n{retry_feedback}")
    task = "\n\n".join(task_parts)

    context_files = [stub_rel]
    for tr in ctx.store.list_test_records(node["id"]):
        if tr.get("file_path"):
            context_files.append(tr["file_path"])

    settings = project.get("settings") or {}
    allow_bash = bool(settings.get("coder_allow_bash", False))

    with ctx.ws.job_context(
        _job_context(node["id"], "implement", ctx.job.get("attempt", 1), policy)
    ):
        before_hashes = ctx.ws.protected_hashes(policy) if hasattr(ctx.ws, "protected_hashes") else {}
        summary = _run_coder(ctx, task, context_files, allow_bash, tools_bundle)
        after_hashes = ctx.ws.protected_hashes(policy) if hasattr(ctx.ws, "protected_hashes") else {}

        if before_hashes != after_hashes:
            msg = f"Protected read-only files modified during implement job for {node['name']}"
            ctx.event("build.protected_tamper", msg, level="error")
            _fail_node(ctx.store, node["id"], msg)
            return _make_job_result(False, error=msg)

        if hasattr(ctx.ws, "checkpoint_job"):
            ctx.ws.checkpoint_job()

    ctx.event("build.implemented", f"Implementation step finished for {node['name']}")
    # The runner counts attempts at job start; direct handler callers (tests and
    # legacy integrations without a runner) need the handler to record it.
    store_proxy_type = type(ctx.store).__name__
    if store_proxy_type != "StoreProxy":
        ctx.store.increment_node_attempts(node["id"], 1)
    return _make_job_result(True, summary=summary, writable=list(writable), prompt_id=prompt.id, content_hash=node.get("content_hash"))


# ---------------------------------------------------------------------------
# BLD-6 & BLD-12: verify
# ---------------------------------------------------------------------------


def _run_final_acceptance(ctx: Any, ws_root: Path, tenv: TargetEnv, tenv_mgr: TargetEnvManager) -> tuple[bool, str]:
    """Run project-wide `--test-cmd` list after root integration passes (BLD-12)."""
    settings = (ctx.project or {}).get("settings") or {}
    test_cmds = list(settings.get("test_cmds") or [])
    if not test_cmds:
        return True, ""

    for cmd in test_cmds:
        wrapped = tenv_mgr.wrap_test_command(ws_root, tenv, str(cmd))
        rc, out = _unpack_cmd_result(ctx.ws.run_command(wrapped))
        if rc != 0:
            return False, f"Project acceptance command failed `{cmd}`:\n{out}"
    return True, "All project acceptance commands passed."


def handle_verify(ctx: Any) -> Any:
    """Verify tests, gate, frozen test hashes, traceability, lint gate, and stale APIs (BLD-6, TENV-2/3/6)."""
    ctx.check()
    project = ctx.project
    node = ctx.node
    ws_root = _workspace_root(ctx)

    if node["status"] == "ready":
        ctx.store.transition_node_status(node["id"], "running", note="Verify starting")
        node = ctx.store.get_node(node["id"]) or node
    if node["status"] == "running":
        ctx.store.transition_node_status(node["id"], "verifying", note="Entering verification gate")
        node = ctx.store.get_node(node["id"]) or node

    tenv = parse_target_env(project, ws_root)
    tenv_mgr = TargetEnvManager()
    tenv_mgr.ensure_env(ws_root, tenv)

    test_records = list(ctx.store.list_test_records(node["id"]) or [])
    raw_test_cmd = node.get("test_command")
    if not raw_test_cmd and test_records:
        raw_test_cmd = f"pytest -q {' '.join(tr['file_path'] for tr in test_records)}"
    if not raw_test_cmd:
        _fail_node(ctx.store, node["id"], "No test_command or frozen TestRecord found")
        return _make_job_result(False, error="No test_command configured")

    # 1. Check frozen test hashes unchanged (BLD-6)
    test_sources: dict[str, str] = {}
    for tr in test_records:
        fp = ws_root / tr["file_path"]
        if not fp.is_file():
            reason = f"Frozen test file missing: {tr['file_path']}"
            ctx.store.upsert_test_record(
                node["id"], tr["file_path"], tr["requirement_ids"], tr["frozen_hash"],
                last_result="error", last_run_at=_utc_now(),
            )
            _fail_node(ctx.store, node["id"], reason)
            return _make_job_result(False, error=reason)
        content = fp.read_text(encoding="utf-8")
        actual_hash = hashlib.sha1(content.encode("utf-8")).hexdigest()
        if actual_hash != tr["frozen_hash"]:
            reason = f"Frozen test hash mismatch on {tr['file_path']}"
            ctx.store.upsert_test_record(
                node["id"], tr["file_path"], tr["requirement_ids"], tr["frozen_hash"],
                last_result="error", last_run_at=_utc_now(),
            )
            _fail_node(ctx.store, node["id"], reason)
            return _make_job_result(False, error=reason)
        test_sources[tr["file_path"]] = content

    # 2. Traceability check (BLD-4, BLD-6)
    if node.get("kind") == "integration":
        reqs = _scenario_requirements_for_integration(_parent_contract(ctx.store, node))
    else:
        reqs = list(ctx.store.list_requirements(node["id"]) or [])
    if reqs and test_sources:
        trace = check_traceability(reqs, test_sources)
        if not trace.passed:
            reason = f"Traceability check failed; missing requirement tests: {', '.join(trace.missing)}"
            _fail_node(ctx.store, node["id"], reason)
            return _make_job_result(False, error=reason, missing_requirements=trace.missing)

    # 3. Run test_command inside target env with -W error::DeprecationWarning (TENV-2, CORE-4)
    wrapped_cmd = tenv_mgr.wrap_test_command(ws_root, tenv, raw_test_cmd)
    effective_cmd = f"PYTHONPATH=src {wrapped_cmd}" if os.name != "nt" else wrapped_cmd
    policy = _path_policy(writable=(), readonly=("src/**", "tests/**", ".nai/**"))

    with ctx.ws.job_context(
        _job_context(node["id"], "verify", ctx.job.get("attempt", 1), policy, test_command=effective_cmd)
    ):
        rc, test_out = _unpack_cmd_result(ctx.ws.run_command(effective_cmd))
        gate_problem = ctx.ws.verification_problem() if hasattr(ctx.ws, "verification_problem") else None

        # 4. Lint & type-check gate (TENV-3)
        lint_targets: list[str] = []
        stub_rel = node_stub_rel_path(project, node)
        if (ws_root / stub_rel).exists():
            lint_targets.append(stub_rel)
        lint_res = tenv_mgr.run_lint_gate(
            ws_root,
            tenv,
            lint_targets,
            run_cmd=lambda c: _unpack_cmd_result(ctx.ws.run_command(c)),
        )

    combined_output = test_out
    if lint_res.raw_output:
        combined_output = f"{test_out}\n\n{lint_res.raw_output}".strip()

    # TENV-6: staleness tracking for ImportError / AttributeError / DeprecationWarning
    for stale_hit in detect_stale_api(combined_output, tenv):
        ctx.event(
            "build.stale_api",
            f"Stale API signal for package {stale_hit['package']}: {stale_hit['kind']}",
            level="warning",
            data={"stale_api": stale_hit["package"], **stale_hit},
        )

    passed = (rc == 0) and (gate_problem is None) and lint_res.passed
    last_result = "pass" if passed else "fail"
    now = _utc_now()
    for tr in test_records:
        ctx.store.upsert_test_record(
            node["id"],
            tr["file_path"],
            tr["requirement_ids"],
            tr["frozen_hash"],
            last_result=last_result,
            last_run_at=now,
        )

    if not passed:
        failure_parts: list[str] = []
        if rc != 0:
            failure_parts.append(f"Tests exited {rc}:\n{test_out}")
        if gate_problem is not None:
            failure_parts.append(f"Verification gate failed: {gate_problem}")
        if not lint_res.passed:
            failure_parts.append("Lint gate findings:\n" + "\n".join(lint_res.findings))
        full_err = "\n\n".join(failure_parts)
        ctx.artifact("log", full_err, f"verify_fail_{node['name']}.log")
        _fail_node(ctx.store, node["id"], "Verification failed", data={"output": full_err[:4000]})
        return _make_job_result(
            False,
            error="Verification failed",
            output=full_err,
            lint_findings=lint_res.findings,
        )

    # BLD-12: If this is the root's integration node, run final project acceptance commands
    if node.get("kind") == "integration" and node.get("parent_id"):
        parent = ctx.store.get_node(node["parent_id"])
        if parent is not None and parent.get("parent_id") is None:
            acc_ok, acc_msg = _run_final_acceptance(ctx, ws_root, tenv, tenv_mgr)
            if not acc_ok:
                ctx.artifact("log", acc_msg, f"final_acceptance_fail_{node['name']}.log")
                _fail_node(ctx.store, node["id"], acc_msg)
                return _make_job_result(False, error=acc_msg, output=acc_msg)
            ctx.store.transition_node_status(node["id"], "done", note="Root integration & final acceptance passed")
            if parent["status"] in ("approved", "ready"):
                ctx.store.transition_node_status(parent["id"], "running", note="Finalizing root")
                ctx.store.transition_node_status(parent["id"], "verifying", note="Root verified by integration")
            if ctx.store.get_node(parent["id"])["status"] in ("running", "verifying"):
                ctx.store.transition_node_status(parent["id"], "done", note="Project final acceptance passed (BLD-12)")
            if hasattr(ctx.store, "update_project_status"):
                ctx.store.update_project_status(project["id"], "done")
            ctx.event("project.done", f"Project {project['name']} passed final acceptance (BLD-12)")
            return _make_job_result(True, output=test_out, final_acceptance=True)

    ctx.store.transition_node_status(node["id"], "done", note="Verification passed (BLD-6)")
    ctx.event("build.verified", f"Verification passed for {node['name']}")
    return _make_job_result(True, output=test_out)


# ---------------------------------------------------------------------------
# BLD-7: integrate
# ---------------------------------------------------------------------------


def handle_integrate(ctx: Any) -> Any:
    """Assemble and wire child components to pass parent scenario tests (BLD-7, D6)."""
    ctx.check()
    project = ctx.project
    node = _ensure_running(ctx.store, ctx.node)
    ws_root = _workspace_root(ctx)
    parent = ctx.store.get_node(node["parent_id"]) if node.get("parent_id") else None
    parent_contract = parent.get("contract") if parent else {}

    tenv = parse_target_env(project, ws_root)
    TargetEnvManager().ensure_env(ws_root, tenv)
    tools_bundle = build_tools_bundle(ctx.store, project, node, ws_root)

    pkg = project_package_name(project)
    mod = slugify_module(node["name"])
    wiring_rel = f"src/{pkg}/{mod}.py"
    policy = _path_policy(writable=(wiring_rel,), readonly=("tests/**", ".nai/**"))

    prompt = get_prompt("integrate")
    children = ctx.store.list_nodes(project["id"], parent_id=parent["id"]) if parent else []
    child_summaries = [
        {"name": c["name"], "kind": c["kind"], "status": c["status"], "stub": node_stub_rel_path(project, c)}
        for c in children
        if c["id"] != node["id"]
    ]
    task = (
        f"{prompt.text}\n\n"
        f"{format_env_block(tenv)}\n\n"
        f"Parent node: `{(parent or {}).get('name', 'root')}`\n"
        f"Writable integration glue path: `{wiring_rel}`\n"
        f"Children:\n```json\n{json.dumps(child_summaries, indent=2)}\n```\n"
        f"Scenarios:\n```json\n{json.dumps((parent_contract or {}).get('scenarios') or [], indent=2)}\n```"
    )

    with ctx.ws.job_context(_job_context(node["id"], "integrate", ctx.job.get("attempt", 1), policy)):
        # D6: ConsultThreads side conversation allowed inside integrate, transcript stored as artifact
        try:
            from nai.consult import ConsultThreads

            consult = ConsultThreads(ctx.client, ctx.ws)
            advice = consult.consult(
                f"integrate_{node['id']}",
                f"Review integration wiring for scenarios: {json.dumps((parent_contract or {}).get('scenarios') or [])}",
            )
            ctx.artifact("log", str(advice), f"integrate_consult_{node['name']}.log")
        except Exception:
            pass

        summary = _run_coder(ctx, task, [wiring_rel], False, tools_bundle)
        if hasattr(ctx.ws, "checkpoint_job"):
            ctx.ws.checkpoint_job()

    return _make_job_result(True, summary=summary, prompt_id=prompt.id, content_hash=node.get("content_hash"))


# ---------------------------------------------------------------------------
# BLD-9: diagnose
# ---------------------------------------------------------------------------


def handle_diagnose(ctx: Any) -> Any:
    """Diagnose repeated verification failures with reasoner model (BLD-9, D6)."""
    ctx.check()
    project = ctx.project
    node = ctx.node
    ws_root = _workspace_root(ctx)
    parent_contract = _parent_contract(ctx.store, node)
    tenv = parse_target_env(project, ws_root)

    input_ref = ctx.job.get("input_ref") or {}
    escalated_pass = bool(input_ref.get("escalated_pass", False))
    failure_output = str(input_ref.get("failure_output") or "")

    prompt = get_prompt("diagnose")
    max_tokens = None
    if escalated_pass and ctx.profile is not None and getattr(ctx.profile, "reasoner", None) is not None:
        max_tokens = int(ctx.profile.reasoner.max_tokens)

    # D6: Optional ConsultThreads side conversation in diagnose, transcript saved as artifact
    consult_note = ""
    if ctx.ws is not None:
        try:
            from nai.consult import ConsultThreads

            consult = ConsultThreads(ctx.client, ctx.ws)
            consult_note = consult.consult(
                f"diagnose_{node['id']}",
                f"Inspect failing output for `{node['name']}`:\n{failure_output[:3000]}",
            )
            ctx.artifact("log", str(consult_note), f"diagnose_consult_{node['name']}.log")
        except Exception:
            consult_note = ""

    payload = {
        "node_name": node["name"],
        "objective": node.get("objective") or "",
        "spec_text": node.get("spec_text") or "",
        "contract": parent_contract or {},
        "failure_output": failure_output[:6000],
        "consult_notes": consult_note[:2000],
        "escalated_pass": escalated_pass,
    }
    messages = [
        {"role": "system", "content": prompt.text},
        {
            "role": "user",
            "content": (
                f"{format_env_block(tenv)}\n\n"
                f"Diagnose this failure:\n```json\n{json.dumps(payload, indent=2, sort_keys=True)}\n```"
            ),
        },
    ]

    meta = {"job_id": ctx.job["id"], "prompts": [prompt.id]}
    try:
        diag = call_structured_json(
            ctx.client,
            "reasoner",
            messages,
            DIAGNOSE_SCHEMA,
            max_tokens=max_tokens,
            tag="diagnose_escalated" if escalated_pass else "diagnose",
            meta=meta,
            max_repairs=3,
        )
    except StructuredOutputError as exc:
        return _make_job_result(False, error=str(exc))

    ctx.artifact(
        "report",
        json.dumps(diag, indent=2, sort_keys=True),
        f"diagnosis_{node['name']}_{'escalated' if escalated_pass else 'std'}.json",
    )

    fault_kind = diag["fault_kind"]
    if not escalated_pass:
        if fault_kind == "test":
            # BLD-9: test faults go to BLD-3 after owner approval; escalate so owner approves
            if ctx.store.get_node(node["id"])["status"] == "ready":
                ctx.store.transition_node_status(node["id"], "running", note="Diagnose found test fault")
            if ctx.store.get_node(node["id"])["status"] == "running":
                ctx.store.transition_node_status(node["id"], "failed", note=diag["summary"])
            if ctx.store.get_node(node["id"])["status"] == "failed":
                ctx.store.transition_node_status(node["id"], "escalated", note="Test fault requires owner approval before re-authoring (BLD-9)")
            ctx.event(
                "build.test_reauthor_requested",
                f"Diagnose flagged test fault for {node['name']}: {diag['summary']}",
                level="warning",
                data=diag,
            )
        elif fault_kind in ("contract", "environment"):
            raise_change_request(
                ctx.store,
                project,
                node,
                diag["summary"],
                missing_package=diag.get("missing_package"),
                details=diag,
            )
        elif fault_kind == "spec":
            if ctx.store.get_node(node["id"])["status"] == "ready":
                ctx.store.transition_node_status(node["id"], "running", note="Diagnose found spec fault")
            if ctx.store.get_node(node["id"])["status"] == "running":
                ctx.store.transition_node_status(node["id"], "failed", note=diag["summary"])
            if ctx.store.get_node(node["id"])["status"] == "failed":
                ctx.store.transition_node_status(node["id"], "escalated", note=f"Spec fault: {diag['summary']}")

    return _make_job_result(True, **diag, prompt_id=prompt.id)


# ---------------------------------------------------------------------------
# BLD-11: review (optional)
# ---------------------------------------------------------------------------


def handle_review(ctx: Any) -> Any:
    """Optional post-implementation reasoner review against spec (BLD-11)."""
    ctx.check()
    project = ctx.project
    node = ctx.node
    ws_root = _workspace_root(ctx)
    tenv = parse_target_env(project, ws_root)
    stub_rel = node_stub_rel_path(project, node)
    impl_code = (ws_root / stub_rel).read_text(encoding="utf-8") if (ws_root / stub_rel).is_file() else ""

    prompt = get_prompt("review")
    payload = {
        "node_name": node["name"],
        "spec_text": node.get("spec_text") or "",
        "implementation_path": stub_rel,
        "implementation_code": impl_code,
    }
    messages = [
        {"role": "system", "content": prompt.text},
        {"role": "user", "content": f"{format_env_block(tenv)}\n\n```json\n{json.dumps(payload, indent=2)}\n```"},
    ]

    try:
        rev = call_structured_json(
            ctx.client,
            "reasoner",
            messages,
            REVIEW_SCHEMA,
            tag="review",
            meta={"job_id": ctx.job["id"], "prompts": [prompt.id]},
            max_repairs=3,
        )
    except StructuredOutputError as exc:
        return _make_job_result(False, error=str(exc))

    ctx.artifact("report", json.dumps(rev, indent=2, sort_keys=True), f"review_{node['name']}.json")

    mismatches = list(rev.get("mismatches") or [])
    if not rev.get("approved", True) and mismatches:
        # BLD-11: findings become new failing tests or change requests, never direct edits
        contract_issues = [m for m in mismatches if m.get("kind") == "contract_mismatch"]
        if contract_issues:
            raise_change_request(
                ctx.store,
                project,
                node,
                contract_issues[0]["description"],
                details={"mismatches": contract_issues},
            )
        else:
            ctx.event(
                "build.review_mismatches",
                f"Review found {len(mismatches)} mismatch(es) for {node['name']}",
                level="warning",
                data={"mismatches": mismatches},
            )
            # Re-open for additional test authoring
            if node["status"] == "done":
                ctx.store.transition_node_status(node["id"], "stale", note="Review found spec mismatches (BLD-11)")
                ctx.store.transition_node_status(node["id"], "ready", note="Re-queueing for review test authoring")
            ctx.enqueue("author_tests", "reasoner", input_ref={"review_mismatches": mismatches})

    return _make_job_result(True, **rev, prompt_id=prompt.id)
