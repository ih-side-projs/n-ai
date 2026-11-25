"""Integration self-check for nai.

`nai doctor` verifies the surfaces that unit tests have historically faked:
subsystem imports, CLI registration, planner/context compatibility, handler
coverage, artifact kinds, schema artifacts, package data and a real store
migration smoke test. It runs offline and needs no model server.
"""
from __future__ import annotations

import argparse
import ast
import importlib
import json
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

MODULES = (
    "nai.cli", "nai.consult", "nai.structured", "nai.store", "nai.sched.runner",
    "nai.spec.plugin", "nai.build.plugin", "nai.tools.registry", "nai.web.app",
    "nai.eval.harness", "nai.service.daemon",
)
EXPECTED_SUBCOMMANDS = ("run", "mcp", "bench", "service", "doctor", "project", "languages", "where", "relocate")
PACKAGE_GLOBS = (
    "prompts/*.txt", "web/templates/*.html", "web/static/*.css", "web/static/*.js",
    "spec/prompts/*.txt", "spec/schemas/*.json", "build/prompts/*.txt", "store/migrations/*.sql",
    "langs/knowledge/*/*.md",
)
REQUIRED_TEMPLATES = (
    "base.html", "projects.html", "project.html", "node.html", "node_edit.html",
    "approvals.html", "escalations.html", "tools.html", "logs.html", "metrics.html",
    "models.html", "artifacts.html", "file_view.html", "auth.html", "service.html",
    "settings.html", "_tree.html",
)


@dataclass(frozen=True)
class CheckResult:
    """Checkresult."""
    name: str
    ok: bool
    detail: str = ""


class _DoctorStore:
    """Minimal Store surface used by planners; no persistence."""

    def list_nodes(self, project_id: str, parent_id: str | None = None) -> list[dict]:
        """List nodes."""
        return []

    def list_jobs(self, project_id: str, limit: int = 100000) -> list[dict]:
        """List jobs."""
        return []

    def list_approvals(self, node_id: str) -> list[dict]:
        """List approvals."""
        return []

    def get_node(self, node_id: str) -> dict | None:
        """Get node."""
        return None

    def create_job(self, project_id, kind, model_role, *, node_id=None, attempt=1, input_ref=None) -> str:
        """Create job."""
        return f"doctor-{kind}"

    def transition_node_status(self, node_id: str, status: str, note: str = "") -> None:
        """Transition node status."""
        return None

    def record_event(self, project_id, event_type, message, **kwargs) -> None:
        """Record event."""
        return None

    def add_artifact(self, project_id, kind, content, *, node_id=None, filename="artifact.txt") -> str:
        """Add artifact."""
        return "doctor-artifact"


def _pkg_root() -> Path:
    return Path(importlib.import_module("nai").__file__).resolve().parent


def check_imports() -> CheckResult:
    """Check imports."""
    failures = []
    for name in MODULES:
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{name}: {type(exc).__name__}: {exc}")
    return CheckResult(
        "imports", not failures,
        "; ".join(failures) if failures else f"{len(MODULES)} modules imported",
    )


def check_subcommands() -> CheckResult:
    """Check subcommands."""
    nai = importlib.import_module("nai")
    cli = importlib.import_module("nai.cli")
    errors = getattr(nai, "_SUBCOMMAND_IMPORT_ERRORS", {})
    missing = [name for name in EXPECTED_SUBCOMMANDS if name not in cli.SUBCOMMANDS]
    detail = []
    if missing:
        detail.append(f"missing: {', '.join(missing)}")
    if errors:
        detail.extend(f"import failed: {k} ({v})" for k, v in errors.items())
    return CheckResult("cli-subcommands", not missing and not errors, "; ".join(detail) or ", ".join(sorted(cli.SUBCOMMANDS)))


def check_handlers() -> CheckResult:
    """Check handlers."""
    registry_mod = importlib.import_module("nai.sched.registry")
    spec_plugin = importlib.import_module("nai.spec.plugin")
    build_plugin = importlib.import_module("nai.build.plugin")
    store_mod = importlib.import_module("nai.store.store")

    class _Runner:
        def __init__(self):
            """Initialize the object."""
            self.registry = registry_mod.HandlerRegistry()
            self.planners: list[Callable] = []

        def register_planner(self, fn: Callable) -> None:
            """Register planner."""
            self.planners.append(fn)

    runner = _Runner()
    spec_plugin.register(runner)
    build_plugin.register(runner)
    registered = set(runner.registry.kinds())
    missing = sorted(set(store_mod.JOB_KINDS) - registered)
    ok = not missing and len(runner.planners) >= 2
    if missing:
        detail = f"unhandled job kinds: {', '.join(missing)}"
    elif len(runner.planners) < 2:
        detail = "expected spec and build planners"
    else:
        detail = f"{len(registered)} job kinds, {len(runner.planners)} planners"
    return CheckResult("job-handlers", ok, detail)


def _planner_probe(name: str, planner: Callable) -> CheckResult:
    pc_mod = importlib.import_module("nai.sched.runner")
    pc = pc_mod.PlanContext(
        store=_DoctorStore(),
        project={"id": "doctor", "brief": "", "settings": {}},
        nodes=[],
        jobs=[],
        runner=SimpleNamespace(node_usage=lambda node: {}),
    )
    required = ("project", "nodes", "jobs", "store", "active_jobs", "nodes_with_status", "idle_ready_nodes", "usage")
    missing = [attr for attr in required if not hasattr(pc, attr)]
    if missing:
        return CheckResult("planner-api", False, f"PlanContext missing {', '.join(missing)}")
    try:
        planner(pc)
    except Exception as exc:  # noqa: BLE001
        return CheckResult("planner-api", False, f"{name} planner: {type(exc).__name__}: {exc}")
    return CheckResult("planner-api", True, f"{name} planner ran against PlanContext")


def check_planner_api() -> CheckResult:
    """Check planner api."""
    spec_plugin = importlib.import_module("nai.spec.plugin")
    build_plugin = importlib.import_module("nai.build.plugin")
    results = [
        _planner_probe("spec", spec_plugin.plan),
        _planner_probe("build", build_plugin.plan),
    ]
    failed = [r.detail for r in results if not r.ok]
    return CheckResult("planner-api", not failed, "; ".join(failed) if failed else "spec and build planners ran")



def check_artifact_kinds() -> CheckResult:
    """Check artifact kinds."""
    store_mod = importlib.import_module("nai.store.store")
    root = _pkg_root()
    invalid: list[str] = []
    seen = 0
    for rel in ("spec", "build"):
        for path in sorted((root / rel).rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                fname = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if fname not in {"artifact", "add_artifact"}:
                    continue
                kind_node = node.args[0] if node.args else next(
                    (kw.value for kw in node.keywords if kw.arg == "kind"), None
                )
                if not isinstance(kind_node, ast.Constant) or not isinstance(kind_node.value, str):
                    continue
                seen += 1
                if kind_node.value not in store_mod.ARTIFACT_KINDS:
                    invalid.append(f"{path.relative_to(root)}:{node.lineno} {kind_node.value}")
    return CheckResult(
        "artifact-kinds", not invalid,
        "; ".join(invalid) if invalid else f"{seen} literal artifact kinds valid",
    )


def check_schemas() -> CheckResult:
    """Check schemas."""
    schemas_mod = importlib.import_module("nai.spec.schemas")
    expected = dict(schemas_mod.SCHEMAS)
    expected["contract"] = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "Contract",
        **schemas_mod.INTERFACES_SCHEMA["properties"]["contract"],
    }
    failures = []
    for name, schema in expected.items():
        path = schemas_mod._SCHEMAS_DIR / f"{name}.json"
        want = json.dumps(schema, indent=2, sort_keys=True)
        if not path.is_file():
            failures.append(f"missing {path.name}")
        elif path.read_text(encoding="utf-8") != want:
            failures.append(f"out of date {path.name}")
    return CheckResult("schema-artifacts", not failures, "; ".join(failures) or f"{len(expected)} schemas in sync")


def check_package_data() -> CheckResult:
    """Check package data."""
    root = _pkg_root()
    missing = [pattern for pattern in PACKAGE_GLOBS if not any(root.glob(pattern))]
    tmpl = root / "web" / "templates"
    missing_templates = [name for name in REQUIRED_TEMPLATES if not (tmpl / name).is_file()]
    if missing_templates:
        missing.append(f"templates: {', '.join(missing_templates)}")
    return CheckResult(
        "package-data", not missing,
        "; ".join(missing) or "migrations, schemas, prompts, templates and static assets present",
    )



def check_language_support() -> CheckResult:
    """Check that every registered language meets the runtime support criteria."""
    langs = importlib.import_module("nai.langs")
    problems: list[str] = []
    for language_id in langs.registry.supported():
        status = langs.support.support_status(language_id)
        if not status.supported:
            missing = ", ".join(status.missing_knowledge) or "toolchain probe"
            problems.append(f"{language_id} ({missing})")
    count = len(langs.registry.supported())
    if not count:
        problems.append("no adapters registered")
    return CheckResult(
        "language-support", not problems,
        "; ".join(problems) or f"{count} language(s) supported: {', '.join(langs.support.supported_languages())}",
    )


def check_execution_target() -> CheckResult:
    """Check the single command-execution seam and its hardening probes."""
    mod = importlib.import_module("nai.exec")
    try:
        with tempfile.TemporaryDirectory(prefix="nai-doctor-exec-") as root:
            target = mod.make_target(root)
            try:
                return CheckResult("execution-target", True, target.describe())
            finally:
                target.close()
    except Exception as exc:  # noqa: BLE001 - a probe failure is a doctor result
        return CheckResult("execution-target", False, f"{type(exc).__name__}: {exc}")


def check_store() -> CheckResult:
    """Check store."""
    store_mod = importlib.import_module("nai.store.store")
    try:
        with tempfile.TemporaryDirectory(prefix="nai-doctor-") as tmp:
            store = store_mod.Store(Path(tmp) / "doctor.sqlite3")
            try:
                pid = store.create_project("doctor", "self check")
                store.create_node(pid, "root", "system", objective="self check", spec_text="# root")
            finally:
                store.close()
    except Exception as exc:  # noqa: BLE001
        return CheckResult("store-migrations", False, f"{type(exc).__name__}: {exc}")
    return CheckResult("store-migrations", True, "temp SQLite store created and migrated")


def check_tool_registry() -> CheckResult:
    """Check tool registry."""
    tools = importlib.import_module("nai.tools")
    registry = tools.get_registry()
    names = set(registry.get_all())
    expected = {"list_tools", "describe_tool", "read_csv", "read_json", "write_json", "diff_text"}
    missing = sorted(expected - names)
    return CheckResult(
        "tool-registry", not missing,
        f"missing: {', '.join(missing)}" if missing else f"{len(names)} tools registered",
    )


CHECKS: tuple[tuple[str, Callable[[], CheckResult]], ...] = (
    ("imports", check_imports),
    ("cli-subcommands", check_subcommands),
    ("job-handlers", check_handlers),
    ("planner-api", check_planner_api),
    ("artifact-kinds", check_artifact_kinds),
    ("schema-artifacts", check_schemas),
    ("package-data", check_package_data),
    ("language-support", check_language_support),
    ("execution-target", check_execution_target),
    ("store-migrations", check_store),
    ("tool-registry", check_tool_registry),
)


def run_checks() -> list[CheckResult]:
    """Run all checks; an individual check may fail without aborting the rest."""
    results: list[CheckResult] = []
    for name, fn in CHECKS:
        try:
            results.append(fn())
        except Exception as exc:  # noqa: BLE001
            results.append(CheckResult(name, False, f"check crashed: {type(exc).__name__}: {exc}"))
    return results


def format_report(results: list[CheckResult]) -> str:
    """Format report."""
    lines = ["nai doctor", "=" * 10]
    for result in results:
        mark = "OK  " if result.ok else "FAIL"
        lines.append(f"[{mark}] {result.name}: {result.detail}")
    passed = sum(1 for r in results if r.ok)
    lines.append(f"\n{passed}/{len(results)} checks passed")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(prog="nai doctor", description="Check nai integration surfaces")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    results = run_checks()
    if args.json:
        print(json.dumps([r.__dict__ for r in results], indent=2))
    else:
        print(format_report(results))
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())

