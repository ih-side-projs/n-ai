# Language adapters

`nai/langs/` is the only layer that knows a language's toolchain. The pipeline
asks an adapter for layout, stub rendering, commands, result parsing and path
policy instead of hard-coding Python assumptions.

## Modules

| Module | Responsibility |
|--------|----------------|
| `base.py` | `LanguageAdapter` protocol, `Command`, `Outcome`, `ParseResult`, `StubResult` |
| `registry.py` | `register()`, `get()`, `language_for()`, `supported()`, `UnknownLanguageError` |
| `python.py` | Python 3 adapter (pytest, venv/pip, ruff, `NotImplementedError` stubs) |

## Selection

The adapter is chosen by `project.settings.language`:

```python
from nai.langs import registry

adapter = registry.language_for(project)   # defaults to "python"
```

Migration `0007_project_language.sql` writes `settings.language = "python"` for
projects created before the key existed, and `Store.create_project` fills the
same default for new projects. An unknown language raises
`registry.UnknownLanguageError` rather than silently falling back.

## Interface

An adapter supplies:

- **identity** — `id`, `test_framework`, `extensions`
- **layout** — `source_rel`, `test_rel`, `entities_rel`, `normalize_test_path`,
  `check_syntax`
- **stubs** — `render_stub`, `validate_stub`, `extract_signatures`
- **commands** — `setup_commands`, `build_commands`, `test_command`,
  `lint_command`, `env_overrides` (argv lists, never shell strings)
- **results** — `parse_result`, `stub_failure_is_valid`
- **policy** — `protected_globs`, `writable_globs`, `snapshot_ignore`,
  `cache_dirs`

## Wiring

`nai/build/jobs.py` routes through the adapter for contract stubs, test
authoring, verification commands and implement path policy.
`nai/build/context_pack.py` uses the adapter for dependency signature fences,
and `nai/build/tenv.py` carries `TargetEnv.language` plus
`wrap_adapter_command()` for argv-shaped commands.

## Adding a language

1. Implement the protocol in `nai/langs/<id>.py`.
2. Register it in `nai/langs/__init__.py`.
3. Add equivalence tests next to `tests/langs/`.
4. Set `project.settings.language` to the new id.
