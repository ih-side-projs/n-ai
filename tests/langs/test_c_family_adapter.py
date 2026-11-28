"""C and C++ adapters: CMake generation, stubs, JUnit parsing, recipes.

Both adapters share ``_c_family.CFamilyAdapter``; these tests parametrise over
``c`` and ``cpp`` where the behaviour is shared and pin the differences (source
extension, stub syntax, test framework) explicitly.

The samples under ``tests/langs/fixtures/{c,cpp}`` were captured from a real
toolchain (see their READMEs).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nai.langs import knowledge, registry, support
from nai.langs._c_family import (
    CAdapter,
    CppAdapter,
    c_type,
    cpp_type,
    render_cmake,
    render_entities_c,
    render_entities_cpp,
)
from nai.langs.base import Outcome

FIXTURES = Path(__file__).resolve().parent / "fixtures"
LANG_FIXTURES = Path(__file__).resolve().parents[1] / "e2e" / "fixtures" / "langs"

ADAPTERS = [pytest.param(CAdapter, id="c"), pytest.param(CppAdapter, id="cpp")]


def _sample(language: str, name: str) -> str:
    return (FIXTURES / language / name).read_text(encoding="utf-8")


@pytest.fixture(params=ADAPTERS)
def adapter(request):
    return request.param()


def _project() -> dict:
    return {"name": "Tiny Library", "settings": {"package_name": "tiny_library"}}


def _node(name: str, kind: str = "component") -> dict:
    return {"id": f"id-{name}", "name": name, "kind": kind, "parent_id": "root", "contract": {}}


CONTRACT = {
    "entities": [
        {
            "name": "Input",
            "description": "Text input",
            "fields": [
                {"name": "text", "type": "str"},
                {"name": "note", "type": "str", "required": False},
            ],
        },
    ],
    "components": [
        {
            "name": "text-normalizer",
            "provides": [
                {
                    "name": "normalize_text",
                    "signature": "normalize_text(text: str) -> str",
                    "description": "Collapse whitespace.",
                }
            ],
            "consumes": [],
        },
        {
            "name": "word-counter",
            "provides": [{"name": "count_words", "signature": "count_words(text: str) -> int"}],
            "consumes": ["text-normalizer"],
        },
    ],
    "scenarios": [{"id": "S1", "title": "Normalize then count", "steps": []}],
}



# ---------------------------------------------------------------------------
# type mapping and entities
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, owned, expected",
    [
        ("str", False, "const char *"),
        ("str", True, "char *"),
        ("int", True, "int"),
        ("float", True, "double"),
        ("bool", True, "bool"),
        ("dict", False, "const char *"),
        ("list", False, "const char *const *"),
        ("list", True, "char **"),
        ("Input", False, "const Input *"),
        ("Input", True, "Input"),
        ("mystery type", False, "const char *"),
        ("", False, "void"),
    ],
)
def test_c_types(raw: str, owned: bool, expected: str) -> None:
    assert c_type(raw, ("Input",), owned=owned) == expected


@pytest.mark.parametrize(
    "raw, owned, expected",
    [
        ("str", False, "const std::string &"),
        ("str", True, "std::string"),
        ("int", True, "int"),
        ("bool", True, "bool"),
        ("list", False, "const std::vector<std::string> &"),
        ("list", True, "std::vector<std::string>"),
        ("str?", False, "const std::optional<std::string> &"),
        ("str?", True, "std::optional<std::string>"),
        ("Input", False, "const Input &"),
        ("Input", True, "Input"),
        ("", False, "void"),
    ],
)
def test_cpp_types(raw: str, owned: bool, expected: str) -> None:
    assert cpp_type(raw, ("Input",), owned=owned) == expected


def test_c_entities_are_a_header() -> None:
    rendered = render_entities_c(CONTRACT["entities"])
    assert "#ifndef NAI_ENTITIES_H" in rendered
    assert "typedef struct Input {" in rendered
    assert "    const char * text;" in rendered
    assert "} Input;" in rendered
    assert "#endif /* NAI_ENTITIES_H */" in rendered
    assert rendered == render_entities_c(CONTRACT["entities"])


def test_cpp_entities_are_a_header_with_string() -> None:
    rendered = render_entities_cpp(CONTRACT["entities"])
    assert "#include <string>" in rendered
    assert "struct Input {" in rendered
    assert "    std::string text;" in rendered
    assert "#endif  // NAI_ENTITIES_H" in rendered
    assert rendered == render_entities_cpp(CONTRACT["entities"])


# ---------------------------------------------------------------------------
# layout and stubs
# ---------------------------------------------------------------------------


def test_layout_uses_headers_sources_and_tests(adapter) -> None:
    project, node = _project(), _node("text-normalizer")
    ext = ".c" if adapter.id == "c" else ".cpp"
    assert adapter.header_rel(project, node) == "include/text_normalizer/text_normalizer.h"
    assert adapter.interface_rel(project, node) == adapter.header_rel(project, node)
    assert adapter.source_rel(project, node) == f"src/text_normalizer/text_normalizer{ext}"
    assert adapter.test_rel(project, node) == f"tests/text_normalizer/text_normalizer_test{ext}"
    assert adapter.entities_rel(project) == "include/entities/entities.h"
    assert adapter.module_import(project, node) == "text_normalizer"
    assert adapter.normalize_test_path("anything.c", node) == adapter.test_rel(project, node)


def test_stub_renders_header_and_source(adapter) -> None:
    project, node = _project(), _node("text-normalizer")
    stub = adapter.render_stub(project, node, CONTRACT)
    assert [f.rel_path for f in stub.files] == [
        "include/entities/entities.h",
        adapter.header_rel(project, node),
        adapter.source_rel(project, node),
    ]
    header = stub.files[1].content
    assert "#ifndef NAI_TEXT_NORMALIZER_H" in header
    source = stub.files[2].content
    assert '#include "text_normalizer/text_normalizer.h"' in source
    if adapter.id == "c":
        assert "char *text_normalizer_normalize_text(const char *text);" in header
        assert "char *text_normalizer_normalize_text(const char *text) {" in source
        assert 'NOT_IMPLEMENTED("text_normalizer_normalize_text");' in source
        assert '#include "nai_runtime.h"' in source
    else:
        assert "namespace text_normalizer {" in header
        assert "std::string normalize_text(const std::string &text);" in header
        assert "std::string normalize_text(const std::string &text) {" in source
        assert 'throw std::logic_error("not implemented");' in source


def test_stub_maps_the_contract_return_type(adapter) -> None:
    project, node = _project(), _node("word-counter")
    source = adapter.render_stub(project, node, CONTRACT).files[-1].content
    if adapter.id == "c":
        assert "int word_counter_count_words(const char *text) {" in source
    else:
        assert "int count_words(const std::string &text) {" in source


def test_integration_stub_exposes_the_conventional_entry_point(adapter) -> None:
    project = _project()
    stub = adapter.render_stub(project, _node("root-integration", "integration"), CONTRACT)
    header, source = stub.files[-2].content, stub.files[-1].content
    if adapter.id == "c":
        assert "int integrate(void);" in header
        assert "int integrate(void) {" in source
        assert 'NOT_IMPLEMENTED("integrate");' in source
    else:
        assert "int integrate();" in header
        assert "int integrate() {" in source
        assert 'throw std::logic_error("not implemented");' in source


def test_extract_signatures_keeps_declarations_only(adapter) -> None:
    header = (
        "/* comment */\n"
        "#ifndef X_H\n"
        "#define X_H\n"
        "\n"
        "int text_normalizer_normalize_text(const char *text);\n"
        "\n"
        "#endif\n"
    )
    assert adapter.extract_signatures(header) == (
        "int text_normalizer_normalize_text(const char *text);"
    )



# ---------------------------------------------------------------------------
# CMake generation and workspace preparation
# ---------------------------------------------------------------------------


def _workspace(tmp_path: Path, language: str) -> Path:
    ext = ".c" if language == "c" else ".cpp"
    for base in ("src/text_normalizer", "src/word_counter", "tests/text_normalizer"):
        (tmp_path / base).mkdir(parents=True, exist_ok=True)
    (tmp_path / f"src/text_normalizer/text_normalizer{ext}").write_text("// stub\n", encoding="utf-8")
    (tmp_path / f"src/word_counter/word_counter{ext}").write_text("// stub\n", encoding="utf-8")
    (tmp_path / f"tests/text_normalizer/text_normalizer_test{ext}").write_text(
        "// test\n", encoding="utf-8"
    )
    return tmp_path


def test_cmake_declares_libraries_tests_and_standards(tmp_path: Path) -> None:
    project = _project()
    _workspace(tmp_path, "c")
    cmake = render_cmake(
        project, tmp_path, language="C", extension=".c", standard="11", cxx=False
    )
    assert "cmake_minimum_required(VERSION 3.21)" in cmake
    assert "project(tiny_library C)" in cmake
    assert "set(CMAKE_C_STANDARD 11)" in cmake
    assert "add_library(text_normalizer STATIC src/text_normalizer/text_normalizer.c)" in cmake
    assert "add_executable(text_normalizer_test tests/text_normalizer/text_normalizer_test.c)" in cmake
    assert "target_link_libraries(text_normalizer_test PRIVATE text_normalizer)" in cmake
    assert "add_test(NAME text_normalizer_test COMMAND text_normalizer_test)" in cmake
    assert cmake == render_cmake(
        project, tmp_path, language="C", extension=".c", standard="11", cxx=False
    )


def test_cmake_uses_cxx_and_links_contract_edges(tmp_path: Path) -> None:
    project = {
        "name": "Tiny Library",
        "settings": {
            "package_name": "tiny_library",
            "component_map": {
                "components": [
                    {"name": "text-normalizer"},
                    {"name": "word-counter", "depends_on": ["text-normalizer"]},
                ]
            },
        },
    }
    _workspace(tmp_path, "cpp")
    cmake = render_cmake(
        project, tmp_path, language="CXX", extension=".cpp", standard="17", cxx=True
    )
    assert "project(tiny_library CXX)" in cmake
    assert "set(CMAKE_CXX_STANDARD 17)" in cmake
    assert "target_link_libraries(word_counter PUBLIC text_normalizer)" in cmake
    assert "tests/doctest_main.cpp" in cmake


def test_cmake_integration_targets_link_every_component(tmp_path: Path) -> None:
    project = _project()
    _workspace(tmp_path, "c")
    (tmp_path / "src/root_integration").mkdir(parents=True)
    (tmp_path / "src/root_integration/root_integration.c").write_text("// glue\n", encoding="utf-8")
    cmake = render_cmake(project, tmp_path, language="C", extension=".c", standard="11", cxx=False)
    assert "target_link_libraries(root_integration PUBLIC text_normalizer)" in cmake
    assert "target_link_libraries(root_integration PUBLIC word_counter)" in cmake


def test_prepare_workspace_vendors_headers_and_writes_cmake(tmp_path: Path, adapter) -> None:
    project = _project()
    _workspace(tmp_path, adapter.id)
    written = adapter.prepare_workspace(project, tmp_path)
    assert "vendor/nai_assert.h" in written
    assert "vendor/nai_runtime.h" in written
    assert "vendor/doctest.h" in written
    assert "CMakeLists.txt" in written
    if adapter.id == "cpp":
        assert "tests/doctest_main.cpp" in written
        main = (tmp_path / "tests/doctest_main.cpp").read_text(encoding="utf-8")
        assert "#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN" in main
    else:
        assert "tests/doctest_main.cpp" not in written
    assert adapter.prepare_workspace(project, tmp_path) == ()


def test_prepare_workspace_generates_the_integration_header(tmp_path: Path, adapter) -> None:
    project = _project()
    _workspace(tmp_path, adapter.id)
    ext = ".c" if adapter.id == "c" else ".cpp"
    (tmp_path / "src/root_integration").mkdir(parents=True)
    (tmp_path / f"src/root_integration/root_integration{ext}").write_text("// glue\n", encoding="utf-8")
    adapter.prepare_workspace(project, tmp_path)
    header = (tmp_path / "include/root_integration/root_integration.h").read_text(encoding="utf-8")
    assert "#ifndef NAI_ROOT_INTEGRATION_H" in header
    assert ("int integrate(void);" if adapter.id == "c" else "int integrate();") in header


def test_prepare_workspace_rejects_a_mixed_language_tree(tmp_path: Path) -> None:
    project = _project()
    (tmp_path / "src/text_normalizer").mkdir(parents=True)
    (tmp_path / "src/text_normalizer/text_normalizer.cpp").write_text("// c++\n", encoding="utf-8")
    with pytest.raises(ValueError, match="mixed C/C\\+\\+ project"):
        CAdapter().prepare_workspace(project, tmp_path)


def test_protected_and_writable_globs(adapter) -> None:
    project, node = _project(), _node("text-normalizer")
    for pattern in ("CMakeLists.txt", "vendor/**", "include/entities/**", "tests/**", "build/**"):
        assert pattern in adapter.protected_globs(project)
    assert adapter.writable_globs(project, node) == (
        adapter.source_rel(project, node),
        "src/text_normalizer/**",
        "include/text_normalizer/**",
    )
    assert "build" in adapter.snapshot_ignore()
    assert adapter.cache_dirs() == ("build",)



# ---------------------------------------------------------------------------
# result parsing (captured-sample)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "language, sample, exit_code, kind, outcome",
    [
        ("c", "junit_pass.xml", 0, "test", Outcome.PASS),
        ("c", "junit_fail.xml", 1, "test", Outcome.TEST_FAIL),
        ("c", "stub_abort.txt", 134, "test", Outcome.TEST_FAIL),
        ("c", "compile_error.txt", 2, "build", Outcome.BUILD_ERROR),
        ("c", "link_error.txt", 2, "build", Outcome.BUILD_ERROR),
        ("cpp", "junit_pass.xml", 0, "test", Outcome.PASS),
        ("cpp", "junit_fail.xml", 1, "test", Outcome.TEST_FAIL),
        ("cpp", "stub_throw.xml", 1, "test", Outcome.TEST_FAIL),
        ("cpp", "compile_error.txt", 2, "build", Outcome.BUILD_ERROR),
        ("cpp", "link_error.txt", 2, "build", Outcome.BUILD_ERROR),
    ],
)
def test_parse_result_matches_captured_samples(
    language: str, sample: str, exit_code: int, kind: str, outcome: Outcome
) -> None:
    """captured-sample: GCC 15 / CMake 4.2 / doctest 2.4 JUnit XML."""
    adapter = CAdapter() if language == "c" else CppAdapter()
    result = adapter.parse_result(_sample(language, sample), exit_code, kind)
    assert result.outcome is outcome
    assert result.raw_output


def test_cases_come_from_junit_xml() -> None:
    result = CAdapter().parse_result(_sample("c", "junit_pass.xml"), 0, "test")
    assert [(case.name, case.outcome) for case in result.tests] == [
        ("R1_adds_two_numbers", "passed")
    ]
    failed = CAdapter().parse_result(_sample("c", "junit_fail.xml"), 1, "test")
    assert failed.tests[0].message == "adder_add(2, 3) == 6 (got 5, want 6)"
    cpp_failed = CppAdapter().parse_result(_sample("cpp", "junit_fail.xml"), 1, "test")
    assert cpp_failed.tests[0].message == "5 == 6"


def test_c_stub_abort_is_a_valid_pre_implementation_failure() -> None:
    adapter = CAdapter()
    result = adapter.parse_result(_sample("c", "stub_abort.txt"), 134, "test")
    assert result.marker_present is True
    assert adapter.stub_failure_is_valid(result) is True


def test_cpp_stub_throw_is_a_valid_pre_implementation_failure() -> None:
    adapter = CppAdapter()
    result = adapter.parse_result(_sample("cpp", "stub_throw.xml"), 1, "test")
    assert result.marker_present is True
    assert adapter.stub_failure_is_valid(result) is True


def test_compile_and_link_errors_carry_diagnostics() -> None:
    compile_result = CAdapter().parse_result(_sample("c", "compile_error.txt"), 2, "build")
    assert any("error:" in message for message in compile_result.failure_messages)
    link_result = CAdapter().parse_result(_sample("c", "link_error.txt"), 2, "build")
    assert any("undefined reference" in message for message in link_result.failure_messages)
    assert CAdapter().stub_failure_is_valid(link_result) is False


def test_zero_tests_is_not_a_pass(adapter) -> None:
    result = adapter.parse_result('<testsuite name="x" tests="0" failures="0">', 0, "test")
    assert result.outcome is Outcome.NO_TESTS
    assert adapter.stub_failure_is_valid(result) is False


def test_an_integration_build_failure_names_the_missing_header(adapter) -> None:
    output = "fatal error: root_integration/root_integration.h: No such file or directory"
    result = adapter.parse_result(output, 2, "build")
    assert result.outcome is Outcome.BUILD_ERROR
    assert adapter.stub_failure_is_valid(
        result, integration=True, expected_module="root_integration"
    )
    assert not adapter.stub_failure_is_valid(result, integration=True, expected_module="other")


def test_timeout_short_circuits(adapter) -> None:
    result = adapter.parse_result("anything", 124, "test", timed_out=True)
    assert result.outcome is Outcome.TIMEOUT and result.timed_out is True


def test_extract_test_functions_and_traceability(adapter) -> None:
    if adapter.id == "c":
        source = (
            '#include "nai_assert.h"\n'
            "int main(void) {\n"
            '    nai_begin("text_normalizer");\n'
            '    nai_case("R1_normalize_text");\n'
            '    nai_case("R2_trims");\n'
            "    return nai_finish();\n"
            "}\n"
        )
    else:
        source = (
            '#include "doctest.h"\n'
            'TEST_CASE("R1_normalize_text") { CHECK(1 == 1); }\n'
            'TEST_CASE("R2_trims") { CHECK(1 == 1); }\n'
        )
    functions, errors = adapter.extract_test_functions({"tests/x/x_test.c": source})
    assert functions == ["R1_normalize_text", "R2_trims"]
    assert errors == []

    from nai.build.traceability import check_traceability

    trace = check_traceability(
        [{"id": "R1", "text": "normalize"}, {"id": "R3", "text": "missing"}],
        {"tests/x/x_test.c": source},
        extractor=adapter.extract_test_functions,
    )
    assert trace.missing == ["R3"]
    assert trace.covered["R1"] == ["R1_normalize_text"]



# ---------------------------------------------------------------------------
# platform lint, syntax check, commands
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("framework", ["Cocoa/Cocoa.h", "AppKit/AppKit.h", "SwiftUI/SwiftUI.h"])
def test_portable_components_may_not_include_apple_frameworks(
    tmp_path: Path, adapter, framework: str
) -> None:
    project, node = _project(), _node("text-normalizer")
    ext = ".c" if adapter.id == "c" else ".cpp"
    path = tmp_path / "src" / "text_normalizer" / f"text_normalizer{ext}"
    path.parent.mkdir(parents=True)
    path.write_text(f"#include <{framework}>\n", encoding="utf-8")
    findings = adapter.platform_lint(project, node, tmp_path, platform="any")
    assert len(findings) == 1
    assert f"includes {framework}" in findings[0]
    assert adapter.platform_lint(project, node, tmp_path, platform="macos") == ()


def test_portable_components_may_include_standard_headers(
    tmp_path: Path, adapter
) -> None:
    project, node = _project(), _node("text-normalizer")
    ext = ".c" if adapter.id == "c" else ".cpp"
    path = tmp_path / "src" / "text_normalizer" / f"text_normalizer{ext}"
    path.parent.mkdir(parents=True)
    path.write_text("#include <string.h>\n", encoding="utf-8")
    assert adapter.platform_lint(project, node, tmp_path, platform="linux") == ()


def test_syntax_check_skips_preprocessor_lines(adapter) -> None:
    code = "#define OPEN_BRACE {\n\nint f(void) { return 0; }\n"
    assert adapter.check_syntax(code, "src/x/x.c") is None
    problem = adapter.check_syntax("int f(void) {\n", "src/x/x.c")
    assert problem and "unclosed" in problem


def test_commands_configure_build_and_run_the_test_binary(adapter, tmp_path: Path) -> None:
    project, node = _project(), _node("text-normalizer")
    setup = adapter.setup_commands(project, tmp_path)[0]
    assert setup.argv[:5] == ("cmake", "-S", ".", "-B", "build")
    assert "-G" in setup.argv
    assert setup.network is False
    assert adapter.build_commands(project, tmp_path)[0].argv == ("cmake", "--build", "build")
    test_cmd = adapter.test_command(project, node)
    assert test_cmd.argv[:5] == ("ctest", "--build-and-test", ".", "build", "--build-generator")
    assert test_cmd.argv[-1] == ("./text_normalizer_test" if adapter.id == "c" else "--reporters=junit")
    assert adapter.needs_target_env() is False
    assert adapter.env_overrides(project, tmp_path)["CTEST_OUTPUT_ON_FAILURE"] == "1"


def test_lint_command_requires_clang_format(adapter, tmp_path: Path, monkeypatch) -> None:
    project = _project()
    monkeypatch.setattr("nai.langs._c_family.shutil.which", lambda name: None)
    assert adapter.lint_command(project, tmp_path, ("src/x/x.c",)) is None
    monkeypatch.setattr("nai.langs._c_family.shutil.which", lambda name: "/usr/bin/clang-format")
    command = adapter.lint_command(project, tmp_path, ("src/x/x.c",))
    assert command.argv == ("clang-format", "--dry-run", "--Werror", "src/x/x.c")


# ---------------------------------------------------------------------------
# remote recipes
# ---------------------------------------------------------------------------


def test_remote_recipes_use_cmake_and_ctest(adapter) -> None:
    project, node = _project(), _node("text-normalizer")
    assert adapter.remote_recipe(project, node, "verify") == "ctest"
    assert adapter.remote_recipe(project, node, "author_tests") == "ctest"
    assert adapter.remote_recipe(project, node, "build") == "cmake-build"
    assert adapter.remote_params(project, node, "verify") == {"filter": "text_normalizer_test"}
    assert adapter.remote_params(project, node, "build") == {}


def test_remote_params_are_accepted_by_the_recipe_registry(adapter) -> None:
    from nai.remote.recipes import spec_for, validate_params

    node = _node("text-normalizer")
    for job_kind in ("verify", "author_tests", "build"):
        recipe = adapter.remote_recipe(_project(), node, job_kind)
        params = adapter.remote_params(_project(), node, job_kind)
        validate_params(spec_for(recipe), params)  # raises when the worker would reject it


def test_environment_block_describes_cmake(adapter, tmp_path: Path) -> None:
    block = adapter.environment_block(_project(), tmp_path)
    assert "CMake >= 3.21" in block
    assert "CTest" in block
    assert "include/<component>/" in block


# ---------------------------------------------------------------------------
# registration and support
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("language_id", ["c", "cpp"])
def test_c_family_is_registered_and_supported(language_id: str) -> None:
    assert registry.get(language_id).id == language_id
    status = support.support_status(language_id, fixture_root=LANG_FIXTURES)
    assert status.adapter and status.knowledge and status.probe_defined
    assert status.e2e_fixture is True
    assert support.is_supported(language_id)
    assert language_id in support.supported_languages()


@pytest.mark.parametrize(
    "language_id, expected",
    [("c", ("C11", "CTest", "NOT_IMPLEMENTED")), ("cpp", ("C++17", "doctest", "logic_error"))],
)
def test_c_family_packs_cover_the_required_guidance(language_id: str, expected: tuple[str, ...]) -> None:
    assert knowledge.has_pack(language_id), knowledge.missing_sections(language_id)
    spec = knowledge.load_section(language_id, "spec")
    test = knowledge.load_section(language_id, "test")
    stub = knowledge.load_section(language_id, "stub")
    assert expected[0] in spec
    assert expected[1] in test
    assert expected[2] in stub
    assert "error" in knowledge.load_section(language_id, "compile_errors").lower()

