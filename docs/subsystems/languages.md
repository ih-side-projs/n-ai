# Language adapters

`nai/langs/` is the only layer that knows a language's toolchain. The pipeline
asks an adapter for layout, stub rendering, commands, result parsing and path
policy instead of hard-coding Python assumptions.

## Modules

| Module | Responsibility |
|--------|----------------|
| `base.py` | `LanguageAdapter` protocol, `Command`, `Outcome`, `ParseResult`, `StubResult`, `ToolCheck` |
| `registry.py` | `register()`, `unregister()`, `get()`, `language_for()`, `supported()`, `UnknownLanguageError` |
| `python.py` | Python 3 adapter (pytest, venv/pip, ruff, `NotImplementedError` stubs) |
| `swift.py` | Swift 6 adapter (SwiftPM, XCTest, `fatalError` stubs) |
| `typescript.py` | TypeScript adapter (Node 20, npm, vitest, `tsc --noEmit`, `throw new Error` stubs) |
| `golang.py` | Go adapter (`go.mod`, `go build`/`go vet`, `go test -json`, `panic` stubs) |
| `rust.py` | Rust adapter (Cargo workspace, `--message-format=json`, libtest, `unimplemented!()` stubs) |
| `_c_family.py` | C and C++ adapters (CMake + CTest, generated `CMakeLists.txt`, vendored test harnesses) |
| `_common.py` | shared casing, contract traversal and structural syntax check for the compiled languages |
| `vendor/` | vendored single-header test frameworks (`nai_assert.h`, `nai_runtime.h`, `doctest.h`) |
| `support.py` | derived support status, supported list, unsupported-language detection, prompt block |
| `probe.py` | `probe(language, root=...)` runs the adapter's declared `--version` checks |
| `knowledge/` | per-language knowledge packs and the job-kind injection map |
| `../langs_cli.py` | `nai languages` |

## What "supported" means

Support is derived, never listed. `support.support_status(id)` reports four
criteria:

1. **adapter** — `registry.get(id)` resolves;
2. **knowledge** — all six pack sections exist, are non-empty and stay under the cap;
3. **probe** — the adapter declares `toolchain` (`tuple[ToolCheck, ...]`);
4. **e2e fixture** — `tests/e2e/fixtures/langs/<id>/project.json` exists and a
   CI test module runs it. This is a repository artifact, so it is reported as
   `None` in an installed wheel (no `tests/`) and never blocks runtime use.

`supported_languages()` returns the ids meeting criteria 1–3.
`tests/e2e/test_language_fixtures.py` enforces all four for every registered
adapter, and CI runs that file as its own "Language support matrix" step.

## Enforcement

Every entry point reads the same list:

| Entry point | Behaviour on an unsupported id |
|-------------|-------------------------------|
| `Store.create_project` / `update_project` | `StoreError("unsupported language …")`; a list value is rejected (one language per project) |
| `Store.import_project` | `ExportImportError` for a bundle whose settings name an unsupported language; older bundles get the default |
| `nai project create --language` | argparse `choices` → exit 2 |
| web `/projects/create` and `/projects/{id}/settings` | HTTP 400; the dropdowns list `supported_languages()` |
| contract schema | `language` enum generated from the registry (CI regenerates and diffs `schemas/`) |
| contract content | `contract.language` is normalised to the project language; a differing value emits `spec.contract_language_normalized` |
| spec prompts | `supported_languages_block()` appended to every structured call |
| clarify | an unsupported language named in the brief/guardrails becomes an explicit owner question; `settings.language` is never substituted |

`settings.language` holds the adapter id (`python`). `settings.target_env.language`
keeps the human stack string (`Python 3.12`) that prompts and the environment
block show.

## Selection

```python
from nai.langs import registry

adapter = registry.language_for(project)   # defaults to "python"
```

Migration `0007_project_language.sql` writes `settings.language = "python"` for
projects created before the key existed, and `Store.create_project` fills the
same default. An unknown language raises `registry.UnknownLanguageError` rather
than silently falling back.

## Interface

An adapter supplies:

- **identity** — `id`, `display_name`, `test_framework`, `extensions`, `stack_hint`,
  `style_guide`
- **toolchain** — `toolchain: tuple[ToolCheck, ...]` (name, argv, required, fallback names)
- **layout** — `source_rel`, `test_rel`, `entities_rel`, `normalize_test_path`,
  `check_syntax`
- **stubs** — `render_stub`, `validate_stub`, `extract_signatures`
- **commands** — `setup_commands`, `build_commands`, `test_command`,
  `lint_command`, `env_overrides` (argv lists, never shell strings)
- **environment** — `needs_target_env`, `prepare_workspace` (regenerate files the
  adapter owns), `environment_block` (the TENV-4 header), `extract_test_functions`
  (traceability), `platform_lint` (platform-restricted imports)
- **remote** — `remote_recipe`, `remote_params` (which worker recipe runs a step)
- **results** — `parse_result`, `stub_failure_is_valid`
- **policy** — `protected_globs`, `writable_globs`, `snapshot_ignore`,
  `cache_dirs`

## Swift

`nai/langs/swift.py` targets Swift 6 with a SwiftPM 5.9 manifest:

| Piece | Rule |
|-------|------|
| layout | `Sources/<Target>/<Target>.swift`, `Tests/<Target>Tests/<Target>Tests.swift`, shared entities in `Sources/<Package>Core/Entities.swift` |
| `Package.swift` | **adapter-owned**: `prepare_workspace` regenerates it deterministically from the component map and the targets that exist on disk; the coder cannot edit it |
| stub | `public func … { fatalError("not implemented") }`; integration nodes expose `public func integrate() -> Int` |
| tests | XCTest, `func test_<REQ-ID>_<behaviour>()`, `@testable import <Target>` |
| results | `error:` lines → `BUILD_ERROR`; `Fatal error: not implemented` with a non-zero exit → a valid stub failure; `Executed 0 tests` → `NO_TESTS` |
| syntax | `swiftc -parse` when `swiftc` is installed, otherwise no pre-check |
| platform lint | a component whose platform is `any` or `linux` may not import SwiftUI, UIKit or AppKit |
| Xcode | never generated: the owner supplies a project under `external/`, referenced by `project.settings.swift.xcode_project` and included in snapshots |

Remote recipes: `swift-build` and `swift-test` on any host with Swift;
`xcodebuild-test` on macOS with `xcode_project`, `scheme` and a `destination`
from an allowlist. The adapter chooses between them (`remote_recipe`), so a
portable host never tries to run `xcodebuild`.

The `tests/langs/fixtures/swift/` samples are **hand-captured** Swift 6.0 /
Xcode 16 output (see their README); the real-toolchain coverage is
`tests/e2e/test_pipeline_swift.py`, which skips without `swift`.

## TypeScript

`nai/langs/typescript.py` targets Node 20 with a pinned TypeScript 5 / vitest 2
toolchain. The adapter owns both manifests, so the model never edits build
configuration:

| Piece | Rule |
|-------|------|
| layout | `src/<component>/index.ts`, `tests/<component>.test.ts`, shared entities in `src/entities.ts` |
| `package.json` / `tsconfig.json` | **adapter-owned**: regenerated deterministically by `prepare_workspace`; strict, `noEmit`, ESM (`"type": "module"`) |
| stub | `export function … { throw new Error("not implemented"); }`; integration nodes expose `integrate(): number` |
| tests | vitest, `it("R1_<behaviour>", …)`, importing `../src/<component>/index.js` |
| install | `npm ci` when a lockfile exists, else `npm install`; the adapter never adds dependencies |
| results | vitest's JSON reporter gives per-case outcomes; `tsc --noEmit` diagnostics (`error TSxxxx`) decide `BUILD_ERROR`; `numTotalTests: 0` is `NO_TESTS` |
| platform lint | a `platform = any|linux` component may not import react, react-dom, react-native, expo or `@react-native/*` |

Remote recipes: `npm-build` and `npm-test`.

## Go

`nai/langs/golang.py` targets Go 1.22 with no third-party dependencies:

| Piece | Rule |
|-------|------|
| layout | `internal/<package>/<package>.go`, `internal/<package>/<package>_test.go`, shared entities in `internal/entities/entities.go` |
| `go.mod` | **adapter-owned**: `settings.go.module` when set, else the kebab-case project name |
| stub | `panic("not implemented")`; integration nodes expose `func Integrate() int` |
| tests | standard `testing`, `func Test_<REQ-ID>_<behaviour>(t *testing.T)` |
| results | `go test -json` events give per-test outcomes; `go build`/`go vet` diagnostics (`file.go:line:col:`) decide `BUILD_ERROR`; a run with no test events is `NO_TESTS` |
| platform lint | a portable component may not import Fyne, andlabs/ui, lxn/walk or therecipe/qt |

Test names need an underscore before the requirement id (`Test_R1_…`), because
`TestR1_…` has no separator and the traceability matcher would not see `R1`.

Remote recipes: `go-build` and `go-test`.

## Rust

`nai/langs/rust.py` targets Rust 1.75 / edition 2021 with one crate per
component:

| Piece | Rule |
|-------|------|
| layout | `crates/<crate>/src/lib.rs`, `crates/<crate>/tests/<crate>_test.rs`, shared entities in `crates/entities/src/lib.rs` |
| manifests | **adapter-owned**: `prepare_workspace` regenerates the workspace `Cargo.toml` and every crate manifest from the crates present on disk plus the contract's `depends_on` edges |
| stub | `unimplemented!()`; integration nodes expose `pub fn integrate() -> i64` |
| tests | libtest integration tests, `#[test] fn r1_<behaviour>()` |
| results | `cargo build --message-format=json` compiler messages decide `BUILD_ERROR`; libtest's `test <name> ... ok|FAILED` lines give per-test outcomes; `running 0 tests` is `NO_TESTS` |
| test command | targets `--test <crate>_test` only, so doc-test runs cannot look like a zero-test suite |
| platform lint | a portable component may not `use` iced, egui, gtk/gtk4, slint, druid or relm4 |

Remote recipes: `cargo-build` and `cargo-test`.

## C and C++

`nai/langs/_c_family.py` implements both adapters; `CAdapter` (C11) and
`CppAdapter` (C++17) differ only in the standard, source extension, stub syntax
and test framework:

| Piece | Rule |
|-------|------|
| layout | `include/<component>/<component>.h`, `src/<component>/<component>.c|cpp`, `tests/<component>/<component>_test.c|cpp` |
| `CMakeLists.txt` | **adapter-owned**: regenerated deterministically from the directories on disk; one static library and one test executable per component; `*_integration` targets link every component |
| vendored frameworks | `vendor/nai_assert.h` + `nai_runtime.h` (C) and `vendor/doctest.h` + a generated `tests/doctest_main.cpp` (C++) — no network at test time |
| stub | C: `NOT_IMPLEMENTED("<symbol>")` (aborts); C++: `throw std::logic_error("not implemented")`; integration nodes expose `int integrate(void)` / `int integrate()` |
| tests | C: the vendored harness (`nai_begin`/`nai_case`/`NAI_CHECK_*`); C++: doctest `TEST_CASE("R1_<behaviour>")` |
| results | the test binary prints JUnit XML on stdout (doctest via `--reporters=junit`), so the adapter runs it directly instead of parsing CTest prose; `file:line:col: error:` and linker failures decide `BUILD_ERROR`; sanitizer reports are attached to failure messages |
| mixed trees | a workspace mixing `.c` and `.cpp`/`.hpp` files is rejected before generation (one language per project) |
| platform lint | a portable component may not include AppKit, Cocoa, SwiftUI, Foundation, IOKit or Security headers |

Remote recipes: `cmake-configure`, `cmake-build` and `ctest`.

The `tests/langs/fixtures/{c,cpp}/` samples were captured from GCC 15 /
CMake 4.2 / doctest 2.4; `tests/e2e/test_pipeline_{c,cpp}.py` run the real
toolchain and skip when it is absent.

## Compiled-language shared helpers

`nai/langs/_common.py` holds the pieces every compiled-language adapter needs:
`pascal`/`camel`/`snake`/`kebab`, contract traversal (`components`,
`component_entry`, `entities`, `parse_provide`), a structural bracket/literal
check (`structural_error`), and `integration_build_failure`. Integration nodes
have no contract stub, so their expected pre-implementation state is a *build*
failure that names the integration module and matches a missing-symbol marker
(`no such file`, `undefined reference`, `did not match any packages`, …);
`stub_failure_is_valid(..., integration=True, expected_module=...)` accepts
exactly that and nothing else.


## Knowledge packs

`nai/langs/knowledge/<lang>/` holds six markdown sections: `spec`, `stub`,
`test`, `impl`, `pitfalls`, `compile_errors`. Each is capped at
`MAX_SECTION_CHARS = 2400` (~600 tokens at the project-wide `chars / 4`
estimate) and the cap is enforced by `tests/langs/test_knowledge.py`. Content
rules: only facts the author is certain of, with tool versions stated.

Injection is by job kind (`knowledge.JOB_SECTIONS`):

| Job kind | Sections |
|----------|----------|
| `implement` | impl, stub, pitfalls |
| `integrate` | impl, pitfalls |
| `author_tests` | test, pitfalls |
| `contract_stub` | stub, compile_errors |
| `verify` | compile_errors |
| `diagnose` | compile_errors, pitfalls |
| spec stages | spec |

`assemble_context_pack(..., job_kind=...)` adds the block as the
`language_knowledge` section; `author_tests` and `integrate` add it to their
prompts directly. The block is chosen from `project.settings.language`, never by
the model.

## Platform isolation

Contract components may declare `platform: any | linux | macos` (default
`any`). `checkers.check_platforms()` reports `SPEC3_PLATFORM` when an `any` or
`linux` component consumes a `macos` component, and `SPEC3_PLATFORM_INVALID` for
an unknown value. `handle_check_interfaces` fails deterministically on these
findings instead of asking the model to fill a gap. Spec prompts require
platform-specific code to live in leaf components with portable logic around it.

## Adding a language

1. Implement the protocol in `nai/langs/<id>.py`, including `stack_hint` and
   `toolchain`.
2. Register it in `nai/langs/__init__.py`.
3. Add `nai/langs/knowledge/<id>/` with all six sections under the cap.
4. Add `tests/e2e/fixtures/langs/<id>/project.json` and a
   `tests/e2e/test_pipeline_<id>.py` that loads it;
   `tests/e2e/test_language_fixtures.py` fails until both exist.
5. Add `tests/langs/test_<id>_adapter.py` plus captured output samples under
   `tests/langs/fixtures/<id>/` (with a README saying how they were captured),
   so the parser and stub renderer stay covered without the toolchain.
6. Add a CI job that installs the toolchain, runs the adapter unit tests, asserts
   `nai languages --json` reports the toolchain as present, and runs the e2e test.
7. Run `python -m nai.spec.schemas` so the contract `language` enum includes it.
8. Set `project.settings.language` to the new id (or pass `--language`).
