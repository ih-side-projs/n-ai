"""TypeScript adapter: naming, stubs, vitest parsing, platform lint and recipes.

The vitest/tsc output samples under ``tests/langs/fixtures/typescript`` are
hand-captured (see their README); these tests pin the formats the adapter
depends on and are labelled ``captured-sample``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nai.langs import knowledge, registry, support
from nai.langs.base import Outcome
from nai.langs.typescript import (
    TypeScriptAdapter,
    render_entities,
    render_package_json,
    render_tsconfig,
    ts_package_name,
    ts_type,
)

SAMPLES = Path(__file__).resolve().parent / "fixtures" / "typescript"
LANG_FIXTURES = Path(__file__).resolve().parents[1] / "e2e" / "fixtures" / "langs"


def _sample(name: str) -> str:
    return (SAMPLES / name).read_text(encoding="utf-8")


@pytest.fixture
def adapter() -> TypeScriptAdapter:
    return TypeScriptAdapter()


def _project(**settings) -> dict:
    return {"name": "Tiny Library", "settings": {"package_name": "tiny-lib", **settings}}


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
# naming and types
# ---------------------------------------------------------------------------


def test_package_names_are_kebab_case() -> None:
    assert ts_package_name(_project()) == "tiny-lib"
    assert ts_package_name({"name": "my app", "settings": {}}) == "my-app"
    assert ts_package_name({"name": "x", "settings": {"package_name": "2fa"}}) == "app-2fa"
    assert ts_package_name({}) == "app"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("str", "string"),
        ("int", "number"),
        ("float", "number"),
        ("bool", "boolean"),
        ("dict", "Record<string, string>"),
        ("list[str]", "string[]"),
        ("Optional[str]", "string | undefined"),
        ("str?", "string | undefined"),
        ("void", "void"),
        ("", "void"),
        ("Input", "Input"),
        ("mystery type", "string"),
    ],
)
def test_contract_types_map_to_typescript(raw: str, expected: str) -> None:
    assert ts_type(raw, ("Input",)) == expected


def test_entities_render_as_interfaces() -> None:
    rendered = render_entities(CONTRACT["entities"])
    assert "export interface Input {" in rendered
    assert "  text: string;" in rendered
    assert "  note?: string;" in rendered
    assert "/** Text input */" in rendered
    assert rendered == render_entities(CONTRACT["entities"])  # deterministic


def test_manifests_pin_the_toolchain() -> None:
    package = json.loads(render_package_json(_project()))
    assert package["name"] == "tiny-lib"
    assert package["type"] == "module"
    assert package["scripts"] == {"build": "tsc --noEmit", "test": "vitest run --reporter=json"}
    assert set(package["devDependencies"]) == {"typescript", "vitest"}

    config = json.loads(render_tsconfig())
    assert config["compilerOptions"]["strict"] is True
    assert config["compilerOptions"]["noEmit"] is True
    assert config["include"] == ["src/**/*.ts", "tests/**/*.ts"]
    assert render_tsconfig() == render_tsconfig()


# ---------------------------------------------------------------------------
# layout and stubs
# ---------------------------------------------------------------------------


def test_layout_uses_esm_paths(adapter: TypeScriptAdapter) -> None:
    project, node = _project(), _node("text-normalizer")
    assert adapter.source_rel(project, node) == "src/text-normalizer/index.ts"
    assert adapter.test_rel(project, node) == "tests/text-normalizer.test.ts"
    assert adapter.entities_rel(project) == "src/entities.ts"
    assert adapter.module_import(project, node) == "text-normalizer"
    assert adapter.package_name(project) == "tiny-lib"


def test_normalize_test_path_forces_the_node_spec(adapter: TypeScriptAdapter) -> None:
    node = _node("text-normalizer")
    assert adapter.normalize_test_path("test_normalize.ts", node) == "tests/test_normalize.test.ts"
    assert adapter.normalize_test_path("tests/Other/Weird Name.ts", node) == "tests/Weird-Name.test.ts"
    assert adapter.normalize_test_path("", node) == "tests/text-normalizer.test.ts"


def test_stub_renders_signatures_and_entities(adapter: TypeScriptAdapter) -> None:
    project, node = _project(), _node("text-normalizer")
    stub = adapter.render_stub(project, node, CONTRACT)
    assert [f.rel_path for f in stub.files] == ["src/entities.ts", "src/text-normalizer/index.ts"]
    assert stub.module_import == "text-normalizer"
    source = stub.files[1].content
    assert 'import type { Input } from "../entities.js";' in source
    assert "/** Collapse whitespace. */" in source
    assert "export function normalizeText(text: string): string {" in source
    assert 'throw new Error("not implemented");' in source
    assert "export interface Input {" in stub.files[0].content


def test_stub_maps_the_contract_return_type(adapter: TypeScriptAdapter) -> None:
    source = adapter.render_stub(_project(), _node("word-counter"), CONTRACT).files[-1].content
    assert "export function countWords(text: string): number {" in source


def test_integration_stub_exposes_the_conventional_entry_point(adapter: TypeScriptAdapter) -> None:
    stub = adapter.render_stub(_project(), _node("root-integration", "integration"), CONTRACT)
    source = stub.files[-1].content
    assert "export function integrate(): number {" in source
    assert 'throw new Error("not implemented");' in source


def test_extract_signatures_keeps_declarations_only(adapter: TypeScriptAdapter) -> None:
    source = (
        "// comment\n"
        'import type { Input } from "../entities.js";\n'
        "export function normalizeText(text: string): string {\n"
        "  return text;\n"
        "}\n"
        "export interface Input {\n"
        "  text: string;\n"
        "}\n"
    )
    assert adapter.extract_signatures(source) == (
        "export function normalizeText(text: string): string\n"
        "export interface Input"
    )



# ---------------------------------------------------------------------------
# result parsing (captured-sample)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sample, exit_code, kind, outcome",
    [
        ("tsc_build_error.txt", 2, "build", Outcome.BUILD_ERROR),
        ("vitest_pass.json", 0, "test", Outcome.PASS),
        ("vitest_fail.json", 1, "test", Outcome.TEST_FAIL),
        ("vitest_stub_crash.json", 1, "test", Outcome.TEST_FAIL),
        ("zero_tests.json", 0, "test", Outcome.NO_TESTS),
    ],
)
def test_parse_result_matches_captured_samples(
    adapter: TypeScriptAdapter, sample: str, exit_code: int, kind: str, outcome: Outcome
) -> None:
    """captured-sample: vitest 2 JSON reporter and tsc 5 diagnostics."""
    result = adapter.parse_result(_sample(sample), exit_code, kind)
    assert result.outcome is outcome
    assert result.raw_output


def test_build_errors_carry_the_tsc_diagnostics(adapter: TypeScriptAdapter) -> None:
    result = adapter.parse_result(_sample("tsc_build_error.txt"), 2, "build")
    assert any("TS2322" in message for message in result.failure_messages)
    assert any("TS2304" in message for message in result.failure_messages)


def test_passing_tests_are_listed(adapter: TypeScriptAdapter) -> None:
    result = adapter.parse_result(_sample("vitest_pass.json"), 0, "test")
    assert [(case.name, case.outcome) for case in result.tests] == [
        ("normalizeText > R1_collapses whitespace", "passed")
    ]


def test_failing_tests_keep_the_assertion_message(adapter: TypeScriptAdapter) -> None:
    result = adapter.parse_result(_sample("vitest_fail.json"), 1, "test")
    assert [case.outcome for case in result.tests] == ["failed"]
    assert "expected 3 to be 2" in result.tests[0].message


def test_stub_crash_is_a_valid_pre_implementation_failure(adapter: TypeScriptAdapter) -> None:
    result = adapter.parse_result(_sample("vitest_stub_crash.json"), 1, "test")
    assert result.marker_present is True
    assert adapter.stub_failure_is_valid(result) is True


def test_a_build_failure_is_never_a_valid_stub_failure(adapter: TypeScriptAdapter) -> None:
    result = adapter.parse_result(_sample("tsc_build_error.txt"), 2, "test")
    assert result.outcome is Outcome.BUILD_ERROR
    assert adapter.stub_failure_is_valid(result) is False


def test_zero_tests_is_not_a_pass(adapter: TypeScriptAdapter) -> None:
    result = adapter.parse_result(_sample("zero_tests.json"), 0, "test")
    assert result.outcome is Outcome.NO_TESTS
    assert adapter.stub_failure_is_valid(result) is False


def test_an_integration_build_failure_names_the_missing_module(adapter: TypeScriptAdapter) -> None:
    output = "src/root-integration/index.ts(1,25): error TS2307: Cannot find module './root-integration'."
    result = adapter.parse_result(output, 2, "build")
    assert result.outcome is Outcome.BUILD_ERROR
    assert adapter.stub_failure_is_valid(result, integration=True, expected_module="root-integration")
    assert not adapter.stub_failure_is_valid(result, integration=True, expected_module="other")


def test_timeout_short_circuits(adapter: TypeScriptAdapter) -> None:
    result = adapter.parse_result("anything", 124, "test", timed_out=True)
    assert result.outcome is Outcome.TIMEOUT and result.timed_out is True


def test_extract_test_functions_and_traceability(adapter: TypeScriptAdapter) -> None:
    source = (
        'import { describe, expect, it } from "vitest";\n'
        'describe("normalizeText", () => {\n'
        '  it("R1_collapses whitespace", () => { expect(1).toBe(1); });\n'
        '  test("R2_trims", () => { expect(1).toBe(1); });\n'
        "});\n"
    )
    functions, errors = adapter.extract_test_functions({"tests/x.test.ts": source})
    assert functions == ["R1_collapses whitespace", "R2_trims"]
    assert errors == []

    from nai.build.traceability import check_traceability

    trace = check_traceability(
        [{"id": "R1", "text": "normalize"}, {"id": "R3", "text": "missing"}],
        {"tests/x.test.ts": source},
        extractor=adapter.extract_test_functions,
    )
    assert trace.missing == ["R3"]
    assert trace.covered["R1"] == ["R1_collapses whitespace"]


# ---------------------------------------------------------------------------
# platform lint, syntax check, commands
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module", ["react-native", "react-dom", "expo", "@react-native-community/netinfo"]
)
def test_portable_components_may_not_import_ui_frameworks(
    tmp_path: Path, adapter: TypeScriptAdapter, module: str
) -> None:
    project, node = _project(), _node("text-normalizer")
    path = tmp_path / "src" / "text-normalizer" / "index.ts"
    path.parent.mkdir(parents=True)
    path.write_text(f'import {{ View }} from "{module}";\n\nexport const x = 1;\n', encoding="utf-8")
    findings = adapter.platform_lint(project, node, tmp_path, platform="any")
    assert len(findings) == 1
    assert f"imports {module}" in findings[0]
    assert "src/text-normalizer/index.ts" in findings[0]
    assert adapter.platform_lint(project, node, tmp_path, platform="macos") == ()


def test_portable_components_may_import_node_builtins(
    tmp_path: Path, adapter: TypeScriptAdapter
) -> None:
    project, node = _project(), _node("text-normalizer")
    path = tmp_path / "src" / "text-normalizer" / "index.ts"
    path.parent.mkdir(parents=True)
    path.write_text('import { readFile } from "node:fs/promises";\n', encoding="utf-8")
    assert adapter.platform_lint(project, node, tmp_path, platform="linux") == ()


def test_syntax_check_balances_brackets(adapter: TypeScriptAdapter) -> None:
    assert adapter.check_syntax("export function f() { return 1; }\n", "src/x/index.ts") is None
    problem = adapter.check_syntax("export function f() {\n", "src/x/index.ts")
    assert problem and "unclosed" in problem


def test_commands_use_npm_and_vitest(adapter: TypeScriptAdapter, tmp_path: Path) -> None:
    project, node = _project(), _node("text-normalizer")
    assert adapter.setup_commands(project, tmp_path)[0].argv == (
        "npm", "install", "--no-audit", "--no-fund",
    )
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    assert adapter.setup_commands(project, tmp_path)[0].argv == (
        "npm", "ci", "--no-audit", "--no-fund",
    )
    assert adapter.build_commands(project, tmp_path)[0].argv == (
        "npx", "--no-install", "tsc", "--noEmit",
    )
    test_cmd = adapter.test_command(project, node)
    assert test_cmd.argv == (
        "npx", "--no-install", "vitest", "run", "--reporter=json", "--no-color",
        "tests/text-normalizer.test.ts",
    )
    assert test_cmd.network is False
    assert adapter.lint_command(project, tmp_path, ("src/x/index.ts",)) is None
    assert adapter.needs_target_env() is False
    assert adapter.env_overrides(project, tmp_path) == {"CI": "1", "NO_COLOR": "1"}


def test_prepare_workspace_writes_once_then_reports_no_change(
    tmp_path: Path, adapter: TypeScriptAdapter
) -> None:
    project = _project()
    assert adapter.prepare_workspace(project, tmp_path) == ("package.json", "tsconfig.json")
    assert (tmp_path / "package.json").is_file()
    assert adapter.prepare_workspace(project, tmp_path) == ()


def test_protected_and_writable_globs(adapter: TypeScriptAdapter) -> None:
    project, node = _project(), _node("text-normalizer")
    protected = adapter.protected_globs(project)
    for pattern in ("tests/**", "package.json", "package-lock.json", "tsconfig.json", "src/entities.ts"):
        assert pattern in protected
    assert adapter.writable_globs(project, node) == (
        "src/text-normalizer/index.ts",
        "src/text-normalizer/**",
    )
    assert "node_modules" in adapter.snapshot_ignore()
    assert adapter.cache_dirs() == ("node_modules", "dist", "coverage", ".vite")





def test_validate_stub_installs_in_the_setup_phase(
    tmp_path: Path, adapter: TypeScriptAdapter
) -> None:
    """npm needs the network, so the install runs in the setup phase."""
    calls: list[tuple[str, str]] = []

    def _run(cmd: str) -> tuple[int, str]:
        calls.append(("run", cmd))
        return 0, ""

    def _setup(cmd: str) -> tuple[int, str]:
        calls.append(("setup", cmd))
        return 0, ""

    ok, _ = adapter.validate_stub(tmp_path, "text-normalizer", _run, run_setup_cmd=_setup)
    assert ok is True
    assert calls == [
        ("setup", "npm install --no-audit --no-fund"),
        ("run", "npx --no-install tsc --noEmit"),
    ]

    (tmp_path / "node_modules").mkdir()
    calls.clear()
    ok, _ = adapter.validate_stub(tmp_path, "text-normalizer", _run, run_setup_cmd=_setup)
    assert ok is True
    assert calls == [("run", "npx --no-install tsc --noEmit")]


def test_validate_stub_prefers_npm_ci_with_a_lockfile(
    tmp_path: Path, adapter: TypeScriptAdapter
) -> None:
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    seen: list[str] = []

    def _ok(cmd: str) -> tuple[int, str]:
        seen.append(cmd)
        return 0, ""

    ok, _ = adapter.validate_stub(
        tmp_path, "text-normalizer", _ok, run_setup_cmd=_ok
    )
    assert ok is True
    assert seen[0] == "npm ci --no-audit --no-fund"


def test_validate_stub_reports_an_install_failure(
    tmp_path: Path, adapter: TypeScriptAdapter
) -> None:
    ok, output = adapter.validate_stub(
        tmp_path,
        "text-normalizer",
        lambda cmd: (0, ""),
        run_setup_cmd=lambda cmd: (1, "npm ERR! network"),
    )
    assert ok is False
    assert "npm ERR!" in output


# ---------------------------------------------------------------------------
# remote recipes
# ---------------------------------------------------------------------------


def test_remote_recipes_use_npm(adapter: TypeScriptAdapter) -> None:
    project, node = _project(), _node("text-normalizer")
    assert adapter.remote_recipe(project, node, "verify") == "npm-test"
    assert adapter.remote_recipe(project, node, "author_tests") == "npm-test"
    assert adapter.remote_recipe(project, node, "build") == "npm-build"
    assert adapter.remote_params(project, node, "verify") == {
        "filter": "tests/text-normalizer.test.ts"
    }
    assert adapter.remote_params(project, node, "build") == {}


def test_remote_params_are_accepted_by_the_recipe_registry(adapter: TypeScriptAdapter) -> None:
    from nai.remote.recipes import spec_for, validate_params

    node = _node("text-normalizer")
    for job_kind in ("verify", "author_tests", "build"):
        recipe = adapter.remote_recipe(_project(), node, job_kind)
        params = adapter.remote_params(_project(), node, job_kind)
        validate_params(spec_for(recipe), params)  # raises when the worker would reject it


def test_environment_block_describes_the_pinned_toolchain(
    adapter: TypeScriptAdapter, tmp_path: Path
) -> None:
    block = adapter.environment_block(_project(), tmp_path)
    assert "Node 20" in block
    assert "vitest" in block
    assert "ESM" in block


# ---------------------------------------------------------------------------
# registration and support
# ---------------------------------------------------------------------------


def test_typescript_is_registered_and_supported() -> None:
    assert registry.get("typescript").id == "typescript"
    status = support.support_status("typescript", fixture_root=LANG_FIXTURES)
    assert status.adapter and status.knowledge and status.probe_defined
    assert status.e2e_fixture is True
    assert support.is_supported("typescript")
    assert "typescript" in support.supported_languages()


def test_typescript_pack_covers_the_required_guidance() -> None:
    assert knowledge.has_pack("typescript"), knowledge.missing_sections("typescript")
    spec = knowledge.load_section("typescript", "spec")
    assert "Node 20" in spec or "Node.js 20" in spec
    assert "vitest" in knowledge.load_section("typescript", "test")
    assert "not implemented" in knowledge.load_section("typescript", "stub")
    assert "error" in knowledge.load_section("typescript", "compile_errors").lower()
    pitfalls = knowledge.load_section("typescript", "pitfalls").lower()
    assert "strict" in pitfalls or "any" in pitfalls

