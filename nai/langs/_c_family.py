"""Shared implementation for the C and C++ adapters.

Both adapters use CMake (>= 3.21) + CTest, generate ``CMakeLists.txt``
deterministically from the frozen contract and the files on disk, and keep the
public interface in ``include/<component>/`` headers.  The only differences are
the language standard, the source extension, the stub syntax and the test
framework, so everything else lives here.

Layout::

    CMakeLists.txt                      generated
    include/<component>/<component>.h   public header (interface)
    include/entities/entities.h         shared contract entities
    src/<component>/<component>.c|cpp   implementation (stub first)
    tests/<component>/<component>_test.c|cpp  frozen tests
    vendor/                             vendored single-header test frameworks
    build/                              out-of-tree build cache (never committed)

One language per project: a mixed C/C++ workspace is rejected with a clear
message before anything is generated.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

from . import _common
from .base import Command, Outcome, ParseResult, StubFile, StubResult, TestCase, ToolCheck

#: Vendored framework headers shipped in the nai repo and copied into the workspace.
VENDOR_DIR = Path(__file__).resolve().parent / "vendor"
VENDOR_HEADERS = ("nai_assert.h", "nai_runtime.h", "doctest.h")

_TESTCASE = re.compile(
    r"<testcase\s+(?P<attrs>[^>]*?)(?:/>|>(?P<body>.*?)</testcase>)", re.S
)
_ATTR = re.compile(r"(\w+)\s*=\s*\"([^\"]*)\"")
_FAILURE = re.compile(r"<failure[^>]*?message=\"([^\"]*)\"", re.S)
_ERROR_MSG = re.compile(r"<error[^>]*?message=\"([^\"]*)\"", re.S)
_IMPORT_APPLE = re.compile(r"^\s*#\s*(?:include|import)\s*[<\"]([^>\"]+)[>\"]", re.M)
_APPLE_FRAMEWORKS = (
    "AppKit/AppKit.h", "Cocoa/Cocoa.h", "SwiftUI/SwiftUI.h",
    "CoreFoundation/CoreFoundation.h", "Foundation/Foundation.h",
    "IOKit/IOKitLib.h", "Security/Security.h",
)
_EMPTY_TEST = re.compile(
    r"(No tests were found|Total Tests:\s*0|tests?=\"0\"|0 test cases|0 tests? registered)",
    re.I,
)
#: Test case names in either framework: doctest's ``TEST_CASE("name")`` and the
#: C harness's ``nai_case("name")``.
_CASE_NAME = re.compile(r'(?:TEST_CASE|nai_case)\s*\(\s*"([^"]+)"')
#: CTest's stable ``Test #N: name ... Passed`` line.
_CTEST_CASE = re.compile(
    r"^\s*Test\s+#\d+:\s+(?P<name>\S+)\s+\.+\s*(?P<result>\*\*\*Failed|Passed|Not Run|Timeout)\s*$",
    re.M,
)
#: gcc/clang diagnostics: ``file:line[:col]: error: message``.
_CMAKE_ERROR = re.compile(r"^.+?:\d+(?::\d+)?:\s+(?:fatal\s+)?error:\s+.+$", re.M)
#: CMake's own configure errors.
_CMAKE_CONFIG_ERROR = re.compile(r"^CMake Error(?: at .+?)?:.*$", re.M)
#: Linker failures.
_LINK_ERROR = re.compile(
    r"^(?:.*undefined reference to.*|.*ld: symbol\(s\) not found.*|"
    r".*collect2: error: ld returned.*|.*multiple definition of.*)$",
    re.M,
)
_SANITIZER = re.compile(
    r"(ERROR: AddressSanitizer|runtime error:|SUMMARY: (?:Address|UndefinedBehavior)Sanitizer|"
    r"LeakSanitizer: detected memory leaks)",
    re.M,
)

#: C type names for the contract's Python-ish type names.
_C_TYPES = {
    "str": "const char *", "string": "const char *", "text": "const char *",
    "int": "int", "integer": "int", "float": "double", "double": "double",
    "number": "double", "bool": "bool", "boolean": "bool",
    "date": "const char *", "any": "const char *", "object": "const char *",
    "dict": "const char *", "map": "const char *", "list": "const char *const *",
}

#: C++ type names for the contract's Python-ish type names.
_CPP_TYPES = {
    "str": "std::string", "string": "std::string", "text": "std::string",
    "int": "int", "integer": "int", "float": "double", "double": "double",
    "number": "double", "bool": "bool", "boolean": "bool",
    "date": "std::string", "any": "std::string", "object": "std::string",
    "dict": "std::string", "map": "std::string", "list": "std::vector<std::string>",
}


def component_dir(node: dict[str, Any]) -> str:
    """Directory and target name for one component."""
    return _common.snake(str(node.get("name") or "component"))


def component_prefix(node: dict[str, Any]) -> str:
    """C symbol prefix (``word_counter_count``) for one component."""
    return _common.snake(str(node.get("name") or "component"))


def _header_guard(node: dict[str, Any]) -> str:
    name = component_dir(node).upper()
    return f"NAI_{name}_H"


# -- type mapping -----------------------------------------------------------


def c_type(raw: Any, entities: tuple[str, ...] = (), *, owned: bool = False) -> str:
    """Map a contract type to C. ``owned`` selects the return/value spelling."""
    text = str(raw or "").strip()
    if not text or text in ("void", "None", "none"):
        return "void"
    optional = text.endswith("?")
    if optional:
        text = text.rstrip("?")
    lowered = text.lower()
    if lowered in _C_TYPES:
        base = _C_TYPES[lowered]
        if owned and base == "const char *const *":
            base = "char **"
        elif owned and base.startswith("const char *"):
            base = "char *"
        return base
    if lowered.startswith("list[") and lowered.endswith("]"):
        return "char **" if owned else "const char *const *"
    if text in entities:
        return text if owned else f"const {text} *"
    return "const char *" if not owned else "char *"


def cpp_type(raw: Any, entities: tuple[str, ...] = (), *, owned: bool = False) -> str:
    """Map a contract type to C++. ``owned`` selects the return/value spelling."""
    text = str(raw or "").strip()
    if not text or text in ("void", "None", "none"):
        return "void"
    optional = text.endswith("?")
    if optional:
        text = text.rstrip("?")
    lowered = text.lower()
    if lowered in _CPP_TYPES:
        base = _CPP_TYPES[lowered]
        if optional:
            return f"std::optional<{base}>" if owned else f"const std::optional<{base}> &"
        return base if owned else f"const {base} &"
    if lowered.startswith("list[") and lowered.endswith("]"):
        base = "std::vector<std::string>"
        return base if owned else f"const {base} &"
    if text in entities:
        if optional:
            return f"std::optional<{text}>" if owned else f"const std::optional<{text}> &"
        return text if owned else f"const {text} &"
    return "std::string" if owned else "const std::string &"


def _decl(type_text: str, name: str) -> str:
    """``const char *`` + ``text`` -> ``const char *text`` (C/C++ spacing)."""
    return f"{type_text}{name}" if type_text.endswith(("*", "&")) else f"{type_text} {name}"


def _cpp_headers(types: list[str], entities: tuple[str, ...] = ()) -> list[str]:
    """Standard headers a C++ signature needs, derived from the rendered types."""
    rendered = " ".join(cpp_type(item, entities, owned=True) for item in types)
    headers: list[str] = []
    if "std::string" in rendered:
        headers.append("#include <string>")
    if "std::vector" in rendered:
        headers.append("#include <vector>")
    if "std::optional" in rendered:
        headers.append("#include <optional>")
    if "std::map" in rendered:
        headers.append("#include <map>")
    return headers


def _c_headers(types: list[str], entities: tuple[str, ...] = ()) -> list[str]:
    """Standard headers a C signature needs, derived from the rendered types."""
    rendered = " ".join(c_type(item, entities, owned=True) for item in types)
    headers: list[str] = []
    if "bool" in rendered:
        headers.append("#include <stdbool.h>")
    if "size_t" in rendered:
        headers.append("#include <stddef.h>")
    return headers


# -- entities ---------------------------------------------------------------


def render_entities_c(entities: list[dict[str, Any]]) -> str:
    """Render the shared contract entities as one C header."""
    names = tuple(_common.pascal(str(e.get("name") or "")) for e in entities)
    lines = [
        "/* Generated by nai from the frozen contract. Do not edit: the adapter regenerates this file. */",
        "#ifndef NAI_ENTITIES_H",
        "#define NAI_ENTITIES_H",
        "",
    ]
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        name = _common.pascal(str(entity.get("name") or "Entity"))
        fields = [f for f in (entity.get("fields") or []) if isinstance(f, dict)]
        description = str(entity.get("description") or "").strip()
        if description:
            lines.append(f"/* {description} */")
        lines.append(f"typedef struct {name} {{")
        if not fields:
            lines.append("    int _unused;")
        for field in fields:
            field_name = _common.snake(str(field.get("name") or "value"))
            field_type = c_type(field.get("type"), names)
            lines.append(f"    {field_type} {field_name};")
        lines.append(f"}} {name};")
        lines.append("")
    lines.append("#endif /* NAI_ENTITIES_H */")
    lines.append("")
    return "\n".join(lines)


def render_entities_cpp(entities: list[dict[str, Any]]) -> str:
    """Render the shared contract entities as one C++ header."""
    names = tuple(_common.pascal(str(e.get("name") or "")) for e in entities)
    needs_string = any(
        "std::string" in cpp_type(f.get("type"), names)
        for entity in entities
        for f in (entity.get("fields") or [])
        if isinstance(f, dict)
    )
    lines = [
        "// Generated by nai from the frozen contract. Do not edit: the adapter regenerates this file.",
        "#ifndef NAI_ENTITIES_H",
        "#define NAI_ENTITIES_H",
        "",
    ]
    if needs_string:
        lines.append("#include <string>")
        lines.append("")
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        name = _common.pascal(str(entity.get("name") or "Entity"))
        fields = [f for f in (entity.get("fields") or []) if isinstance(f, dict)]
        description = str(entity.get("description") or "").strip()
        if description:
            lines.append(f"// {description}")
        lines.append(f"struct {name} {{")
        for field in fields:
            field_name = _common.snake(str(field.get("name") or "value"))
            field_type = cpp_type(field.get("type"), names, owned=True)
            lines.append(f"    {field_type} {field_name};")
        lines.append("};")
        lines.append("")
    lines.append("#endif  // NAI_ENTITIES_H")
    lines.append("")
    return "\n".join(lines)


def _uses_entities(component: dict[str, Any], names: tuple[str, ...]) -> bool:
    for provide in component.get("provides") or []:
        if not isinstance(provide, dict):
            continue
        _name, params, returns = _common.parse_provide(provide)
        for _param, kind in params:
            if kind in names:
                return True
        if returns in names:
            return True
    return False


class CFamilyAdapter:
    """Shared base for the C and C++ adapters (CMake + CTest)."""

    language = "c"
    display_name = "C"
    test_framework = "CTest"
    extension = ".c"
    header_extension = ".h"
    extensions: tuple[str, ...] = (".c", ".h")
    stack_hint = "C11"
    standard = "C11"
    #: The bare version CMake wants in ``set(CMAKE_<lang>_STANDARD ...)``.
    cmake_standard = "11"
    project_language = "C"
    stub_marker = "not implemented"
    style_guide = ""
    #: ``cc``/``c++`` resolve to gcc or clang on the host; Ninja is preferred but
    #: optional.  Subclasses narrow the compiler probe to their own language.
    toolchain: tuple[ToolCheck, ...] = (
        ToolCheck("cc", ("cc", "--version"), fallback=("gcc", "clang")),
        ToolCheck("cmake", ("cmake", "--version")),
        ToolCheck("ninja", ("ninja", "--version"), required=False),
        ToolCheck("clang-format", ("clang-format", "--version"), required=False),
        ToolCheck("clang-tidy", ("clang-tidy", "--version"), required=False),
    )

    # -- layout ------------------------------------------------------------
    def package_name(self, project: dict[str, Any]) -> str:
        return _common.snake(str((project or {}).get("name") or "app"))

    def module_import(self, project: dict[str, Any], node: dict[str, Any]) -> str:
        return component_dir(node)

    def header_rel(self, project: dict[str, Any], node: dict[str, Any]) -> str:
        name = component_dir(node)
        return f"include/{name}/{name}{self.header_extension}"

    def interface_rel(self, project: dict[str, Any], node: dict[str, Any]) -> str:
        """The public header the context pack shows instead of the body."""
        return self.header_rel(project, node)

    def source_rel(self, project: dict[str, Any], node: dict[str, Any]) -> str:
        name = component_dir(node)
        return f"src/{name}/{name}{self.extension}"

    def test_rel(self, project: dict[str, Any], node: dict[str, Any]) -> str:
        name = component_dir(node)
        return f"tests/{name}/{name}_test{self.extension}"

    def entities_rel(self, project: dict[str, Any]) -> str:
        return "include/entities/entities.h"

    def normalize_test_path(self, raw_path: str, node: dict[str, Any]) -> str:
        name = component_dir(node)
        return f"tests/{name}/{name}_test{self.extension}"

    def protected_globs(self, project: dict[str, Any]) -> tuple[str, ...]:
        return (
            "CMakeLists.txt",
            "vendor/**",
            "include/entities/**",
            "tests/**",
            "build/**",
        )

    def writable_globs(self, project: dict[str, Any], node: dict[str, Any]) -> tuple[str, ...]:
        name = component_dir(node)
        return (self.source_rel(project, node), f"src/{name}/**", f"include/{name}/**")

    def snapshot_ignore(self) -> tuple[str, ...]:
        return (".git", "build", "vendor")

    def cache_dirs(self) -> tuple[str, ...]:
        return ("build",)

    # -- environment and workspace -----------------------------------------
    def needs_target_env(self) -> bool:
        return False

    def environment_block(self, project: dict[str, Any], workspace_root: Path) -> str:
        return (
            "## Target Environment (TENV-4)\n"
            f"- Language: {self.stack_hint}; CMake >= 3.21 + CTest; one library per component.\n"
            "- Public headers live in `include/<component>/`; implementations in `src/<component>/`.\n"
            "- The adapter owns `CMakeLists.txt`, `vendor/` and `include/entities/`; do not edit them.\n"
            "- Tests build with `-fsanitize=address,undefined` when the compiler supports it."
        )

    # -- stubs -------------------------------------------------------------
    def render_stub(
        self,
        project: dict[str, Any],
        node: dict[str, Any],
        contract: dict[str, Any] | None,
    ) -> StubResult:
        shared = _common.entities(contract)
        files: list[StubFile] = []
        if shared:
            renderer = render_entities_c if self.language == "c" else render_entities_cpp
            files.append(StubFile(self.entities_rel(project), renderer(shared)))
        files.append(StubFile(self.header_rel(project, node), self._render_header(project, node, contract, shared)))
        files.append(StubFile(self.source_rel(project, node), self._render_source(project, node, contract, shared)))
        return StubResult(
            files=tuple(files),
            module_import=self.module_import(project, node),
            package_name=self.package_name(project),
        )

    def _render_header(
        self,
        project: dict[str, Any],
        node: dict[str, Any],
        contract: dict[str, Any] | None,
        shared: list[dict[str, Any]],
    ) -> str:
        guard = _header_guard(node)
        component = _common.component_entry(contract, node)
        provides = [p for p in (component.get("provides") or []) if isinstance(p, dict)]
        entity_names = tuple(_common.pascal(str(e.get("name") or "")) for e in shared)
        uses = _uses_entities(component, entity_names)
        types: list[str] = []
        for provide in provides:
            _op, params, returns = _common.parse_provide(provide)
            types.extend(kind for _param, kind in params)
            types.append(returns)
        lines = [
            f"/* Generated by nai contract stubs for {node.get('name')}. Keep the declarations; implement the bodies. */",
            f"#ifndef {guard}",
            f"#define {guard}",
            "",
        ]
        headers = (
            _c_headers(types, entity_names)
            if self.language == "c"
            else _cpp_headers(types, entity_names)
        )
        if uses:
            lines.append('#include "entities/entities.h"')
        lines.extend(headers)
        if uses or headers:
            lines.append("")
        if self.language == "c":
            lines.extend(self._render_c_declarations(project, node, provides, entity_names))
            lines.extend(["", f"#endif /* {guard} */", ""])
            return "\n".join(lines)
        lines.extend(self._render_cpp_declarations(project, node, provides, entity_names))
        lines.extend(["", f"#endif  // {guard}", ""])
        return "\n".join(lines)

    def _render_c_declarations(
        self,
        project: dict[str, Any],
        node: dict[str, Any],
        provides: list[dict[str, Any]],
        entity_names: tuple[str, ...],
    ) -> list[str]:
        prefix = component_prefix(node)
        if str(node.get("kind") or "") == "integration":
            return ["int integrate(void);"]
        lines: list[str] = []
        for provide in provides:
            name, params, returns = _common.parse_provide(provide)
            rendered = ", ".join(
                _decl(c_type(kind, entity_names), _common.snake(param)) for param, kind in params
            )
            if not rendered:
                rendered = "void"
            lines.append(
                f"{_decl(c_type(returns, entity_names, owned=True), f'{prefix}_{_common.snake(name)}')}"
                f"({rendered});"
            )
        return lines or [f"/* {component_dir(node)} has no contract operations yet. */"]

    def _render_cpp_declarations(
        self,
        project: dict[str, Any],
        node: dict[str, Any],
        provides: list[dict[str, Any]],
        entity_names: tuple[str, ...],
    ) -> list[str]:
        if str(node.get("kind") or "") == "integration":
            return ["int integrate();"]
        lines = [f"namespace {component_dir(node)} {{", ""]
        for provide in provides:
            name, params, returns = _common.parse_provide(provide)
            rendered = ", ".join(
                _decl(cpp_type(kind, entity_names), _common.snake(param))
                for param, kind in params
            )
            lines.append(
                f"{_decl(cpp_type(returns, entity_names, owned=True), _common.snake(name))}"
                f"({rendered});"
            )
        lines.extend(["", f"}}  // namespace {component_dir(node)}"])
        return lines

    def _public_include(self, node: dict[str, Any]) -> str:
        """The header path as seen from the ``include/`` directory."""
        name = component_dir(node)
        return f"{name}/{name}{self.header_extension}"

    def _render_source(
        self,
        project: dict[str, Any],
        node: dict[str, Any],
        contract: dict[str, Any] | None,
        shared: list[dict[str, Any]],
    ) -> str:
        header = self._public_include(node)
        component = _common.component_entry(contract, node)
        provides = [p for p in (component.get("provides") or []) if isinstance(p, dict)]
        entity_names = tuple(_common.pascal(str(e.get("name") or "")) for e in shared)
        if self.language == "c":
            return self._render_c_source(node, header, provides, entity_names)
        return self._render_cpp_source(node, header, provides, entity_names)

    def _render_c_source(
        self,
        node: dict[str, Any],
        header: str,
        provides: list[dict[str, Any]],
        entity_names: tuple[str, ...],
    ) -> str:
        lines = [
            f"/* Generated by nai contract stubs for {node.get('name')}. Keep the signatures; implement the bodies. */",
            f'#include "{header}"',
            '#include "nai_runtime.h"',
            "",
        ]
        if str(node.get("kind") or "") == "integration":
            lines.extend([
                "int integrate(void) {",
                '    NOT_IMPLEMENTED("integrate");',
                "}",
                "",
            ])
            return "\n".join(lines)
        prefix = component_prefix(node)
        for provide in provides:
            name, params, returns = _common.parse_provide(provide)
            rendered = ", ".join(
                _decl(c_type(kind, entity_names), _common.snake(param)) for param, kind in params
            )
            if not rendered:
                rendered = "void"
            lines.append(
                f"{_decl(c_type(returns, entity_names, owned=True), f'{prefix}_{_common.snake(name)}')}"
                f"({rendered}) {{"
            )
            lines.append(f'    NOT_IMPLEMENTED("{prefix}_{_common.snake(name)}");')
            lines.append("}")
            lines.append("")
        if not provides:
            lines.append(f"/* {component_dir(node)} has no contract operations yet. */")
            lines.append("")
        return "\n".join(lines)

    def _render_cpp_source(
        self,
        node: dict[str, Any],
        header: str,
        provides: list[dict[str, Any]],
        entity_names: tuple[str, ...],
    ) -> str:
        lines = [
            f"// Generated by nai contract stubs for {node.get('name')}. Keep the signatures; implement the bodies.",
            f'#include "{header}"',
            "#include <stdexcept>",
            "",
        ]
        if str(node.get("kind") or "") == "integration":
            lines.extend([
                "int integrate() {",
                '    throw std::logic_error("not implemented");',
                "}",
                "",
            ])
            return "\n".join(lines)
        lines.append(f"namespace {component_dir(node)} {{")
        lines.append("")
        for provide in provides:
            name, params, returns = _common.parse_provide(provide)
            rendered = ", ".join(
                _decl(cpp_type(kind, entity_names), _common.snake(param))
                for param, kind in params
            )
            lines.append(
                f"{_decl(cpp_type(returns, entity_names, owned=True), _common.snake(name))}"
                f"({rendered}) {{"
            )
            lines.append('    throw std::logic_error("not implemented");')
            lines.append("}")
            lines.append("")
        lines.extend([f"}}  // namespace {component_dir(node)}", ""])
        return "\n".join(lines)

    # -- workspace preparation ---------------------------------------------
    def prepare_workspace(self, project: dict[str, Any], workspace_root: Path) -> tuple[str, ...]:
        """Regenerate the manifest, vendor headers and the C++ doctest main."""
        root = Path(workspace_root)
        _reject_mixed_language(root, self.language)
        written: list[str] = []
        for header in VENDOR_HEADERS:
            source = VENDOR_DIR / header
            if not source.is_file():  # pragma: no cover - shipped package data
                continue
            rel = f"vendor/{header}"
            if _write_if_changed(root / rel, source.read_text(encoding="utf-8")):
                written.append(rel)
        if self.language == "cpp":
            main = (
                "// Generated by nai. Do not edit: the adapter regenerates this file.\n"
                "#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN\n"
                '#include "doctest.h"\n'
            )
            if _write_if_changed(root / "tests" / "doctest_main.cpp", main):
                written.append("tests/doctest_main.cpp")
        # Integration nodes have no contract stub, so nothing else creates their
        # public header.  Generate it once the glue source exists; the coder owns
        # the source, the adapter owns the declaration.
        for name in _integration_dirs(root, self.extension):
            rel = f"include/{name}/{name}{self.header_extension}"
            if (root / rel).is_file():
                continue
            guard = f"NAI_{name.upper()}_H"
            declaration = "int integrate(void);" if self.language == "c" else "int integrate();"
            content = (
                "/* Generated by nai for the integration node. Do not edit. */\n"
                f"#ifndef {guard}\n"
                f"#define {guard}\n"
                "\n"
                f"{declaration}\n"
                "\n"
                f"#endif /* {guard} */\n"
            )
            if _write_if_changed(root / rel, content):
                written.append(rel)
        manifest = render_cmake(
            project,
            root,
            language=self.project_language,
            extension=self.extension,
            standard=self.cmake_standard,
            cxx=self.language == "cpp",
        )
        if _write_if_changed(root / "CMakeLists.txt", manifest):
            written.append("CMakeLists.txt")
        return tuple(written)

    def validate_stub(
        self,
        workspace_root: Path,
        module_import: str,
        run_cmd: Any,
        *,
        executable: str | None = None,
        run_setup_cmd: Any | None = None,
    ) -> tuple[bool, str]:
        """Configure and build the stubs; warnings are not failures."""
        import shlex

        generator = _generator()
        configure = shlex.join(
            ["cmake", "-S", ".", "-B", "build", "-G", generator, "-DCMAKE_BUILD_TYPE=Debug"]
        )
        code, output = run_cmd(configure)
        if code != 0:
            return False, output
        code, output = run_cmd("cmake --build build")
        return code == 0, output

    def extract_signatures(self, source: str) -> str:
        """Public declarations only, never bodies (BLD-1)."""
        found: list[str] = []
        for line in (source or "").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith(("/*", "*", "//", "#")):
                continue
            if stripped.endswith(";") and "(" in stripped:
                found.append(stripped)
        return "\n".join(found)

    # -- commands ----------------------------------------------------------
    def setup_commands(self, project: dict[str, Any], workspace_root: Path) -> tuple[Command, ...]:
        """Configure the out-of-tree build directory."""
        return (
            Command(
                argv=("cmake", "-S", ".", "-B", "build", "-G", _generator(), "-DCMAKE_BUILD_TYPE=Debug"),
                network=False,
            ),
        )

    def build_commands(self, project: dict[str, Any], workspace_root: Path) -> tuple[Command, ...]:
        return (Command(argv=("cmake", "--build", "build"), network=False),)

    def test_command(
        self,
        project: dict[str, Any],
        node: dict[str, Any],
        *,
        scope: str | None = None,
        paths: tuple[str, ...] = (),
    ) -> Command:
        """Configure, build and run the component's test binary in one argv.

        Running the binary directly (rather than ``ctest``) keeps the JUnit XML
        on stdout: doctest's ``--reporters=junit`` reporter and the C harness
        both print it there, so ``parse_result`` gets machine-readable results.
        """
        target = f"{component_dir(node)}_test"
        extra = ("--reporters=junit",) if self.language == "cpp" else ()
        return Command(
            argv=(
                "ctest",
                "--build-and-test",
                ".",
                "build",
                "--build-generator",
                _generator(),
                "--build-options",
                "-DCMAKE_BUILD_TYPE=Debug",
                "--test-command",
                f"./{target}",
                *extra,
            ),
            network=False,
        )

    def lint_command(
        self,
        project: dict[str, Any],
        workspace_root: Path,
        targets: tuple[str, ...],
    ) -> Command | None:
        """``clang-format --dry-run`` only when clang-format is installed."""
        if not targets or not shutil.which("clang-format"):
            return None
        return Command(argv=("clang-format", "--dry-run", "--Werror", *targets), network=False)

    def env_overrides(self, project: dict[str, Any], workspace_root: Path) -> dict[str, str]:
        return {"CTEST_OUTPUT_ON_FAILURE": "1", "ASAN_OPTIONS": "detect_leaks=1"}

    # -- results -----------------------------------------------------------
    def parse_result(
        self,
        raw_output: str,
        exit_code: int,
        kind: str,
        *,
        timed_out: bool = False,
    ) -> ParseResult:
        """Classify CMake/CTest output and JUnit XML."""
        text = raw_output or ""
        if timed_out:
            return ParseResult(Outcome.TIMEOUT, exit_code=exit_code, raw_output=text, timed_out=True)
        cases = _junit_cases(text) or _ctest_cases(text)
        errors = _compile_errors(text)
        marker = "not implemented" in text.lower()
        sanitizer = _SANITIZER.search(text)
        zero_tests = bool(_EMPTY_TEST.search(text))
        failed_cases = any(case.outcome in ("failed", "error") for case in cases)
        if exit_code == 0:
            if zero_tests:
                outcome = Outcome.NO_TESTS
            elif failed_cases:
                outcome = Outcome.TEST_FAIL
            else:
                outcome = Outcome.PASS
        elif errors or kind == "build":
            outcome = Outcome.BUILD_ERROR
        else:
            outcome = Outcome.TEST_FAIL
        messages = list(errors)
        if sanitizer:
            messages.append(sanitizer.group(0))
        return ParseResult(
            outcome=outcome,
            exit_code=exit_code,
            tests=cases,
            failure_messages=tuple(dict.fromkeys(messages)),
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
        """A stub is valid when the build succeeded and the run hit the marker."""
        if result.timed_out:
            return False
        if result.outcome in (Outcome.PASS, Outcome.NO_TESTS):
            return False
        if result.outcome is Outcome.BUILD_ERROR:
            # Integration nodes have no contract stub: the expected pre-stub
            # state is the missing integration header, not a marker trap.
            return bool(
                integration
                and _common.integration_build_failure(result.raw_output, expected_module or "")
            )
        return result.marker_present

    def extract_test_functions(self, test_sources: dict[str, str]) -> tuple[list[str], list[str]]:
        """Test case names from the C harness or doctest macros."""
        names: list[str] = []
        errors: list[str] = []
        for path, source in sorted(test_sources.items()):
            text = str(source or "")
            if not text.strip():
                errors.append(f"{path}: empty test file")
                continue
            found = _CASE_NAME.findall(text)
            if not found:
                errors.append(f"{path}: no TEST_CASE()/nai_case() found")
            names.extend(found)
        return names, errors

    def check_syntax(self, code: str, filename: str) -> str | None:
        """Structural pre-check; preprocessor lines may contain braces."""
        return _common.structural_error(code, filename, preprocessor=True)

    # -- platform lint -----------------------------------------------------
    def platform_lint(
        self,
        project: dict[str, Any],
        node: dict[str, Any],
        workspace_root: Path,
        *,
        platform: str = "any",
    ) -> tuple[str, ...]:
        """A portable component may not include Apple-only frameworks."""
        if str(platform).lower() not in ("any", "linux"):
            return ()
        root = Path(workspace_root)
        name = component_dir(node)
        findings: list[str] = []
        for base in (root / "src" / name, root / "include" / name):
            if not base.is_dir():
                continue
            for path in sorted(base.rglob(f"*{self.extension}")):
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:  # pragma: no cover - unreadable file is reported by verify
                    continue
                for match in _IMPORT_APPLE.finditer(text):
                    if match.group(1) in _APPLE_FRAMEWORKS:
                        findings.append(
                            f"{path.relative_to(root).as_posix()}: includes {match.group(1)}; "
                            f"a platform {platform} component must not use Apple frameworks"
                        )
        return tuple(findings)

    # -- remote execution --------------------------------------------------
    def remote_recipe(
        self, project: dict[str, Any], node: dict[str, Any], job_kind: str, *, platform: str = "any"
    ) -> str:
        if job_kind in ("verify", "author_tests", "integrate", "lint"):
            return "ctest"
        return "cmake-build"

    def remote_params(
        self,
        project: dict[str, Any],
        node: dict[str, Any],
        job_kind: str,
        *,
        targets: tuple[str, ...] = (),
        platform: str = "any",
    ) -> dict[str, Any]:
        if job_kind in ("verify", "author_tests", "integrate", "lint"):
            return {"filter": f"{component_dir(node)}_test"}
        return {}


# -- CMake generation -------------------------------------------------------


def _language_dirs(root: Path, relative: str, extension: str) -> list[str]:
    base = Path(root) / relative
    if not base.is_dir():
        return []
    return sorted(
        child.name for child in base.iterdir() if child.is_dir() and any(child.glob(f"*{extension}"))
    )


def _integration_dirs(root: Path, extension: str) -> list[str]:
    """Integration glue directories under ``src/`` (``*_integration``).

    The CMake generator already treats a ``*_integration`` target as the node
    that wires every component together, so the same convention identifies the
    headers the adapter must generate for integration nodes.
    """
    base = Path(root) / "src"
    if not base.is_dir():
        return []
    return sorted(
        child.name
        for child in base.iterdir()
        if child.is_dir()
        and child.name.endswith("_integration")
        and any(child.glob(f"*{extension}"))
    )


def _relative_sources(root: Path, relative: str, name: str, extension: str) -> list[str]:
    base = Path(root) / relative / name
    if not base.is_dir():
        return []
    return sorted(f"{relative}/{name}/{child.name}" for child in base.glob(f"*{extension}"))


def _component_dependencies(project: dict[str, Any]) -> dict[str, list[str]]:
    """``consumes``/``depends_on`` edges from the frozen component map."""
    edges: dict[str, list[str]] = {}
    for component in _common.components(project):
        name = _common.snake(str(component.get("name") or ""))
        if not name:
            continue
        targets: list[str] = []
        for key in ("consumes", "depends_on"):
            for dep in component.get(key) or []:
                target = _common.snake(str(dep))
                if target and target != name and target not in targets:
                    targets.append(target)
        edges[name] = targets
    return edges


def render_cmake(
    project: dict[str, Any],
    workspace_root: Path,
    *,
    language: str,
    extension: str,
    standard: str,
    cxx: bool,
) -> str:
    """Render a deterministic ``CMakeLists.txt`` for the on-disk targets."""
    root = Path(workspace_root)
    libraries = _language_dirs(root, "src", extension)
    tests = _language_dirs(root, "tests", extension)
    edges = _component_dependencies(project)
    lines = [
        "# Generated by nai. Do not edit: the adapter regenerates this file.",
        "cmake_minimum_required(VERSION 3.21)",
        f"project({_common.snake(str(project.get('name') or 'project'))} {language})",
        "",
        f"set(CMAKE_{language}_STANDARD {standard})",
        f"set(CMAKE_{language}_STANDARD_REQUIRED ON)",
        f"set(CMAKE_{language}_EXTENSIONS OFF)",
        "",
        "include(CTest)",
        "enable_testing()",
        "",
        "include(CheckCCompilerFlag)" if not cxx else "include(CheckCXXCompilerFlag)",
        'check_c_compiler_flag("-fsanitize=address,undefined" NAI_HAVE_SANITIZERS)'
        if not cxx
        else 'check_cxx_compiler_flag("-fsanitize=address,undefined" NAI_HAVE_SANITIZERS)',
        "",
    ]
    for library in libraries:
        sources = _relative_sources(root, "src", library, extension)
        if not sources:
            continue
        lines.append(f"add_library({library} STATIC {' '.join(sources)})")
        lines.append(
            f"target_include_directories({library} PUBLIC "
            f"${{CMAKE_CURRENT_SOURCE_DIR}}/include ${{CMAKE_CURRENT_SOURCE_DIR}}/vendor)"
        )
        warning = "-Wall -Wextra -Werror=return-type"
        if not cxx:
            warning += " -Werror=implicit-function-declaration"
        lines.append(f"target_compile_options({library} PRIVATE {warning})")
        for dependency in edges.get(library, []):
            if dependency in libraries:
                lines.append(f"target_link_libraries({library} PUBLIC {dependency})")
        if library.endswith("_integration"):
            # The integration node wires every component together, so it links
            # all of them even though the frozen map has no edges for it.
            for other in libraries:
                if other != library and not other.endswith("_integration"):
                    lines.append(f"target_link_libraries({library} PUBLIC {other})")
        lines.append("")
    for test in tests:
        sources = _relative_sources(root, "tests", test, extension)
        if not sources:
            continue
        if cxx:
            sources = [*sources, "tests/doctest_main.cpp"]
        lines.append(f"add_executable({test}_test {' '.join(sources)})")
        if test in libraries:
            lines.append(f"target_link_libraries({test}_test PRIVATE {test})")
        lines.append(
            f"target_include_directories({test}_test PRIVATE "
            f"${{CMAKE_CURRENT_SOURCE_DIR}}/include ${{CMAKE_CURRENT_SOURCE_DIR}}/vendor)"
        )
        lines.append(f"target_compile_options({test}_test PRIVATE -Wall -Wextra -g -O0)")
        lines.append("if(NAI_HAVE_SANITIZERS)")
        lines.append(
            f"  target_compile_options({test}_test PRIVATE -fsanitize=address,undefined -fno-omit-frame-pointer)"
        )
        lines.append(f"  target_link_options({test}_test PRIVATE -fsanitize=address,undefined)")
        lines.append("endif()")
        lines.append(f"add_test(NAME {test}_test COMMAND {test}_test)")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _generator() -> str:
    """Prefer Ninja; fall back to Make when it is not installed."""
    return "Ninja" if shutil.which("ninja") else "Unix Makefiles"


def _write_if_changed(path: Path, content: str) -> bool:
    if path.is_file() and path.read_text(encoding="utf-8") == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


# -- result parsing ---------------------------------------------------------


def _unescape(text: str) -> str:
    """Decode the XML entities the JUnit writers emit in failure messages."""
    return (
        text.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&apos;", "'")
        .replace("&amp;", "&")
    )


def _junit_cases(text: str) -> tuple[TestCase, ...]:
    """Test cases from JUnit XML emitted by doctest or the C harness."""
    found: list[TestCase] = []
    for match in _TESTCASE.finditer(text or ""):
        attrs = dict(_ATTR.findall(match.group("attrs")))
        name = attrs.get("name") or attrs.get("classname") or "test"
        body = match.group("body") or ""
        failure = _FAILURE.search(body)
        error = _ERROR_MSG.search(body)
        if failure:
            outcome, message = "failed", _unescape(failure.group(1))
        elif error:
            outcome, message = "error", _unescape(error.group(1))
        elif "<skipped" in body:
            outcome, message = "skipped", ""
        else:
            outcome, message = "passed", ""
        found.append(TestCase(name=name, outcome=outcome, message=message[:2000]))
    return tuple(found)


def _ctest_cases(text: str) -> tuple[TestCase, ...]:
    """CTest's stable ``Test #N: name ... Passed`` lines."""
    found: list[TestCase] = []
    for match in _CTEST_CASE.finditer(text or ""):
        name = match.group("name").strip()
        result = match.group("result").strip()
        outcome = "passed" if result == "Passed" else "failed"
        found.append(TestCase(name=name, outcome=outcome))
    return tuple(found)


def _compile_errors(text: str) -> tuple[str, ...]:
    """Compiler diagnostics and linker errors from a CMake/CTest run."""
    found: list[str] = []
    for pattern in (_CMAKE_ERROR, _CMAKE_CONFIG_ERROR, _LINK_ERROR):
        for match in pattern.finditer(text or ""):
            found.append(match.group(0).strip())
    return tuple(dict.fromkeys(found))


def _reject_mixed_language(root: Path, language: str) -> None:
    """Refuse a workspace that mixes C and C++ sources (one language per project)."""
    foreign = (".cpp", ".cc", ".cxx", ".hpp", ".hxx") if language == "c" else (".c",)
    found: list[str] = []
    for base in ("src", "tests", "include"):
        directory = root / base
        if not directory.is_dir():
            continue
        for path in directory.rglob("*"):
            if path.is_file() and path.suffix in foreign:
                found.append(path.relative_to(root).as_posix())
    if found:
        raise ValueError(
            f"mixed C/C++ project: the {language} adapter found "
            f"{', '.join(sorted(found)[:5])}; one language per project"
        )


# -- concrete adapters ------------------------------------------------------


class CAdapter(CFamilyAdapter):
    """C11 adapter: CMake + CTest, a vendored single-header assert harness."""

    id = "c"
    display_name = "C"
    language = "c"
    extension = ".c"
    header_extension = ".h"
    stack_hint = "C11 / CMake 3.21 / CTest"
    standard = "C11"
    project_language = "C"
    stub_marker = "not implemented"
    style_guide = (
        "- State memory ownership in every contract: say who allocates and who frees.\n"
        "- No hidden globals; pass state through parameters and return error codes.\n"
        "- Every public header needs an include guard and only the declarations callers need.\n"
        "- Use `size_t` for lengths and indices, check every bound, and never rely on\n"
        "  signed/unsigned wraparound.\n"
        "- Contract stubs call `NOT_IMPLEMENTED(name)`; tests use the vendored `nai_assert.h`.\n"
        "- Do not edit `CMakeLists.txt`, `vendor/`, `include/entities/` or the generated headers."
    )
    toolchain: tuple[ToolCheck, ...] = (
        ToolCheck("cc", ("cc", "--version"), fallback=("gcc", "clang")),
        ToolCheck("cmake", ("cmake", "--version")),
        ToolCheck("ninja", ("ninja", "--version"), required=False),
        ToolCheck("clang-format", ("clang-format", "--version"), required=False),
        ToolCheck("clang-tidy", ("clang-tidy", "--version"), required=False),
    )


class CppAdapter(CFamilyAdapter):
    """C++17 adapter: CMake + CTest, vendored doctest."""

    id = "cpp"
    display_name = "C++"
    language = "cpp"
    extension = ".cpp"
    header_extension = ".h"
    extensions: tuple[str, ...] = (".cpp", ".cc", ".cxx", ".h", ".hpp")
    stack_hint = "C++17 / CMake 3.21 / CTest / doctest"
    standard = "C++17"
    cmake_standard = "17"
    project_language = "CXX"
    stub_marker = "not implemented"
    style_guide = (
        "- Use RAII and smart pointers; never own a resource through a raw pointer.\n"
        "- Be const-correct and follow the rule of zero (or five when you own a resource).\n"
        "- Avoid unnecessary templates; prefer plain functions and `std::` containers.\n"
        "- Put declarations in headers and definitions in `.cpp`; keep headers include-safe.\n"
        "- Contract stubs throw `std::logic_error(\"not implemented\")`; tests use doctest.\n"
        "- Do not edit `CMakeLists.txt`, `vendor/`, `include/entities/` or the generated headers."
    )
    toolchain: tuple[ToolCheck, ...] = (
        ToolCheck("c++", ("c++", "--version"), fallback=("g++", "clang++")),
        ToolCheck("cmake", ("cmake", "--version")),
        ToolCheck("ninja", ("ninja", "--version"), required=False),
        ToolCheck("clang-format", ("clang-format", "--version"), required=False),
        ToolCheck("clang-tidy", ("clang-tidy", "--version"), required=False),
    )
