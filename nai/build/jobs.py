"""SS-BUILD job handlers executed by the WP3 scheduler."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import manifest, paths
from ..exec import PHASE_RUN, PHASE_SETUP, needs_shell, shell_argv
from ..langs import knowledge, registry
from ..langs.base import Outcome
from ..remote import routing as remote_routing
from ..remote.recipes import spec_for
from ..remote.target import RemoteTarget
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
from .tenv import (
    LintResult,
    TargetEnv,
    TargetEnvManager,
    detect_stale_api,
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


def _exec_target(ctx: Any) -> Any:
    """The workspace's execution target, when the workspace exposes one."""
    return getattr(ctx.ws, "exec_target", None)


def _run_argv(
    ctx: Any,
    argv: Any,
    *,
    phase: str = PHASE_RUN,
    env_overrides: dict[str, str] | None = None,
    network: bool | None = None,
) -> tuple[int, str]:
    """Run argv through the workspace's execution target and unpack the result."""
    res = ctx.ws.run_argv(
        argv, phase=phase, env_overrides=env_overrides, network=network
    )
    return _unpack_cmd_result(res)


def _run_string(ctx: Any, command: str, **kwargs: Any) -> tuple[int, str]:
    """Run a command string; shell syntax becomes an explicit shell step."""
    argv = shell_argv(command) if needs_shell(command) else shlex.split(command)
    return _run_argv(ctx, argv, **kwargs)


def _remote_block(project: Any) -> dict[str, Any]:
    """``settings.remote`` when it is a table (enabled flag, waits, extra caps)."""
    settings = (project or {}).get("settings") or {}
    block = settings.get("remote") if isinstance(settings, dict) else None
    return block if isinstance(block, dict) else {}


def _cmd_timeout(ctx: Any) -> float:
    """The project's command timeout, used for both local and remote runs."""
    settings = (ctx.project or {}).get("settings") or {}
    try:
        return float(settings.get("cmd_timeout") or 600)
    except (TypeError, ValueError):
        return 600.0


def _argv_targets(argv: Any, ws_root: Path) -> list[str]:
    """Workspace-relative paths named in an argv (a recipe's ``targets``)."""
    root = ws_root.resolve()
    targets: list[str] = []
    for part in list(argv)[1:]:
        text = str(part)
        if not text or text.startswith("-") or "=" in text:
            continue
        path = Path(text)
        candidate = path if path.is_absolute() else (root / path)
        try:
            rel = candidate.resolve().relative_to(root).as_posix()
        except (ValueError, OSError):
            continue
        if rel not in targets and (root / rel).exists():
            targets.append(rel)
    return targets


def _remote_target(
    ctx: Any,
    node: dict[str, Any],
    job_kind: str,
    recipe: str,
    *,
    params: dict[str, Any] | None = None,
    ws_root: Path | None = None,
) -> RemoteTarget | None:
    """A :class:`RemoteTarget` when this job's commands belong on a worker."""
    project = ctx.project
    block = _remote_block(project)
    if not remote_routing.remote_enabled(project):
        if remote_routing.node_platform(ctx.store, node) == remote_routing.MACOS:
            ctx.event(
                "remote.disabled",
                f"{node['name']} builds for macos but settings.remote.enabled is false; running locally",
                level="warning",
            )
        return None
    remote, reason = remote_routing.route_for(project, node, job_kind, store=ctx.store)
    if not remote:
        return None
    root = Path(ws_root or _workspace_root(ctx))
    adapter = _adapter_for(project)
    caps = set(spec_for(recipe).capabilities)
    platform = remote_routing.resolved_platform(ctx.store, project, node)
    if platform != "any":
        caps.add(platform)
    caps.update(str(c).strip().lower() for c in (block.get("capabilities") or []) if str(c).strip())
    ctx.event(
        "remote.routed",
        f"{job_kind} runs on a worker ({reason})",
        data={"recipe": recipe, "caps": sorted(caps)},
    )
    return RemoteTarget(
        root,
        ctx.store,
        project_id=project["id"],
        job_id=ctx.job.get("id"),
        recipe=recipe,
        params=params or {},
        required_caps=sorted(caps),
        snapshot_ignore=adapter.snapshot_ignore(),
        snapshot_dir=root / ".nai" / "snapshots",
        cache_dirs=adapter.cache_dirs(),
        worker_wait_seconds=float(block.get("worker_wait_seconds", 60.0)),
        poll_seconds=float(block.get("poll_seconds", 0.25)),
        keep_snapshots=int(block.get("keep_snapshots", 5)),
        default_timeout=_cmd_timeout(ctx),
        on_note=lambda kind, message: ctx.event(kind, message),
    )


def _ensure_target_env(ctx: Any, project: dict[str, Any], ws_root: Path, adapter: Any) -> tuple[Any, Any]:
    """Create the pinned environment only when the adapter uses one.

    Python builds inside ``.nai/venv``; Swift uses the host toolchain, so there
    is nothing to create.  Returns ``(tenv, manager)`` or ``(None, None)``.
    """
    if not adapter.needs_target_env():
        return None, None
    tenv = parse_target_env(project, ws_root)
    manager = TargetEnvManager(_exec_target(ctx))
    manager.ensure_env(ws_root, tenv)
    return tenv, manager


def _prepare_workspace(ctx: Any, project: dict[str, Any], ws_root: Path, adapter: Any) -> tuple[tuple[str, ...], str | None]:
    """Let the adapter regenerate the files it owns (for example ``Package.swift``).

    Returns ``(written, error)``; an error fails the job rather than running
    commands against a stale manifest.
    """
    try:
        written = tuple(adapter.prepare_workspace(project, ws_root))
    except Exception as exc:  # noqa: BLE001 - a broken adapter must fail the job, not crash it
        return (), f"{type(exc).__name__}: {exc}"
    for rel in written:
        path = ws_root / rel
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            manifest.record(ws_root, rel, node_id=ctx.job.get("node_id"), stage="generated", content=content)
        except (OSError, ValueError):
            pass
        ctx.artifact("code", content, Path(rel).name)
    return written, None


def _command_argv(
    ctx: Any, project: dict[str, Any], ws_root: Path, adapter: Any, command: Any
) -> tuple[list[str], dict[str, str]]:
    """Wrap an adapter command: Python runs inside its venv, others as declared.

    Accepts a ``Command``, an argv sequence or a plain command string; a string
    with shell syntax becomes an explicit shell step.
    """
    if isinstance(command, str):
        argv = shell_argv(command) if needs_shell(command) else shlex.split(command)
    else:
        argv = [str(part) for part in getattr(command, "argv", command)]
    if adapter.needs_target_env():
        tenv = parse_target_env(project, ws_root)
        return TargetEnvManager(_exec_target(ctx)).command_argv(ws_root, tenv, argv)
    return argv, {}


def _run_lint_gate(
    ctx: Any,
    project: dict[str, Any],
    ws_root: Path,
    node: dict[str, Any],
    adapter: Any,
    tenv: Any,
    tenv_mgr: Any,
    lint_targets: list[str],
    run_argv: Any,
) -> Any:
    """Run the adapter's lint command plus its platform import rules.

    An adapter without a lint command (Swift) skips the tool gate; its platform
    import rules still run, so a portable component cannot import SwiftUI, UIKit
    or AppKit.
    """
    platform = remote_routing.resolved_platform(ctx.store, project, node)
    findings: list[str] = []
    tools: list[str] = []
    raw_parts: list[str] = []
    passed = True
    if (
        lint_targets
        and tenv_mgr is not None
        and adapter.lint_command(project, ws_root, tuple(lint_targets)) is not None
    ):
        result = tenv_mgr.run_lint_gate(ws_root, tenv, lint_targets, run_argv=run_argv)
        passed = result.passed
        findings.extend(result.findings)
        tools.extend(result.tools_run)
        raw_parts.append(result.raw_output)
    platform_findings = adapter.platform_lint(project, node, ws_root, platform=platform)
    if platform_findings:
        passed = False
        findings.extend(platform_findings)
        tools.append("platform-imports")
        raw_parts.append("\n".join(platform_findings))
    return LintResult(
        passed=passed,
        findings=findings,
        raw_output="\n".join(part for part in raw_parts if part).strip(),
        tools_run=tools,
    )


def _syntax_precheck(ws_root: Path, writable: tuple[str, ...], adapter: Any) -> str | None:
    """Adapter syntax check for files a macos worker would reject on import.

    ``implement`` stays on the orchestrator even for macos nodes (the coder is a
    model job); this hook catches the cheap mistakes before a round trip.
    """
    for rel in writable:
        if "*" in rel:
            continue
        path = ws_root / rel
        if not path.is_file():
            continue
        try:
            problem = adapter.check_syntax(path.read_text(encoding="utf-8", errors="replace"), rel)
        except Exception as exc:  # noqa: BLE001 - a broken adapter must not crash the job
            return f"{rel}: {type(exc).__name__}: {exc}"
        if problem:
            return f"{rel}: {problem}"
    return None


def _coder_command_policy(ctx: Any, node: dict[str, Any]) -> tuple[bool, bool]:
    """``(allow_bash, macos_remote)`` for a coder run.

    For macos components the coder cannot run commands: verification happens on
    a worker, so implement relies on verify feedback plus a syntax pre-check.
    """
    settings = (ctx.project or {}).get("settings") or {}
    macos_remote = remote_routing.remote_enabled(ctx.project) and (
        remote_routing.node_platform(ctx.store, node) == remote_routing.MACOS
    )
    allow_bash = bool(settings.get("coder_allow_bash", False)) and not macos_remote
    if macos_remote and settings.get("coder_allow_bash"):
        ctx.event(
            "remote.run_command_disabled",
            f"run_command is disabled for {node['name']}: macos components are verified on a worker",
            level="warning",
        )
    return allow_bash, macos_remote


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


def _safe_rel_test_path(raw_path: str, node: dict[str, Any], adapter: Any) -> str:
    """Normalise a model-supplied test path through the language adapter."""
    return adapter.normalize_test_path(raw_path, node)


def _adapter_for(project: dict[str, Any]) -> Any:
    """Return the language adapter selected by ``project.settings.language``."""
    return registry.language_for(project)


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
        target = paths.resolve_relative(ws_root, rel_path)
        if target.exists():
            return
    target = paths.resolve_relative(ws_root, rel_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _guard_manifest(ctx: Any, ws_root: Path) -> Any | None:
    """Fail a job instead of silently overwriting a hand-edited tracked file."""
    force = bool((ctx.job.get("input_ref") or {}).get("force_manifest"))
    mismatches = manifest.detect_hand_edits(ctx.store, ctx.project, force=force)
    if mismatches and not force:
        details = ", ".join(f"{m.relpath} ({m.reason})" for m in mismatches[:5])
        return _make_job_result(False, error=f"hand edit detected: {details}")
    return None


def _record_touched(ctx: Any, ws_root: Path, node_id: str, stage: str) -> None:
    touched = sorted(getattr(ctx.ws, "touched", set()) or set())
    for rel in touched:
        try:
            manifest.record(ws_root, rel, node_id=node_id, stage=stage)
        except (OSError, ValueError):
            continue


# ---------------------------------------------------------------------------
# BLD-2: contract_stub
# ---------------------------------------------------------------------------


def handle_contract_stub(ctx: Any) -> Any:
    """Generate and validate importable contract stubs from frozen parent contract (BLD-2)."""
    ctx.check()
    project = ctx.project
    node = _ensure_running(ctx.store, ctx.node)
    ws_root = _workspace_root(ctx)
    guard = _guard_manifest(ctx, ws_root)
    if guard is not None:
        return guard
    parent_contract = _parent_contract(ctx.store, node)

    adapter = _adapter_for(project)
    _tenv, tenv_mgr = _ensure_target_env(ctx, project, ws_root, adapter)
    py_exe = (
        str(tenv_mgr.venv_python(ws_root, _tenv))
        if tenv_mgr is not None and _tenv is not None
        else None
    )
    stub_res = adapter.render_stub(project, node, parent_contract)
    writable = tuple(f.rel_path for f in stub_res.files)
    policy = _path_policy(writable=writable, readonly=("tests/**", ".nai/**"))

    with ctx.ws.job_context(_job_context(node["id"], "contract_stub", ctx.job.get("attempt", 1), policy)):
        for sf in stub_res.files:
            _write_via_ws(ctx.ws, ws_root, sf.rel_path, sf.content)
            try:
                manifest.record(ws_root, sf.rel_path, node_id=node["id"], stage="stub", content=sf.content)
            except (OSError, ValueError):
                pass
            ctx.artifact("code", sf.content, Path(sf.rel_path).name)

        # The adapter owns generated build files (Swift's Package.swift); write
        # them after the stubs so every declared target has sources.
        _prepared, prepare_error = _prepare_workspace(ctx, project, ws_root, adapter)
        if prepare_error:
            _fail_node(ctx.store, node["id"], f"Workspace preparation failed: {prepare_error}")
            return _make_job_result(False, error=f"Workspace preparation failed: {prepare_error}")

        def _run(cmd: str) -> tuple[int, str]:
            # The adapter hands us a shell-quoted string; split it into argv so
            # no shell is involved (run phase: no network).
            return _run_string(ctx, cmd, phase=PHASE_RUN)

        def _run_setup(cmd: str) -> tuple[int, str]:
            # Dependency installation (npm) belongs to the setup phase, where the
            # network is allowed; everything else stays in the run phase.
            return _run_string(ctx, cmd, phase=PHASE_SETUP)

        ok, out = adapter.validate_stub(
            ws_root,
            stub_res.module_import,
            _run,
            executable=py_exe,
            run_setup_cmd=_run_setup,
        )
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
    guard = _guard_manifest(ctx, ws_root)
    if guard is not None:
        return guard
    parent_contract = _parent_contract(ctx.store, node)

    adapter = _adapter_for(project)
    tenv, tenv_mgr = _ensure_target_env(ctx, project, ws_root, adapter)

    is_integration = node.get("kind") == "integration"
    if is_integration:
        requirements = _scenario_requirements_for_integration(parent_contract)
    else:
        requirements = list(ctx.store.list_requirements(node["id"]) or [])

    if not requirements:
        requirements = [{"id": "R1", "text": node.get("objective") or node["name"], "testable": True}]

    prompt = get_prompt("author_tests")
    default_test_path = adapter.test_rel(project, node)

    # Never include implementation source; only spec, contract interfaces, and TENV-4/5
    user_payload = {
        "node_name": node["name"],
        "node_kind": node["kind"],
        "suggested_test_path": default_test_path,
        "import_module": adapter.module_import(project, node),
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
                f"{adapter.environment_block(project, ws_root)}\n\n"
                f"{knowledge.block(adapter.id, 'author_tests')}\n\n"
                f"Author {adapter.test_framework} tests for this component/integration node.\n"
                f"Context JSON:\n```json\n{json.dumps(user_payload, indent=2, sort_keys=True)}\n```"
            ).replace("\n\n\n", "\n\n"),
        },
    ]

    def _validate_tests_payload(data: dict[str, Any]) -> list[str]:
        errs: list[str] = []
        rel_path = _safe_rel_test_path(data["file_path"], node, adapter)
        code = data["code"]
        syntax_error = _adapter_for(project).check_syntax(code, rel_path)
        if syntax_error:
            errs.append(syntax_error)
            return errs
        trace = check_traceability(requirements, {rel_path: code}, extractor=adapter.extract_test_functions)
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

    test_rel = _safe_rel_test_path(parsed["file_path"], node, adapter)
    test_cmd = adapter.test_command(project, node, paths=(test_rel,))
    raw_test_cmd = shlex.join(test_cmd.argv)
    test_argv, test_env = _command_argv(ctx, project, ws_root, adapter, test_cmd)
    test_env = {**adapter.env_overrides(project, ws_root), **test_env}

    # Validate tests RUN and FAIL against the adapter's stubs (BLD-3)
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
            # Regenerate build files after the test file exists so the test
            # target is declared (SwiftPM needs its Sources/Tests directory).
            _prepared, prepare_error = _prepare_workspace(ctx, project, ws_root, adapter)
            if prepare_error:
                _fail_node(ctx.store, node["id"], f"Workspace preparation failed: {prepare_error}")
                return _make_job_result(False, error=f"Workspace preparation failed: {prepare_error}")
            platform = remote_routing.resolved_platform(ctx.store, project, node)
            recipe = adapter.remote_recipe(project, node, "author_tests", platform=platform)
            params = adapter.remote_params(
                project, node, "author_tests",
                targets=tuple(_argv_targets(test_argv, ws_root)), platform=platform,
            )
            remote_tests = _remote_target(ctx, node, "author_tests", recipe, params=params, ws_root=ws_root)
            if remote_tests is not None:
                try:
                    rc, out = _unpack_cmd_result(
                        remote_tests.run(test_argv, timeout=_cmd_timeout(ctx), cancel_event=ctx.cancel_event)
                    )
                finally:
                    remote_tests.close()
            else:
                rc, out = _run_argv(ctx, test_argv, phase=PHASE_RUN, env_overrides=test_env)
            # A real syntax error in the test itself is never an acceptable
            # pre-implementation failure.  BLD-3: a valid failing test must fail
            # *because the stub is not implemented*; assert False / NameError are
            # not acceptable.  BLD-7: integration glue has no contract stub, so a
            # ModuleNotFoundError for exactly the wiring module is the expected
            # pre-implementation state.  Other import failures still go to repair.
            outcome = adapter.parse_result(out, rc, "test")
            expected_module = adapter.module_import(project, node) if is_integration else None
            if rc != 0 and adapter.stub_failure_is_valid(
                outcome, integration=is_integration, expected_module=expected_module
            ):
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
            test_rel = _safe_rel_test_path(parsed["file_path"], node, adapter)

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
        try:
            manifest.record(ws_root, test_rel, node_id=node["id"], stage="test", content=code)
        except (OSError, ValueError):
            pass
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
    guard = _guard_manifest(ctx, ws_root)
    if guard is not None:
        return guard
    parent_contract = _parent_contract(ctx.store, node)

    adapter = _adapter_for(project)
    tenv = parse_target_env(project, ws_root)
    if adapter.needs_target_env():
        TargetEnvManager(_exec_target(ctx)).ensure_env(ws_root, tenv)
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
        job_kind="implement",
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
    stub_rel = adapter.source_rel(project, node)
    writable = adapter.writable_globs(project, node)
    readonly = adapter.protected_globs(project)
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

    allow_bash, _macos_remote = _coder_command_policy(ctx, node)

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

        # Cheap adapter syntax check before a full build (Python: ast, Swift:
        # `swiftc -parse` when a toolchain is installed).
        syntax_problem = _syntax_precheck(ws_root, writable, adapter)
        if syntax_problem:
            msg = f"Syntax pre-check failed: {syntax_problem}"
            ctx.event("build.syntax_precheck_failed", msg, level="warning")
            return _make_job_result(False, error=msg, output=syntax_problem)

        if hasattr(ctx.ws, "checkpoint_job"):
            ctx.ws.checkpoint_job()

    _record_touched(ctx, ws_root, node["id"], "implement")
    try:
        if (ws_root / stub_rel).is_file():
            manifest.record(ws_root, stub_rel, node_id=node["id"], stage="implement")
    except (OSError, ValueError):
        pass
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


def _run_final_acceptance(
    ctx: Any, ws_root: Path, tenv: TargetEnv, tenv_mgr: TargetEnvManager, adapter: Any
) -> tuple[bool, str]:
    """Run project-wide `--test-cmd` list after root integration passes (BLD-12)."""
    settings = (ctx.project or {}).get("settings") or {}
    test_cmds = list(settings.get("test_cmds") or [])
    if not test_cmds:
        return True, ""

    for cmd in test_cmds:
        argv, env = _command_argv(ctx, ctx.project, ws_root, adapter, str(cmd))
        rc, out = _run_argv(ctx, argv, phase=PHASE_RUN, env_overrides=env)
        if rc != 0:
            return False, f"Project acceptance command failed `{cmd}`:\n{out}"
    return True, "All project acceptance commands passed."


def handle_verify(ctx: Any) -> Any:
    """Verify tests, gate, frozen test hashes, traceability, lint gate, and stale APIs (BLD-6, TENV-2/3/6)."""
    ctx.check()
    project = ctx.project
    node = ctx.node
    ws_root = _workspace_root(ctx)
    guard = _guard_manifest(ctx, ws_root)
    if guard is not None:
        return guard

    if node["status"] == "ready":
        ctx.store.transition_node_status(node["id"], "running", note="Verify starting")
        node = ctx.store.get_node(node["id"]) or node
    if node["status"] == "running":
        ctx.store.transition_node_status(node["id"], "verifying", note="Entering verification gate")
        node = ctx.store.get_node(node["id"]) or node

    adapter = _adapter_for(project)
    tenv, tenv_mgr = _ensure_target_env(ctx, project, ws_root, adapter)
    if tenv is None:  # Swift and other host-toolchain adapters
        tenv = parse_target_env(project, ws_root)
    test_records = list(ctx.store.list_test_records(node["id"]) or [])
    test_cmd = None
    raw_test_cmd = node.get("test_command")
    if not raw_test_cmd and test_records:
        test_cmd = adapter.test_command(
            project, node, paths=tuple(tr["file_path"] for tr in test_records)
        )
        raw_test_cmd = shlex.join(test_cmd.argv)
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
        trace = check_traceability(reqs, test_sources, extractor=adapter.extract_test_functions)
        if not trace.passed:
            reason = f"Traceability check failed; missing requirement tests: {', '.join(trace.missing)}"
            _fail_node(ctx.store, node["id"], reason)
            return _make_job_result(False, error=reason, missing_requirements=trace.missing)

    # 3. Run test_command inside target env with -W error::DeprecationWarning (TENV-2, CORE-4)
    test_argv, test_env = _command_argv(
        ctx, project, ws_root, adapter, test_cmd if test_cmd is not None else raw_test_cmd
    )
    test_env = {**adapter.env_overrides(project, ws_root), **test_env}
    effective_cmd = shlex.join(test_argv)
    policy = _path_policy(writable=(), readonly=("src/**", "tests/**", ".nai/**"))

    platform = remote_routing.resolved_platform(ctx.store, project, node)
    verify_recipe = adapter.remote_recipe(project, node, "verify", platform=platform)
    verify_params = adapter.remote_params(
        project, node, "verify",
        targets=tuple(_argv_targets(test_argv, ws_root)), platform=platform,
    )
    # Remote routing (Prompt 5): a macos node verifies on a worker.
    remote_verify = _remote_target(ctx, node, "verify", verify_recipe, params=verify_params, ws_root=ws_root)

    def _lint_runner(argv: list[str]) -> tuple[int, str]:
        if remote_verify is None:
            return _run_argv(ctx, argv, phase=PHASE_RUN)
        lint_recipe = adapter.remote_recipe(project, node, "lint", platform=platform)
        lint_params = adapter.remote_params(
            project, node, "lint", targets=tuple(_argv_targets(argv, ws_root)), platform=platform,
        )
        lint_target = _remote_target(ctx, node, "verify", lint_recipe, params=lint_params, ws_root=ws_root)
        if lint_target is None:  # pragma: no cover - routing is stable within a job
            return _run_argv(ctx, argv, phase=PHASE_RUN)
        try:
            return _unpack_cmd_result(
                lint_target.run(argv, timeout=_cmd_timeout(ctx), cancel_event=ctx.cancel_event)
            )
        finally:
            lint_target.close()

    with ctx.ws.job_context(
        _job_context(node["id"], "verify", ctx.job.get("attempt", 1), policy, test_command=effective_cmd)
    ):
        # The adapter regenerates the build files it owns before any command runs.
        _prepared, prepare_error = _prepare_workspace(ctx, project, ws_root, adapter)
        if prepare_error:
            _fail_node(ctx.store, node["id"], f"Workspace preparation failed: {prepare_error}")
            return _make_job_result(False, error=f"Workspace preparation failed: {prepare_error}")
        if remote_verify is not None:
            # A worker run *is* the verification; the local gate cannot match a
            # command it never saw, so the exit code and output decide.
            rc, test_out = _unpack_cmd_result(
                remote_verify.run(test_argv, timeout=_cmd_timeout(ctx), cancel_event=ctx.cancel_event)
            )
            gate_problem = None
        else:
            rc, test_out = _run_argv(ctx, test_argv, phase=PHASE_RUN, env_overrides=test_env)
            gate_problem = ctx.ws.verification_problem() if hasattr(ctx.ws, "verification_problem") else None

        # 4. Lint & type-check gate (TENV-3)
        lint_targets: list[str] = []
        stub_rel = adapter.source_rel(project, node)
        if (ws_root / stub_rel).exists():
            lint_targets.append(stub_rel)
        lint_res = _run_lint_gate(ctx, project, ws_root, node, adapter, tenv, tenv_mgr, lint_targets, _lint_runner)
    if remote_verify is not None:
        remote_verify.close()

    combined_output = test_out
    if lint_res.raw_output:
        combined_output = f"{test_out}\n\n{lint_res.raw_output}".strip()

    # A run that executed no tests is not a pass, whatever the exit code
    # (`swift test` exits 0 when a filter matches nothing).
    test_outcome = adapter.parse_result(test_out, rc, "test")
    no_tests = test_outcome.outcome is Outcome.NO_TESTS

    # TENV-6: staleness tracking for ImportError / AttributeError / DeprecationWarning
    for stale_hit in detect_stale_api(combined_output, tenv):
        ctx.event(
            "build.stale_api",
            f"Stale API signal for package {stale_hit['package']}: {stale_hit['kind']}",
            level="warning",
            data={"stale_api": stale_hit["package"], **stale_hit},
        )

    passed = (rc == 0) and (gate_problem is None) and lint_res.passed and not no_tests
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
        elif no_tests:
            failure_parts.append(f"No tests ran:\n{test_out}")
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
            acc_ok, acc_msg = _run_final_acceptance(ctx, ws_root, tenv, tenv_mgr, adapter)
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
            if hasattr(ctx.ws, "checkpoint_job"):
                try:
                    ctx.ws.checkpoint_job(node["id"], "done", ctx.job.get("attempt", 1))
                except TypeError:
                    ctx.ws.checkpoint_job()
            ctx.event("project.done", f"Project {project['name']} passed final acceptance (BLD-12)")
            return _make_job_result(True, output=test_out, final_acceptance=True)

    ctx.store.transition_node_status(node["id"], "done", note="Verification passed (BLD-6)")
    if hasattr(ctx.ws, "checkpoint_job"):
        try:
            ctx.ws.checkpoint_job(node["id"], "done", ctx.job.get("attempt", 1))
        except TypeError:
            ctx.ws.checkpoint_job()
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
    guard = _guard_manifest(ctx, ws_root)
    if guard is not None:
        return guard
    parent = ctx.store.get_node(node["parent_id"]) if node.get("parent_id") else None
    parent_contract = parent.get("contract") if parent else {}

    adapter = _adapter_for(project)
    tenv = parse_target_env(project, ws_root)
    if adapter.needs_target_env():
        TargetEnvManager(_exec_target(ctx)).ensure_env(ws_root, tenv)
    tools_bundle = build_tools_bundle(ctx.store, project, node, ws_root)

    wiring_rel = adapter.source_rel(project, node)
    policy = _path_policy(writable=(wiring_rel,), readonly=("tests/**", ".nai/**"))

    prompt = get_prompt("integrate")
    children = ctx.store.list_nodes(project["id"], parent_id=parent["id"]) if parent else []
    child_summaries = [
        {"name": c["name"], "kind": c["kind"], "status": c["status"], "stub": adapter.source_rel(project, c)}
        for c in children
        if c["id"] != node["id"]
    ]
    task = (
        f"{prompt.text}\n\n"
        f"{adapter.environment_block(project, ws_root)}\n\n"
        f"{knowledge.block(adapter.id, 'integrate')}\n\n"
        f"Parent node: `{(parent or {}).get('name', 'root')}`\n"
        f"Writable integration glue path: `{wiring_rel}`\n"
        f"Children:\n```json\n{json.dumps(child_summaries, indent=2)}\n```\n"
        f"Scenarios:\n```json\n{json.dumps((parent_contract or {}).get('scenarios') or [], indent=2)}\n```"
    ).replace("\n\n\n", "\n\n")

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
        _record_touched(ctx, ws_root, node["id"], "integrate")
        if (ws_root / wiring_rel).is_file():
            try:
                manifest.record(ws_root, wiring_rel, node_id=node["id"], stage="integrate")
            except (OSError, ValueError):
                pass
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
    adapter = _adapter_for(project)

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
                f"{adapter.environment_block(project, ws_root)}\n\n"
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
    adapter = _adapter_for(project)
    stub_rel = adapter.source_rel(project, node)
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
        {"role": "user", "content": f"{adapter.environment_block(project, ws_root)}\n\n```json\n{json.dumps(payload, indent=2)}\n```"},
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
