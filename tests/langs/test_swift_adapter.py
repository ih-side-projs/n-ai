"""Swift adapter: naming, stubs, Package.swift, parsing, platform lint and recipes.

The XCTest/SwiftPM output samples under ``tests/langs/fixtures/swift`` are
hand-captured (see their README); these tests pin the formats the adapter
depends on and are labelled ``captured-sample``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nai.langs import knowledge, registry, support
from nai.langs.base import Outcome
from nai.langs.swift import (
    SwiftAdapter,
    render_entities,
    render_package_swift,
    swift_identifier,
    swift_member,
    swift_package_name,
    swift_type,
)

SAMPLES = Path(__file__).resolve().parent / "fixtures" / "swift"


def _sample(name: str) -> str:
    return (SAMPLES / name).read_text(encoding="utf-8")


@pytest.fixture
def adapter() -> SwiftAdapter:
    return SwiftAdapter()


def _project(**settings) -> dict:
    base = {"name": "Tiny Library", "settings": {"package_name": "TinyLibrary", **settings}}
    return base


CONTRACT = {
    "entities": [
        {"name": "Input", "description": "Text input", "fields": [
            {"name": "text", "type": "str"},
            {"name": "note", "type": "str", "required": False},
        ]},
    ],
    "components": [
        {"name": "text-normalizer", "provides": [{
            "name": "normalize_text",
            "signature": "normalize_text(text: str) -> str",
            "description": "Collapse whitespace.",
        }], "consumes": []},
        {"name": "word-counter", "provides": [{
            "name": "count_words",
            "signature": "count_words(text: str) -> int",
        }], "depends_on": ["text-normalizer"]},
    ],
    "scenarios": [{"id": "S1", "title": "Normalize then count", "steps": []}],
}


def _node(name: str, kind: str = "component", parent_id: str | None = "root") -> dict:
    return {"id": f"id-{name}", "name": name, "kind": kind, "parent_id": parent_id, "contract": {}}


# ---------------------------------------------------------------------------
# naming and types
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [("text-normalizer", "TextNormalizer"), ("word_counter", "WordCounter"),
     ("root-integration", "RootIntegration"), ("2fa", "T2fa"), ("", "Component")],
)
def test_identifiers_are_pascal_case(raw: str, expected: str) -> None:
    assert swift_identifier(raw) == expected


def test_members_are_camel_case() -> None:
    assert swift_member("normalize_text") == "normalizeText"
    assert swift_member("word-counter") == "wordCounter"


def test_package_and_core_names() -> None:
    assert swift_package_name(_project()) == "TinyLibrary"
    assert swift_package_name({"name": "my-app", "settings": {}}) == "MyApp"
    assert swift_package_name({"name": "x", "settings": {"package_name": "custom-lib"}}) == "CustomLib"


@pytest.mark.parametrize(
    "raw, expected",
    [("str", "String"), ("int", "Int"), ("bool", "Bool"), ("float", "Double"),
     ("list[str]", "[String]"), ("[int]", "[Int]"), ("dict", "[String: String]"),
     ("Input", "Input"), ("str?", "String?"), ("void", "Void"), ("", "Void"),
     ("mystery type", "String")],
)
def test_contract_types_map_to_swift(raw: str, expected: str) -> None:
    assert swift_type(raw, ("Input",)) == expected


# ---------------------------------------------------------------------------
# stubs and entities
# ---------------------------------------------------------------------------


def test_layout_uses_swiftpm_paths(adapter: SwiftAdapter) -> None:
    project, node = _project(), _node("text-normalizer")
    assert adapter.source_rel(project, node) == "Sources/TextNormalizer/TextNormalizer.swift"
    assert adapter.test_rel(project, node) == "Tests/TextNormalizerTests/TextNormalizerTests.swift"
    assert adapter.entities_rel(project) == "Sources/TinyLibraryCore/Entities.swift"
    assert adapter.module_import(project, node) == "TextNormalizer"
    assert adapter.package_name(project) == "TinyLibrary"


def test_stub_renders_signatures_and_entities(adapter: SwiftAdapter) -> None:
    project, node = _project(), _node("text-normalizer")
    stub = adapter.render_stub(project, node, CONTRACT)
    paths = [f.rel_path for f in stub.files]
    assert paths == ["Sources/TinyLibraryCore/Entities.swift", "Sources/TextNormalizer/TextNormalizer.swift"]
    assert stub.module_import == "TextNormalizer"
    source = stub.files[1].content
    assert "import TinyLibraryCore" in source
    assert "/// Collapse whitespace." in source
    assert 'public func normalizeText(_ text: String) -> String {' in source
    assert 'fatalError("not implemented")' in source
    entities = stub.files[0].content
    assert "public struct Input: Equatable {" in entities
    assert "public let text: String" in entities
    assert "public let note: String?" in entities
    assert "public init(text: String, note: String? = nil) {" in entities


def test_stub_maps_unknown_parameter_types(adapter: SwiftAdapter) -> None:
    contract = {"components": [{"name": "word-counter", "provides": [{
        "name": "count_words", "params": [{"name": "text", "type": "int"}], "returns": "bool",
    }]}]}
    source = adapter.render_stub(_project(), _node("word-counter"), contract).files[-1].content
    assert "public func countWords(_ text: Int) -> Bool {" in source


def test_integration_stub_exposes_the_conventional_entry_point(adapter: SwiftAdapter) -> None:
    project = _project()
    stub = adapter.render_stub(project, _node("root-integration", "integration"), CONTRACT)
    source = stub.files[-1].content
    assert "public func integrate() -> Int {" in source
    assert 'fatalError("not implemented")' in source


def test_entities_are_deterministic() -> None:
    first = render_entities(CONTRACT["entities"])
    second = render_entities(CONTRACT["entities"])
    assert first == second
    assert "import Foundation" in first


def test_extract_signatures_keeps_declarations_only(adapter: SwiftAdapter) -> None:
    source = (
        "import Foundation\n"
        "/// Doc\n"
        "public func normalizeText(_ text: String) -> String {\n"
        "    return text\n"
        "}\n"
        "public struct Input: Equatable {}\n"
    )
    assert adapter.extract_signatures(source) == (
        "public func normalizeText(_ text: String) -> String\n"
        "public struct Input: Equatable {}"
    )



# ---------------------------------------------------------------------------
# Package.swift
# ---------------------------------------------------------------------------


def _workspace(tmp_path: Path, *, sources=(), tests=()) -> Path:
    for target in sources:
        (tmp_path / "Sources" / target).mkdir(parents=True, exist_ok=True)
        (tmp_path / "Sources" / target / f"{target}.swift").write_text("// stub\n", encoding="utf-8")
    for target in tests:
        (tmp_path / "Tests" / f"{target}Tests").mkdir(parents=True, exist_ok=True)
        (tmp_path / "Tests" / f"{target}Tests" / f"{target}Tests.swift").write_text("// test\n", encoding="utf-8")
    return tmp_path


def test_package_manifest_declares_existing_targets_only(tmp_path: Path) -> None:
    project = _project()
    root = _workspace(tmp_path, sources=("TinyLibraryCore", "TextNormalizer"), tests=("TextNormalizer",))
    manifest = render_package_swift(project, root)
    assert manifest.startswith("// swift-tools-version: 5.9\n")
    assert 'name: "TinyLibrary"' in manifest
    assert '.target(name: "TextNormalizer", dependencies: ["TinyLibraryCore"]),' in manifest
    assert '.testTarget(name: "TextNormalizerTests", dependencies: ["TextNormalizer"]),' in manifest
    # The word-counter target does not exist on disk yet, so it is not declared.
    assert "WordCounter" not in manifest
    assert manifest == render_package_swift(project, root)  # deterministic


def test_package_manifest_links_integration_targets_to_components(tmp_path: Path) -> None:
    project = _project()
    root = _workspace(
        tmp_path,
        sources=("TinyLibraryCore", "TextNormalizer", "WordCounter", "RootIntegration"),
        tests=("RootIntegration",),
    )
    manifest = render_package_swift(project, root)
    assert (
        '.target(name: "RootIntegration", dependencies: ["TextNormalizer", "TinyLibraryCore", "WordCounter"]),'
        in manifest
    )
    assert (
        '.testTarget(name: "RootIntegrationTests", dependencies: '
        '["RootIntegration", "TextNormalizer", "TinyLibraryCore", "WordCounter"]),'
        in manifest
    )


def test_package_manifest_is_valid_without_targets(tmp_path: Path) -> None:
    manifest = render_package_swift(_project(), tmp_path)
    assert "targets: []" in manifest


def test_prepare_workspace_writes_once_then_reports_no_change(tmp_path: Path, adapter: SwiftAdapter) -> None:
    project = _project()
    _workspace(tmp_path, sources=("TinyLibraryCore", "TextNormalizer"))
    assert adapter.prepare_workspace(project, tmp_path) == ("Package.swift",)
    written = (tmp_path / "Package.swift").read_text(encoding="utf-8")
    assert 'name: "TextNormalizer"' in written
    assert adapter.prepare_workspace(project, tmp_path) == ()


def test_prepare_workspace_never_touches_external(tmp_path: Path, adapter: SwiftAdapter) -> None:
    project = _project(swift={"xcode_project": "external/TinyLibrary.xcodeproj"})
    external = tmp_path / "external" / "TinyLibrary.xcodeproj"
    external.mkdir(parents=True)
    (external / "project.pbxproj").write_text("// owner supplied\n", encoding="utf-8")
    _workspace(tmp_path, sources=("TinyLibraryCore", "TextNormalizer"))
    adapter.prepare_workspace(project, tmp_path)
    assert (external / "project.pbxproj").read_text(encoding="utf-8") == "// owner supplied\n"
    assert "external/**" in adapter.protected_globs(project)
    assert "external" not in adapter.snapshot_ignore()



# ---------------------------------------------------------------------------
# result parsing (captured-sample)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sample, exit_code, kind, outcome",
    [
        ("build_error.txt", 1, "build", Outcome.BUILD_ERROR),
        ("stub_crash.txt", 132, "test", Outcome.TEST_FAIL),
        ("test_pass.txt", 0, "test", Outcome.PASS),
        ("test_fail.txt", 1, "test", Outcome.TEST_FAIL),
        ("zero_tests.txt", 0, "test", Outcome.NO_TESTS),
    ],
)
def test_parse_result_matches_captured_samples(
    adapter: SwiftAdapter, sample: str, exit_code: int, kind: str, outcome: Outcome
) -> None:
    """captured-sample: formats captured from Swift 6.0 / Xcode 16."""
    result = adapter.parse_result(_sample(sample), exit_code, kind)
    assert result.outcome is outcome
    assert result.raw_output


def test_build_errors_carry_the_diagnostic_lines(adapter: SwiftAdapter) -> None:
    result = adapter.parse_result(_sample("build_error.txt"), 1, "build")
    assert result.outcome is Outcome.BUILD_ERROR
    assert any("has no member 'join'" in message for message in result.failure_messages)


def test_stub_crash_marks_the_process_abort(adapter: SwiftAdapter) -> None:
    result = adapter.parse_result(_sample("stub_crash.txt"), 132, "test")
    assert result.marker_present is True
    assert result.tests == ()
    assert adapter.stub_failure_is_valid(result) is True


def test_a_build_failure_is_never_a_valid_stub_failure(adapter: SwiftAdapter) -> None:
    result = adapter.parse_result(_sample("build_error.txt"), 1, "test")
    assert result.outcome is Outcome.BUILD_ERROR
    assert adapter.stub_failure_is_valid(result) is False


def test_passing_tests_are_listed(adapter: SwiftAdapter) -> None:
    result = adapter.parse_result(_sample("test_pass.txt"), 0, "test")
    assert [(case.name, case.outcome) for case in result.tests] == [
        ("TextNormalizerTests.test_R1_normalizeText", "passed")
    ]


def test_failing_tests_keep_the_xctest_failure_message(adapter: SwiftAdapter) -> None:
    result = adapter.parse_result(_sample("test_fail.txt"), 1, "test")
    assert [case.outcome for case in result.tests] == ["failed"]
    assert any("XCTAssertEqual failed" in message for message in result.failure_messages)


def test_zero_tests_is_not_a_pass(adapter: SwiftAdapter) -> None:
    result = adapter.parse_result(_sample("zero_tests.txt"), 0, "test")
    assert result.outcome is Outcome.NO_TESTS
    assert adapter.stub_failure_is_valid(result) is False


def test_timeout_short_circuits(adapter: SwiftAdapter) -> None:
    result = adapter.parse_result("anything", 124, "test", timed_out=True)
    assert result.outcome is Outcome.TIMEOUT and result.timed_out is True


def test_a_stub_failure_without_the_marker_is_not_valid(adapter: SwiftAdapter) -> None:
    result = adapter.parse_result("Test Case 'X.test_R1_a' failed (0.1 seconds).", 1, "test")
    assert result.outcome is Outcome.TEST_FAIL
    assert adapter.stub_failure_is_valid(result) is False


def test_extract_test_functions_and_traceability(adapter: SwiftAdapter) -> None:
    source = (
        "import XCTest\n"
        "@testable import TextNormalizer\n\n"
        "final class TextNormalizerTests: XCTestCase {\n"
        "    func test_R1_normalizeText() throws {}\n"
        "    func test_R2_emptyInput() throws {}\n"
        "    func helper() {}\n"
        "}\n"
    )
    functions, errors = adapter.extract_test_functions({"Tests/TextNormalizerTests/TextNormalizerTests.swift": source})
    assert functions == ["test_R1_normalizeText", "test_R2_emptyInput"]
    assert errors == []

    from nai.build.traceability import check_traceability

    trace = check_traceability(
        [{"id": "R1", "text": "normalize"}, {"id": "R3", "text": "missing"}],
        {"Tests/TextNormalizerTests/TextNormalizerTests.swift": source},
        extractor=adapter.extract_test_functions,
    )
    assert trace.missing == ["R3"]
    assert trace.covered["R1"] == ["test_R1_normalizeText"]



# ---------------------------------------------------------------------------
# platform lint, syntax check, commands
# ---------------------------------------------------------------------------


def _source_file(root: Path, target: str, body: str) -> Path:
    path = root / "Sources" / target / f"{target}.swift"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


@pytest.mark.parametrize("module", ["SwiftUI", "UIKit", "AppKit"])
def test_portable_components_may_not_import_ui_frameworks(tmp_path: Path, adapter: SwiftAdapter, module: str) -> None:
    project, node = _project(), _node("text-normalizer")
    _source_file(tmp_path, "TextNormalizer", f"import {module}\n\npublic struct View {{}}\n")
    findings = adapter.platform_lint(project, node, tmp_path, platform="any")
    assert len(findings) == 1
    assert f"imports {module}" in findings[0]
    assert "Sources/TextNormalizer/TextNormalizer.swift:1" in findings[0]
    # A macOS-only component may use them.
    assert adapter.platform_lint(project, node, tmp_path, platform="macos") == ()


def test_portable_components_may_import_foundation(tmp_path: Path, adapter: SwiftAdapter) -> None:
    project, node = _project(), _node("text-normalizer")
    _source_file(tmp_path, "TextNormalizer", "import Foundation\n\npublic func f() {}\n")
    assert adapter.platform_lint(project, node, tmp_path, platform="linux") == ()


def test_syntax_check_is_a_no_op_without_swiftc(adapter: SwiftAdapter, monkeypatch) -> None:
    monkeypatch.setattr("nai.langs.swift.shutil.which", lambda name: None)
    assert adapter.check_syntax("public func f() {}", "Sources/X/X.swift") is None


def test_syntax_check_reports_swiftc_errors(adapter: SwiftAdapter, monkeypatch) -> None:
    from nai.exec.target import RunResult

    calls: list[list[str]] = []

    class FakeTarget:
        def __init__(self, root, **kwargs):
            self.root = root

        def run(self, argv, **kwargs):
            calls.append(list(argv))
            return RunResult(argv=tuple(argv), exit_code=1, output="Check.swift:1:1: error: expected declaration")

        def close(self):
            return None

    monkeypatch.setattr("nai.langs.swift.shutil.which", lambda name: "/usr/bin/swiftc")
    monkeypatch.setattr("nai.exec.LocalTarget", FakeTarget)
    problem = adapter.check_syntax("public func f() {", "Sources/X/X.swift")
    assert problem and "expected declaration" in problem
    assert calls and calls[0][0] == "/usr/bin/swiftc" and calls[0][1] == "-parse"


def test_commands_use_swiftpm(adapter: SwiftAdapter, tmp_path: Path) -> None:
    project, node = _project(), _node("text-normalizer")
    test_cmd = adapter.test_command(project, node)
    assert test_cmd.argv == ("swift", "test", "--filter", "TextNormalizerTests")
    assert test_cmd.network is False
    assert adapter.build_commands(project, tmp_path)[0].argv == ("swift", "build")
    assert adapter.setup_commands(project, tmp_path) == ()
    assert adapter.lint_command(project, tmp_path, ("Sources/X/X.swift",)) is None
    assert adapter.needs_target_env() is False
    assert adapter.env_overrides(project, tmp_path) == {}


def test_normalize_test_path_forces_the_node_target(adapter: SwiftAdapter) -> None:
    node = _node("text-normalizer")
    assert adapter.normalize_test_path("test_normalize.py", node) == (
        "Tests/TextNormalizerTests/test_normalize.swift"
    )
    assert adapter.normalize_test_path("Tests/Other/Weird Name.swift", node) == (
        "Tests/TextNormalizerTests/WeirdName.swift"
    )
    assert adapter.normalize_test_path("", node) == "Tests/TextNormalizerTests/TextNormalizerTests.swift"


def test_protected_and_writable_globs(adapter: SwiftAdapter) -> None:
    project, node = _project(), _node("text-normalizer")
    protected = adapter.protected_globs(project)
    for pattern in ("Tests/**", "Package.swift", "external/**", "Sources/TinyLibraryCore/Entities.swift"):
        assert pattern in protected
    assert adapter.writable_globs(project, node) == (
        "Sources/TextNormalizer/TextNormalizer.swift",
        "Sources/TextNormalizer/**",
    )
    ignored = adapter.snapshot_ignore()
    assert ".build" in ignored and ".swiftpm" in ignored and "DerivedData" in ignored
    assert adapter.cache_dirs() == (".build", ".swiftpm", "DerivedData")



# ---------------------------------------------------------------------------
# remote recipes
# ---------------------------------------------------------------------------


def test_remote_recipes_use_swiftpm_by_default(adapter: SwiftAdapter) -> None:
    project, node = _project(), _node("text-normalizer")
    assert adapter.remote_recipe(project, node, "verify") == "swift-test"
    assert adapter.remote_recipe(project, node, "author_tests") == "swift-test"
    assert adapter.remote_recipe(project, node, "build") == "swift-build"
    assert adapter.remote_params(project, node, "verify") == {"filter": "TextNormalizerTests"}
    assert adapter.remote_params(project, node, "build") == {}


def test_remote_recipe_uses_xcodebuild_only_with_an_owner_project(adapter: SwiftAdapter) -> None:
    project = _project(swift={"xcode_project": "TinyLibrary.xcodeproj", "xcode_scheme": "Tiny Library"})
    node = _node("text-normalizer")
    assert adapter.remote_recipe(project, node, "verify", platform="macos") == "xcodebuild-test"
    params = adapter.remote_params(project, node, "verify", platform="macos")
    assert params["xcode_project"] == "external/TinyLibrary.xcodeproj"
    assert params["scheme"] == "TinyLibrary"
    assert params["destination"] == "platform=macOS"
    # A portable host keeps SwiftPM even when an Xcode project exists.
    assert adapter.remote_recipe(project, node, "verify", platform="any") == "swift-test"


def test_xcode_project_and_destination_are_sanitised(adapter: SwiftAdapter) -> None:
    node = _node("text-normalizer")
    absolute = _project(swift={"xcode_project": "/Users/owner/App.xcodeproj"})
    assert adapter.remote_recipe(absolute, node, "verify", platform="macos") == "swift-test"
    escaping = _project(swift={"xcode_project": "../outside/App.xcodeproj"})
    assert adapter.remote_recipe(escaping, node, "verify", platform="macos") == "swift-test"
    allowed = _project(swift={"xcode_project": "App.xcodeproj", "xcode_destination": "platform=visionOS"})
    assert adapter.remote_params(allowed, node, "verify", platform="macos")["destination"] == "platform=macOS"


def test_remote_params_are_accepted_by_the_recipe_registry(adapter: SwiftAdapter) -> None:
    from nai.remote.recipes import spec_for, validate_params

    node = _node("text-normalizer")
    project = _project(swift={"xcode_project": "App.xcodeproj"})
    for job_kind, platform in (("verify", "any"), ("verify", "macos"), ("build", "any")):
        recipe = adapter.remote_recipe(project, node, job_kind, platform=platform)
        params = adapter.remote_params(project, node, job_kind, platform=platform)
        validate_params(spec_for(recipe), params)  # raises when the worker would reject it


def test_environment_block_describes_swiftpm(adapter: SwiftAdapter, tmp_path: Path) -> None:
    block = adapter.environment_block(_project(), tmp_path)
    assert "Swift 6 / SwiftPM" in block
    assert "XCTest" in block
    assert "external/" not in block
    with_xcode = adapter.environment_block(_project(swift={"xcode_project": "App.xcodeproj"}), tmp_path)
    assert "external/App.xcodeproj" in with_xcode


# ---------------------------------------------------------------------------
# registration and support
# ---------------------------------------------------------------------------


def test_swift_is_registered_and_supported() -> None:
    assert registry.get("swift").id == "swift"
    status = support.support_status("swift", fixture_root=Path(__file__).resolve().parents[1] / "e2e" / "fixtures" / "langs")
    assert status.adapter and status.knowledge and status.probe_defined
    assert status.e2e_fixture is True
    assert support.is_supported("swift")
    assert "swift" in support.supported_languages()


def test_swift_pack_covers_the_required_guidance() -> None:
    assert knowledge.has_pack("swift"), knowledge.missing_sections("swift")
    spec = knowledge.load_section("swift", "spec")
    impl = knowledge.load_section("swift", "impl")
    pitfalls = knowledge.load_section("swift", "pitfalls")
    assert "Swift 6" in spec and "SwiftPM" in spec
    assert "SwiftUI" in spec and "view model" in spec.lower()
    assert "fatalError" in knowledge.load_section("swift", "stub")
    assert "XCTest" in knowledge.load_section("swift", "test")
    assert "Sendable" in pitfalls or "actor" in pitfalls
    assert "error:" in knowledge.load_section("swift", "compile_errors")
    assert "portable" in impl.lower()

