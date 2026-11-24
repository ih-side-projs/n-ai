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
- **results** — `parse_result`, `stub_failure_is_valid`
- **policy** — `protected_globs`, `writable_globs`, `snapshot_ignore`,
  `cache_dirs`

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
5. Run `python -m nai.spec.schemas` so the contract `language` enum includes it.
6. Set `project.settings.language` to the new id (or pass `--language`).
