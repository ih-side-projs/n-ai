"""Swift (SwiftPM) language adapter.

The adapter owns ``Package.swift``: it regenerates the manifest deterministically
from the frozen contract and the targets that exist on disk, so the model never
edits build configuration.  Layout::

    Package.swift                       generated, never hand-edited
    Sources/<Target>/<Target>.swift     contract stub / implementation
    Sources/<Package>Core/Entities.swift shared contract entities
    Tests/<Target>Tests/<Target>Tests.swift  XCTest
    external/                           owner-supplied Xcode project (never written)

Stubs trap with ``fatalError("not implemented")``: the build succeeds and the
test run aborts with ``Fatal error: not implemented``, which is exactly the
pre-implementation failure the pipeline proves before implementation.

SwiftPM only.  An Xcode project is never generated: when the owner supplies one
under ``external/`` and references it in ``project.settings.swift.xcode_project``
it is included in snapshots and the macOS verification uses ``xcodebuild-test``.
"""

from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from .base import Command, Outcome, ParseResult, StubFile, StubResult, TestCase, ToolCheck

STUB_MARKER = 'fatalError("not implemented")'
CRASH_MARKER = "Fatal error: not implemented"

_ERROR_LINE = re.compile(r"^(?P<path>.+?):(?P<line>\d+):(?:\d+:)?\s*error:\s*(?P<msg>.+)$", re.M)
_ERROR_BARE = re.compile(r"^(?:<unknown>:0:\s*)?error:\s*(?P<msg>.+)$", re.M)
_TEST_CASE = re.compile(
    r"Test Case '(?P<name>[^']+)' (?P<result>passed|failed) \((?P<seconds>[\d.]+) seconds\)"
)
_ZERO_TESTS = re.compile(r"Executed 0 tests", re.I)
_IMPORT = re.compile(r"^(?:@testable\s+)?import\s+([A-Za-z_][A-Za-z0-9_]*)")
_UI_MODULES = ("SwiftUI", "UIKit", "AppKit")

#: Swift's view of the contract's Python-ish type names.
_SWIFT_TYPES = {
    "str": "String", "string": "String", "text": "String",
    "int": "Int", "integer": "Int", "number": "Double",
    "float": "Double", "double": "Double",
    "bool": "Bool", "boolean": "Bool",
    "date": "Date", "uuid": "UUID",
    "any": "String", "object": "[String: String]", "dict": "[String: String]",
    "map": "[String: String]", "list": "[String]",
}


def swift_identifier(value: str) -> str:
    """PascalCase a name into a valid Swift type/target identifier."""
    words = re.findall(r"[A-Za-z0-9]+", str(value or ""))
    ident = "".join(word[:1].upper() + word[1:] for word in words) or "Component"
    if ident[0].isdigit():
        ident = f"T{ident}"
    return ident


def swift_member(value: str) -> str:
    """camelCase a name into a valid Swift function/parameter identifier."""
    ident = swift_identifier(value)
    return ident[:1].lower() + ident[1:]


def swift_package_name(project: dict[str, Any]) -> str:
    settings = (project or {}).get("settings") or {}
    raw = settings.get("package_name") or (project or {}).get("name") or "App"
    return swift_identifier(str(raw))


def swift_core_target(project: dict[str, Any]) -> str:
    return f"{swift_package_name(project)}Core"


def swift_type(raw: Any, entities: tuple[str, ...] = ()) -> str:
    """Map a contract type to Swift (entities keep their declared name)."""
    text = str(raw or "").strip()
    lower = text.lower()
    if not text or lower in {"none", "null", "void", "unit"}:
        return "Void"
    if lower in _SWIFT_TYPES:
        return _SWIFT_TYPES[lower]
    if text.endswith("?"):
        return _as_optional(swift_type(text.rstrip("?"), entities))
    if lower.startswith("optional[") and text.endswith("]"):
        return _as_optional(swift_type(text[len("optional["):-1], entities))
    bracket = re.fullmatch(r"(?:list|array|set)\s*[\[<]\s*(.+?)\s*[\]]", text, re.I)
    if bracket:
        inner = swift_type(bracket.group(1), entities)
        return f"[{inner}]"
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if ":" in inner:
            key, _, value = inner.partition(":")
            return f"[{swift_type(key, entities)}: {swift_type(value, entities)}]"
        return f"[{swift_type(inner, entities)}]"
    if text in entities or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", text):
        return text
    return "String"


def _as_optional(swift: str) -> str:
    return swift if swift.endswith("?") else f"{swift}?"


def _parse_provide(provide: dict[str, Any], entities: tuple[str, ...]) -> tuple[str, list[tuple[str, str]], str]:
    """Return ``(swift_name, [(param, type)], return_type)`` for one ``provides`` entry."""
    signature = str(provide.get("signature") or "").strip()
    name = str(provide.get("name") or "").strip()
    params: list[Any] = list(provide.get("params") or [])
    returns = str(provide.get("returns") or "").strip()
    if signature:
        match = re.match(
            r"^(?:func\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\((.*?)\)\s*(?:->\s*(.+?))?$",
            signature,
        )
        if match:
            name = name or match.group(1)
            raw_params = match.group(2).strip()
            returns = returns or (match.group(3) or "").strip()
            params = [part.strip() for part in raw_params.split(",") if part.strip()] if raw_params else []
    swift_name = swift_member(name or "operation")
    rendered: list[tuple[str, str]] = []
    for index, param in enumerate(params):
        if isinstance(param, dict):
            param_name = str(param.get("name") or f"arg{index}")
            param_type = param.get("type") or "String"
        else:
            text = str(param)
            if ":" in text:
                param_name, _, param_type = text.partition(":")
            else:
                param_name, param_type = text, "String"
        rendered.append((swift_member(param_name), swift_type(param_type, entities)))
    return swift_name, rendered, swift_type(returns, entities)


def render_entities(entities: list[dict[str, Any]]) -> str:
    """Render the shared contract entities as one Swift file."""
    lines = [
        "// Generated by nai from the frozen contract. Do not edit: the adapter regenerates this file.",
        "",
        "import Foundation",
        "",
    ]
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        name = swift_identifier(str(entity.get("name") or "Entity"))
        fields = [f for f in (entity.get("fields") or []) if isinstance(f, dict)]
        description = str(entity.get("description") or "").strip()
        if description:
            lines.append(f"/// {description}")
        lines.append(f"public struct {name}: Equatable {{")
        if not fields:
            lines.append("    public init() {}")
            lines.append("}")
            lines.append("")
            continue
        for field in fields:
            field_name = swift_member(str(field.get("name") or "value"))
            field_type = swift_type(field.get("type"), ())
            if not field.get("required", True):
                field_type = field_type if field_type.endswith("?") else f"{field_type}?"
            lines.append(f"    public let {field_name}: {field_type}")
        lines.append("")
        params = []
        for field in fields:
            field_name = swift_member(str(field.get("name") or "value"))
            field_type = swift_type(field.get("type"), ())
            if field.get("required", True):
                params.append(f"{field_name}: {field_type}")
            else:
                field_type = field_type if field_type.endswith("?") else f"{field_type}?"
                params.append(f"{field_name}: {field_type} = nil")
        lines.append(f"    public init({', '.join(params)}) {{")
        for field in fields:
            field_name = swift_member(str(field.get("name") or "value"))
            lines.append(f"        self.{field_name} = {field_name}")
        lines.append("    }")
        lines.append("}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _component_entry(contract: dict[str, Any] | None, node: dict[str, Any]) -> dict[str, Any]:
    name = str(node.get("name") or "").strip()
    for component in (contract or {}).get("components") or []:
        if isinstance(component, dict) and str(component.get("name") or "").strip() == name:
            return component
    return {}


def _components(project: dict[str, Any], contract: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Components from the project's frozen map, else from the passed contract."""
    settings = (project or {}).get("settings") or {}
    block = settings.get("component_map") if isinstance(settings, dict) else None
    raw = (block or {}).get("components") if isinstance(block, dict) else None
    if not raw:
        raw = (contract or {}).get("components")
    return [c for c in (raw or []) if isinstance(c, dict) and str(c.get("name") or "").strip()]



def _source_targets(workspace_root: Path) -> list[str]:
    base = Path(workspace_root) / "Sources"
    if not base.is_dir():
        return []
    return sorted(p.name for p in base.iterdir() if p.is_dir() and not p.name.startswith("."))


def _test_targets(workspace_root: Path) -> list[str]:
    base = Path(workspace_root) / "Tests"
    if not base.is_dir():
        return []
    return sorted(
        p.name for p in base.iterdir()
        if p.is_dir() and p.name.endswith("Tests") and not p.name.startswith(".")
    )


def _target_line(kind: str, name: str, deps: list[str]) -> str:
    unique = sorted(dict.fromkeys(deps))
    if not unique:
        return f'{kind}(name: "{name}")'
    joined = ", ".join(f'"{dep}"' for dep in unique)
    return f'{kind}(name: "{name}", dependencies: [{joined}])'


def render_package_swift(project: dict[str, Any], workspace_root: Path) -> str:
    """Render a deterministic ``Package.swift`` for the targets that exist.

    Declaring a target whose ``Sources/<Target>`` directory does not exist yet
    would make SwiftPM fail, so the manifest follows the tree: components come
    from the frozen map (with their ``depends_on`` edges), integration targets
    depend on the portable components, and test targets depend on their target.
    """
    package = swift_package_name(project)
    core = swift_core_target(project)
    libs = _source_targets(workspace_root)
    tests = [t for t in _test_targets(workspace_root) if t[: -len("Tests")] in libs]
    by_target = {swift_identifier(str(c["name"])): c for c in _components(project)}

    target_lines: list[str] = []
    for name in libs:
        deps: list[str] = []
        if name != core and core in libs:
            deps.append(core)
        component = by_target.get(name)
        if component:
            for dep in component.get("depends_on") or []:
                dep_target = swift_identifier(str(dep))
                if dep_target in libs and dep_target != name:
                    deps.append(dep_target)
        elif name.endswith("Integration"):
            deps.extend(
                other for other in libs
                if other not in (name, core) and not other.endswith("Integration")
            )
        target_lines.append(_target_line(".target", name, deps))

    test_lines: list[str] = []
    for test_name in tests:
        base = test_name[: -len("Tests")]
        deps = [base]
        if base.endswith("Integration"):
            deps.extend(other for other in libs if not other.endswith("Integration"))
        test_lines.append(_target_line(".testTarget", test_name, deps))

    entries = target_lines + test_lines
    header = (
        "// swift-tools-version: 5.9\n"
        f"// Generated by nai for {project.get('name') or package}. The adapter owns this file; do not edit.\n"
        "import PackageDescription\n\n"
    )
    if not entries:
        return header + f'let package = Package(\n    name: "{package}",\n    targets: []\n)\n'
    body = "\n".join(f"        {line}," for line in entries)
    return header + (
        "let package = Package(\n"
        f'    name: "{package}",\n'
        "    targets: [\n"
        f"{body}\n"
        "    ]\n"
        ")\n"
    )



class SwiftAdapter:
    """Adapter for Swift packages built with SwiftPM and tested with XCTest."""

    id = "swift"
    display_name = "Swift"
    test_framework = "XCTest"
    extensions = (".swift",)
    stub_marker = STUB_MARKER
    stack_hint = "Swift 6 / SwiftPM"
    style_guide = (
        "- Keep logic in a portable Swift package: no UI imports in view models or library code.\n"
        "- Put SwiftUI views in a thin layer; a `platform = \"any\"` or `linux` component may import only\n"
        "  Foundation and the standard library (never SwiftUI, UIKit or AppKit).\n"
        "- Prefer value types (`struct`, `enum`) and pure functions; make `async` code cancellation-aware.\n"
        "- Name XCTest methods `func test_<REQ-ID>_<behaviour>()` so traceability can find them.\n"
        "- Keep `Package.swift` untouched: the adapter owns it and regenerates it from the contract."
    )
    toolchain = (
        ToolCheck("swift", ("swift", "--version")),
        ToolCheck("swiftc", ("swiftc", "--version"), required=False),
        ToolCheck("xcodebuild", ("xcodebuild", "-version"), required=False),
    )

    # -- layout ------------------------------------------------------------
    def package_name(self, project: dict[str, Any]) -> str:
        return swift_package_name(project)

    def module_import(self, project: dict[str, Any], node: dict[str, Any]) -> str:
        return swift_identifier(str(node.get("name") or "Component"))

    def source_rel(self, project: dict[str, Any], node: dict[str, Any]) -> str:
        target = self.module_import(project, node)
        return f"Sources/{target}/{target}.swift"

    def test_rel(self, project: dict[str, Any], node: dict[str, Any]) -> str:
        target = self.module_import(project, node)
        return f"Tests/{target}Tests/{target}Tests.swift"

    def entities_rel(self, project: dict[str, Any]) -> str:
        core = swift_core_target(project)
        return f"Sources/{core}/Entities.swift"

    def normalize_test_path(self, raw_path: str, node: dict[str, Any]) -> str:
        """Force a model-supplied test path into the node's XCTest target."""
        target = swift_identifier(str(node.get("name") or "Component"))
        name = PurePosixPath(str(raw_path or "").replace("\\", "/")).name or f"{target}Tests.swift"
        name = re.sub(r"[^A-Za-z0-9_.-]", "", name).lstrip(".")
        if name.endswith(".swift"):
            stem = name[: -len(".swift")]
        elif "." in name:
            stem = name.rsplit(".", 1)[0]
        else:
            stem = name
        stem = re.sub(r"[^A-Za-z0-9_-]", "", stem) or f"{target}Tests"
        return f"Tests/{target}Tests/{stem}.swift"

    def protected_globs(self, project: dict[str, Any]) -> tuple[str, ...]:
        return (
            "Tests/**",
            ".nai/**",
            "Package.swift",
            "Package.resolved",
            "external/**",
            self.entities_rel(project),
        )

    def writable_globs(self, project: dict[str, Any], node: dict[str, Any]) -> tuple[str, ...]:
        target = self.module_import(project, node)
        return (self.source_rel(project, node), f"Sources/{target}/**")

    def snapshot_ignore(self) -> tuple[str, ...]:
        return (
            ".nai",
            ".git",
            ".build",
            ".swiftpm",
            ".index-build",
            "DerivedData",
            "*.xcuserstate",
            "xcuserdata",
        )

    def cache_dirs(self) -> tuple[str, ...]:
        return (".build", ".swiftpm", "DerivedData")

    # -- environment and workspace -----------------------------------------
    def needs_target_env(self) -> bool:
        """Swift uses the host toolchain; there is no pinned virtualenv."""
        return False

    def prepare_workspace(self, project: dict[str, Any], workspace_root: Path) -> tuple[str, ...]:
        """Regenerate ``Package.swift`` when the tree changed."""
        root = Path(workspace_root)
        manifest = render_package_swift(project, root)
        path = root / "Package.swift"
        if path.is_file() and path.read_text(encoding="utf-8") == manifest:
            return ()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(manifest, encoding="utf-8")
        return ("Package.swift",)

    def environment_block(self, project: dict[str, Any], workspace_root: Path) -> str:
        xcode = _xcode_project(project)
        deps = (
            "none beyond Foundation and the standard library"
            if not xcode
            else f"the owner-supplied Xcode project at `{xcode}` (never generated or edited)"
        )
        return (
            "## Target Environment (TENV-4)\n"
            f"- Language: {self.stack_hint} (`swift-tools-version: 5.9` manifest)\n"
            "- Tests: XCTest via `swift test`; layout `Sources/<Target>/`, `Tests/<Target>Tests/`\n"
            f"- Dependencies: {deps}\n"
            "- Rule: do not add package dependencies or edit `Package.swift`; the adapter regenerates it "
            "from the frozen contract. Raise a contract change request if another dependency is required."
        )


    # -- stubs -------------------------------------------------------------
    def render_stub(
        self,
        project: dict[str, Any],
        node: dict[str, Any],
        contract: dict[str, Any] | None,
    ) -> StubResult:
        """Render the node's stub plus the shared entities file."""
        entities = [e for e in (contract or {}).get("entities") or [] if isinstance(e, dict)]
        files: list[StubFile] = []
        if entities:
            files.append(StubFile(self.entities_rel(project), render_entities(entities)))
        files.append(StubFile(self.source_rel(project, node), self._render_source(project, node, contract, entities)))
        return StubResult(
            files=tuple(files),
            module_import=self.module_import(project, node),
            package_name=self.package_name(project),
        )

    def _render_source(
        self,
        project: dict[str, Any],
        node: dict[str, Any],
        contract: dict[str, Any] | None,
        entities: list[dict[str, Any]],
    ) -> str:
        target = self.module_import(project, node)
        core = swift_core_target(project)
        lines = [
            f"// Generated by nai contract stubs for {node.get('name')}. Keep the signatures; implement the bodies.",
            "",
        ]
        if entities:
            lines.append(f"import {core}")
            lines.append("")
        if str(node.get("kind") or "") == "integration":
            lines.extend([
                f"/// Integration wiring for `{node.get('name')}`. Run the parent contract's scenarios",
                "/// and return the observable result (0 when the scenario has no numeric result).",
                "public func integrate() -> Int {",
                f"    {STUB_MARKER}",
                "}",
                "",
            ])
            return "\n".join(lines)
        entity_names = tuple(swift_identifier(str(e.get("name") or "")) for e in entities)
        component = _component_entry(contract, node)
        provides = [p for p in (component.get("provides") or []) if isinstance(p, dict)]
        if not provides:
            lines.extend([
                f"/// `{target}` has no contract operations yet.",
                f"public enum {target} {{}}",
                "",
            ])
            return "\n".join(lines)
        for provide in provides:
            name, params, returns = _parse_provide(provide, entity_names)
            description = str(provide.get("description") or "").strip() or f"Contract operation {name}."
            lines.append(f"/// {description}")
            signature = ", ".join(f"_ {param}: {kind}" for param, kind in params)
            suffix = "" if returns == "Void" else f" -> {returns}"
            lines.append(f"public func {name}({signature}){suffix} {{")
            lines.append(f"    {STUB_MARKER}")
            lines.append("}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def validate_stub(
        self,
        workspace_root: Path,
        module_import: str,
        run_cmd: Any,
        *,
        executable: str | None = None,
        run_setup_cmd: Any | None = None,
    ) -> tuple[bool, str]:
        """A stub is valid when the package still builds."""
        rc, out = run_cmd("swift build")
        return rc == 0, out

    def extract_signatures(self, source: str) -> str:
        """Public declarations only, never implementations (BLD-1)."""
        found: list[str] = []
        for line in (source or "").splitlines():
            stripped = line.strip()
            if stripped.startswith("//") or not stripped:
                continue
            if re.match(r"^(?:public\s+)?(?:final\s+)?(?:func|struct|enum|protocol|class|typealias|let|var)\s", stripped):
                found.append(stripped.rstrip("{").rstrip())
        return "\n".join(found)

    # -- commands ----------------------------------------------------------
    def setup_commands(self, project: dict[str, Any], workspace_root: Path) -> tuple[Command, ...]:
        """SwiftPM has no dependency setup for the packages N-AI generates."""
        return ()

    def build_commands(self, project: dict[str, Any], workspace_root: Path) -> tuple[Command, ...]:
        return (Command(argv=("swift", "build"), network=False),)

    def test_command(
        self,
        project: dict[str, Any],
        node: dict[str, Any],
        *,
        scope: str | None = None,
        paths: tuple[str, ...] = (),
    ) -> Command:
        target = self.module_import(project, node)
        return Command(argv=("swift", "test", "--filter", f"{target}Tests"), network=False)

    def lint_command(
        self,
        project: dict[str, Any],
        workspace_root: Path,
        targets: tuple[str, ...],
    ) -> Command | None:
        """No external linter; platform import rules are checked statically."""
        return None

    def env_overrides(self, project: dict[str, Any], workspace_root: Path) -> dict[str, str]:
        return {}


    # -- results -----------------------------------------------------------
    def parse_result(
        self,
        raw_output: str,
        exit_code: int,
        kind: str,
        *,
        timed_out: bool = False,
    ) -> ParseResult:
        """Classify SwiftPM/XCTest output.

        ``swift build`` prints ``error:`` lines; a stub crash aborts the run with
        ``Fatal error: not implemented``; ``Executed 0 tests`` means no test ran.
        """
        text = raw_output or ""
        if timed_out:
            return ParseResult(Outcome.TIMEOUT, exit_code=exit_code, raw_output=text, timed_out=True)
        cases = tuple(
            TestCase(
                name=match.group("name"),
                outcome="passed" if match.group("result") == "passed" else "failed",
            )
            for match in _TEST_CASE.finditer(text)
        )
        located = tuple(match.group(0).strip() for match in _ERROR_LINE.finditer(text))
        bare = tuple(match.group(0).strip() for match in _ERROR_BARE.finditer(text))
        errors = tuple(dict.fromkeys((*located, *bare)))
        marker = CRASH_MARKER in text
        zero_tests = bool(_ZERO_TESTS.search(text))
        if exit_code == 0:
            outcome = Outcome.NO_TESTS if zero_tests else Outcome.PASS
        elif kind == "build":
            outcome = Outcome.BUILD_ERROR
        elif errors and not cases:
            outcome = Outcome.BUILD_ERROR
        else:
            outcome = Outcome.TEST_FAIL
        return ParseResult(
            outcome=outcome,
            exit_code=exit_code,
            tests=cases,
            failure_messages=errors,
            raw_output=text,
            marker_present=marker,
        )

    def stub_failure_is_valid(
        self,
        result: ParseResult,
        *,
        integration: bool = False,
        expected_module: str | None = None,
    ) -> bool:
        """Valid only when the build succeeded and the run hit the stub trap."""
        if result.timed_out:
            return False
        if result.outcome in (Outcome.BUILD_ERROR, Outcome.PASS, Outcome.NO_TESTS):
            return False
        return result.marker_present

    def extract_test_functions(self, test_sources: dict[str, str]) -> tuple[list[str], list[str]]:
        """XCTest methods: ``func test_<ID>_<name>()``."""
        functions: list[str] = []
        errors: list[str] = []
        for path, source in sorted(test_sources.items()):
            if not str(source or "").strip():
                errors.append(f"{path}: empty test file")
                continue
            for match in re.finditer(r"\bfunc\s+(test[A-Za-z0-9_]*)\s*\(", source or ""):
                functions.append(match.group(1))
        return functions, errors

    def check_syntax(self, code: str, filename: str) -> str | None:
        """``swiftc -parse`` when a Swift toolchain is installed on the orchestrator."""
        swiftc = shutil.which("swiftc")
        if not swiftc:
            return None
        from ..exec import PHASE_RUN, LocalTarget

        with tempfile.TemporaryDirectory(prefix="nai-swift-parse-") as tmp:
            path = Path(tmp) / "Check.swift"
            path.write_text(code, encoding="utf-8")
            target = LocalTarget(Path(tmp), default_timeout=60)
            try:
                result = target.run([swiftc, "-parse", str(path)], cwd=tmp, timeout=60, phase=PHASE_RUN)
            finally:
                target.close()
        if result.exit_code == 0:
            return None
        errors = [line.strip() for line in result.output.splitlines() if "error:" in line]
        return "\n".join(errors[:5]) or f"{filename}: swiftc -parse exited {result.exit_code}"

    # -- platform lint ------------------------------------------------------
    def platform_lint(
        self,
        project: dict[str, Any],
        node: dict[str, Any],
        workspace_root: Path,
        *,
        platform: str = "any",
    ) -> tuple[str, ...]:
        """A portable (``any``/``linux``) component may not import UI frameworks."""
        if str(platform).lower() not in ("any", "linux"):
            return ()
        root = Path(workspace_root)
        base = root / "Sources" / self.module_import(project, node)
        if not base.is_dir():
            return ()
        findings: list[str] = []
        for path in sorted(base.rglob("*.swift")):
            rel = path.relative_to(root).as_posix()
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:  # pragma: no cover - unreadable file is reported by verify
                continue
            for number, line in enumerate(lines, 1):
                match = _IMPORT.match(line.strip())
                if match and match.group(1) in _UI_MODULES:
                    findings.append(
                        f"{rel}:{number}: imports {match.group(1)}; a platform "
                        f"{platform} component must not import SwiftUI, UIKit or AppKit"
                    )
        return tuple(findings)


    # -- remote execution ---------------------------------------------------
    def remote_recipe(
        self, project: dict[str, Any], node: dict[str, Any], job_kind: str, *, platform: str = "any"
    ) -> str:
        """Pick the recipe a worker runs for this step."""
        if job_kind in ("verify", "author_tests", "integrate"):
            if str(platform).lower() == "macos" and _xcode_project(project):
                return "xcodebuild-test"
            return "swift-test"
        return "swift-build"

    def remote_params(
        self,
        project: dict[str, Any],
        node: dict[str, Any],
        job_kind: str,
        *,
        targets: tuple[str, ...] = (),
        platform: str = "any",
    ) -> dict[str, Any]:
        if job_kind in ("verify", "author_tests", "integrate"):
            xcode = _xcode_project(project)
            if str(platform).lower() == "macos" and xcode:
                return {
                    "xcode_project": xcode,
                    "scheme": _xcode_scheme(project),
                    "destination": _xcode_destination(project),
                }
            return {"filter": f"{self.module_import(project, node)}Tests"}
        return {}


def swift_settings(project: dict[str, Any]) -> dict[str, Any]:
    """The ``settings.swift`` table (owner-controlled)."""
    settings = (project or {}).get("settings") or {}
    block = settings.get("swift") if isinstance(settings, dict) else None
    return block if isinstance(block, dict) else {}


def _xcode_project(project: dict[str, Any]) -> str:
    """Normalise ``settings.swift.xcode_project`` to a safe path under ``external/``."""
    raw = str(swift_settings(project).get("xcode_project") or "").strip().replace("\\", "/")
    if not raw:
        return ""
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        return ""
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        return ""
    if parts[0] != "external":
        parts = ["external", *parts]
    return "/".join(parts)


def _xcode_scheme(project: dict[str, Any]) -> str:
    raw = str(swift_settings(project).get("xcode_scheme") or "").strip()
    cleaned = re.sub(r"[^A-Za-z0-9._+-]", "", raw)
    return cleaned or swift_package_name(project)


def _xcode_destination(project: dict[str, Any]) -> str:
    """Destination from the recipe's allowlist (default: this Mac)."""
    from ..remote.recipes import XCODE_DESTINATIONS

    raw = str(swift_settings(project).get("xcode_destination") or "").strip()
    return raw if raw in XCODE_DESTINATIONS else XCODE_DESTINATIONS[0]

