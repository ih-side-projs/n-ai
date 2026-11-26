"""Integration self-check for nai.

`nai doctor` verifies the surfaces that unit tests have historically faked:
subsystem imports, CLI registration, planner/context compatibility, handler
coverage, artifact kinds, schema artifacts, package data (including the vendored
C/C++ headers), a real store migration smoke test, the configured database file,
the output root, model-server reachability and the per-language toolchains on
this host (or Docker image) and on named workers.

It never mutates a project.  Advisory checks (``models``,
``language-toolchains``) report ``WARN`` and keep the exit code at 0, so an
offline laptop is still "healthy"; ``--strict`` turns every warning into a
failure for a deployment gate, and ``--offline`` skips the network probe.
"""
from __future__ import annotations

import argparse
import ast
import importlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

MODULES = (
    "nai.cli", "nai.consult", "nai.structured", "nai.store", "nai.sched.runner",
    "nai.spec.plugin", "nai.build.plugin", "nai.tools.registry", "nai.web.app",
    "nai.eval.harness", "nai.service.daemon", "nai.remote.server", "nai.remote.worker",
)
EXPECTED_SUBCOMMANDS = ("run", "mcp", "bench", "service", "doctor", "project", "languages", "where", "relocate",
                        "worker", "remote")
PACKAGE_GLOBS = (
    "prompts/*.txt", "web/templates/*.html", "web/static/*.css", "web/static/*.js",
    "spec/prompts/*.txt", "spec/schemas/*.json", "build/prompts/*.txt", "store/migrations/*.sql",
    "langs/knowledge/*/*.md", "langs/vendor/*.h",
)
REQUIRED_TEMPLATES = (
    "base.html", "projects.html", "project.html", "node.html", "node_edit.html",
    "approvals.html", "escalations.html", "tools.html", "logs.html", "metrics.html",
    "models.html", "artifacts.html", "file_view.html", "auth.html", "service.html",
    "settings.html", "_tree.html", "workers.html",
)

#: Same default the dashboard and the worker CLI use.
DB_ENV = "NAI_DB_PATH"
DEFAULT_DB = ".nai/nai.sqlite3"
OUTPUT_ENV = "NAI_OUTPUT_ROOT"
DOCKER_ENV = "NAI_DOCKER_IMAGE"
MODELS_TIMEOUT = 1.5


@dataclass(frozen=True)
class CheckResult:
    """One check's outcome; ``warning`` means advisory (does not fail the run)."""

    name: str
    ok: bool
    detail: str = ""
    warning: bool = False

    def to_dict(self) -> dict[str, object]:
        """JSON-friendly view used by ``nai doctor --json``."""
        return {"name": self.name, "ok": self.ok, "detail": self.detail, "warning": self.warning}


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
    """Directory of the installed ``nai`` package."""
    module_file = importlib.import_module("nai").__file__
    if not module_file:  # pragma: no cover - a namespace package cannot happen here
        raise RuntimeError("cannot locate the nai package directory")
    return Path(module_file).resolve().parent


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


def _db_path() -> Path:
    """The store path the dashboard, the worker CLI and ``nai run`` share."""
    return Path(os.environ.get(DB_ENV, DEFAULT_DB)).expanduser()


def _writable_anchor(path: Path) -> tuple[Path, bool]:
    """Nearest existing ancestor of *path* and whether it is writable."""
    anchor = path
    while not anchor.exists() and anchor != anchor.parent:
        anchor = anchor.parent
    return anchor, os.access(anchor, os.W_OK)


def check_store_path() -> CheckResult:
    """Check the configured database file (read-only) and its migration level."""
    db = _db_path()
    bundled = len(list((_pkg_root() / "store" / "migrations").glob("*.sql")))
    if not db.exists():
        anchor, writable = _writable_anchor(db.parent)
        if not writable:
            return CheckResult(
                "store-path", False, f"{db} does not exist and {anchor} is not writable"
            )
        return CheckResult(
            "store-path", True, f"{db} (not created yet; {db.parent} will be created)"
        )
    if not db.is_file():
        return CheckResult("store-path", False, f"{db} is not a file")
    try:
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return CheckResult("store-path", False, f"{db}: {type(exc).__name__}: {exc}")
    try:
        applied = conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0]
    except sqlite3.Error as exc:
        return CheckResult(
            "store-path", False, f"{db}: schema_migrations unreadable ({type(exc).__name__}: {exc})"
        )
    finally:
        conn.close()
    if applied < bundled:
        return CheckResult(
            "store-path", False,
            f"{db}: {applied}/{bundled} migrations applied (start the runner once to migrate)",
        )
    return CheckResult("store-path", True, f"{db} ({applied}/{bundled} migrations applied)")


def check_output_root() -> CheckResult:
    """Check the resolved output root: not a dangerous path, and writable."""
    paths_mod = importlib.import_module("nai.paths")
    configured = os.environ.get(OUTPUT_ENV)
    root = (
        Path(configured).expanduser().resolve()
        if configured
        else paths_mod.default_output_root(_db_path())
    )
    reason = paths_mod.dangerous_root_reason(root)
    if reason:
        return CheckResult("output-root", False, f"{root}: {reason}")
    if root.exists() and not root.is_dir():
        return CheckResult("output-root", False, f"{root} is not a directory")
    anchor, writable = _writable_anchor(root)
    if not writable:
        return CheckResult(
            "output-root", False, f"{root} is not writable (nearest existing parent {anchor})"
        )
    state = "writable" if root.exists() else f"will be created under {anchor}"
    return CheckResult("output-root", True, f"{root} ({state})")


_MODEL_PROBE_CACHE: dict[str, tuple[bool, str]] = {}


def _probe_models(base_url: str, api_key: str) -> tuple[bool, str]:
    """``GET <base_url>/models`` with a short timeout; any transport error is 'unreachable'."""
    try:
        import httpx
    except ImportError:  # pragma: no cover - httpx is a hard dependency
        return False, "httpx is not installed"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key and api_key != "none" else {}
    try:
        with httpx.Client(timeout=MODELS_TIMEOUT, headers=headers) as client:
            response = client.get(f"{base_url}/models")
    except Exception as exc:  # noqa: BLE001 - any transport failure means unreachable
        return False, f"unreachable ({type(exc).__name__})"
    if response.status_code >= 500:
        return False, f"HTTP {response.status_code}"
    try:
        data = response.json().get("data")
    except ValueError:
        data = None
    count = len(data) if isinstance(data, list) else 0
    return True, f"HTTP {response.status_code}, {count} model(s)"


def check_models(*, strict: bool = False, offline: bool = False) -> CheckResult:
    """Check the configured model server and the reasoner/coder ids."""
    config_mod = importlib.import_module("nai.config")
    try:
        cfg = config_mod.load_config()
        profile = cfg.get_profile()
    except Exception as exc:  # noqa: BLE001 - a broken config is exactly what doctor reports
        return CheckResult("models", False, f"config: {type(exc).__name__}: {exc}")
    base_url = str(cfg.server.base_url).rstrip("/")
    ids = f"reasoner={profile.reasoner.model_id} coder={profile.coder.model_id}"
    if offline:
        return CheckResult("models", True, f"{base_url} not probed (--offline); {ids}")
    cached = _MODEL_PROBE_CACHE.get(base_url)
    if cached is None:
        cached = _probe_models(base_url, str(cfg.server.api_key))
        _MODEL_PROBE_CACHE[base_url] = cached
    reachable, detail = cached
    if reachable:
        return CheckResult("models", True, f"{base_url}: {detail}; {ids}")
    message = f"{base_url}: {detail} (start llama-swap, set LLAMA_SWAP_URL, or pass --offline)"
    return CheckResult("models", not strict, message, warning=not strict)


def _docker_image_present(image: str) -> bool:
    """``docker image inspect`` through the shared execution seam."""
    if not shutil.which("docker"):
        return False
    exec_mod = importlib.import_module("nai.exec")
    try:
        target = exec_mod.LocalTarget(Path.cwd(), default_timeout=10)
    except Exception:  # noqa: BLE001 - a probe failure just means "not present"
        return False
    try:
        result = target.run(
            ["docker", "image", "inspect", "-f", "{{.Id}}", image],
            cwd=str(Path.cwd()),
            timeout=10,
            phase=exec_mod.PHASE_RUN,
        )
    except Exception:  # noqa: BLE001
        return False
    finally:
        target.close()
    return result.exit_code == 0


def check_language_toolchains(
    *,
    strict: bool = False,
    workers: tuple[tuple[str, str], ...] = (),
) -> CheckResult:
    """Probe every registered language here (or in the Docker image) and on workers.

    Missing required tools are advisory unless ``--strict`` is given: a project
    only needs the language it was created with, so a Go-less host is fine for a
    Python project.
    """
    langs = importlib.import_module("nai.langs")
    registered = langs.registry.supported()
    if not registered:
        return CheckResult("language-toolchains", False, "no adapters registered")
    parts: list[str] = []
    missing: list[str] = []
    image = os.environ.get(DOCKER_ENV)
    if image:
        if not shutil.which("docker"):
            missing.append("docker (not on PATH)")
        elif _docker_image_present(image):
            parts.append(f"docker image {image}: present")
        else:
            missing.append(f"docker image {image} (not present locally)")
    else:
        ready = 0
        for language_id in registered:
            result = langs.probe.probe(language_id)
            if result.ok:
                ready += 1
            else:
                missing.append(f"{language_id} (needs {', '.join(result.missing)})")
        parts.append(f"local {ready}/{len(registered)} ready")
    for name, path in workers:
        bad = [lid for lid in registered if not langs.probe.probe(lid, root=path).ok]
        parts.append(f"{name}: " + ("ready" if not bad else f"missing {', '.join(bad)}"))
        missing.extend(f"{name}/{lid}" for lid in bad)
    detail = "; ".join(parts)
    if missing:
        detail = f"{detail} | missing: {', '.join(missing)}" if detail else ", ".join(missing)
        return CheckResult("language-toolchains", not strict, detail, warning=not strict)
    return CheckResult("language-toolchains", True, detail)


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


def _build_checks(
    *,
    strict: bool,
    offline: bool,
    workers: tuple[tuple[str, str], ...],
) -> tuple[tuple[str, Callable[[], CheckResult]], ...]:
    """The ordered check list, with the advisory checks bound to the run's flags."""
    return (
        ("imports", check_imports),
        ("cli-subcommands", check_subcommands),
        ("job-handlers", check_handlers),
        ("planner-api", check_planner_api),
        ("artifact-kinds", check_artifact_kinds),
        ("schema-artifacts", check_schemas),
        ("package-data", check_package_data),
        ("store-path", check_store_path),
        ("output-root", check_output_root),
        ("models", lambda: check_models(strict=strict, offline=offline)),
        ("language-support", check_language_support),
        (
            "language-toolchains",
            lambda: check_language_toolchains(strict=strict, workers=workers),
        ),
        ("execution-target", check_execution_target),
        ("store-migrations", check_store),
        ("tool-registry", check_tool_registry),
    )


CHECKS: tuple[tuple[str, Callable[[], CheckResult]], ...] = _build_checks(
    strict=False, offline=False, workers=()
)


def run_checks(
    *,
    strict: bool = False,
    offline: bool = False,
    workers: tuple[tuple[str, str], ...] = (),
) -> list[CheckResult]:
    """Run every check; an individual check may fail without aborting the rest."""
    results: list[CheckResult] = []
    for name, fn in _build_checks(strict=strict, offline=offline, workers=workers):
        try:
            results.append(fn())
        except Exception as exc:  # noqa: BLE001
            results.append(CheckResult(name, False, f"check crashed: {type(exc).__name__}: {exc}"))
    return results


def format_report(results: list[CheckResult]) -> str:
    """Render the human-readable report (``WARN`` is advisory)."""
    lines = ["nai doctor", "=" * 10]
    for result in results:
        if result.warning:
            mark = "WARN"
        elif result.ok:
            mark = "OK  "
        else:
            mark = "FAIL"
        lines.append(f"[{mark}] {result.name}: {result.detail}")
    passed = sum(1 for r in results if r.ok)
    warnings = sum(1 for r in results if r.warning)
    suffix = f" ({warnings} warning(s))" if warnings else ""
    lines.append(f"\n{passed}/{len(results)} checks passed{suffix}")
    return "\n".join(lines)


def _parse_workers(values: list[str] | None, env_value: str | None) -> tuple[tuple[str, str], ...]:
    """Parse ``name=path`` worker roots from flags and ``NAI_LANGUAGE_WORKERS``."""
    entries: list[str] = list(values or [])
    if env_value:
        entries.extend(part for part in env_value.split(",") if part.strip())
    workers: list[tuple[str, str]] = []
    for entry in entries:
        name, sep, path = entry.partition("=")
        if not sep or not name.strip() or not path.strip():
            raise ValueError(f"worker must be NAME=PATH, got {entry!r}")
        workers.append((name.strip(), path.strip()))
    return tuple(workers)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(prog="nai doctor", description="Check nai integration surfaces")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    parser.add_argument(
        "--strict", action="store_true",
        help="treat advisory warnings (models, toolchains) as failures",
    )
    parser.add_argument("--offline", action="store_true", help="skip the model-server probe")
    parser.add_argument(
        "--worker", action="append", default=None, metavar="NAME=PATH",
        help="probe a language toolchain on an extra worker root (repeatable)",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    try:
        workers = _parse_workers(args.worker, os.environ.get("NAI_LANGUAGE_WORKERS"))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    results = run_checks(strict=args.strict, offline=args.offline, workers=workers)
    if args.json:
        print(json.dumps([r.to_dict() for r in results], indent=2))
    else:
        print(format_report(results))
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())

