# Contributing to nai

Thanks for your interest in improving nai. This document explains how to set up
a development environment, what we expect from changes, and how to get them merged.

## Code of Conduct

By participating you agree to abide by the [Code of Conduct](CODE_OF_CONDUCT.md).
Please read it before opening issues or pull requests.

## Development setup

```bash
git clone https://github.com/ih-side-projs/n-ai.git
cd n-ai
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e '.[dev,web,eval]'
pytest -q                          # must be green before you start
```

You do **not** need a GPU or a running model server to develop: the test suite
uses a scripted stub server (`nai.testing.stub_server`, `nai.eval.stub_server`).

## Repository layout

```
nai/                 package source (one folder per subsystem)
  core modules       cli.py, llm.py, config.py, models.py, ...
  store/             SQLite persistence, migrations
  sched/             runner, budgets, plugins
  spec/              spec pipeline, schemas, prompts
  build/             build pipeline, context packs, escalation
  tools/             registry, sandbox, MCP server, families
  web/               FastAPI app, templates, static assets
  eval/              benchmarks, harness, reports, stub server
tests/               integrated tests, grouped by subsystem
docs/                documentation
```

## How to contribute

1. **Open an issue first** for anything non-trivial (bug with a clear reproduction,
   or a proposal with the motivation). Use the issue templates.
2. **Fork and branch** from `main`: `git checkout -b fix/short-description`.
3. **Make a focused change.** Include tests for new behaviour and for bugs you fix.
4. **Run the checks** (see below).
5. **Open a pull request** using the PR template. Link the issue, describe the
   change, and note any breaking changes or new dependencies.

## Checks

```bash
pytest -q                          # all tests (must pass)
pytest tests/<subsystem>/ -q       # faster loop while developing
python -m compileall -q nai        # syntax check
```

What we look for:

- Tests are deterministic and require no network or real model.
- Requirement IDs (e.g. `CORE-2`, `STO-3`, `X9`) are referenced in comments or
  test names where relevant.
- Prompts live in versioned `*.txt` files, not inline strings (X10).
- New tools follow the registry contract: type hints, docstring, returns a string;
  errors start with `ERROR:` (X3).
- Configurable limits are read from config, not hard-coded (X9).
- No project content is sent anywhere except the configured local model server.

## Style

- Python 3.10+ compatible syntax; prefer the standard library.
- Keep public APIs typed and documented with short docstrings.
- Follow the existing module layout; do not mix subsystems.
- Use `pathlib`, explicit UTF-8 reads/writes, and deterministic ordering.
- Do not add runtime dependencies without discussing them in an issue.

## Commit messages

Short imperative subject line, optional body explaining *why*:

```
Fix crash recovery for orphaned verifying nodes

The recover() path only walked root nodes; children were skipped.
Refs: SCH-7
```

## Reporting bugs

Include:

- Python version, OS, and `nai --version`.
- Exact command and a minimal spec/config to reproduce.
- Full traceback or relevant log lines (`--call-log` output is ideal).
- Whether a real model server was involved.

Use the [bug report template](.github/ISSUE_TEMPLATE/bug_report.md).

## Adding a tool family

See [Subsystems: Tools](docs/subsystems/tools.md). In short: one module in
`nai/tools/families/`, decorated with `@register_tool`, one test module, and no
changes elsewhere.

## Adding documentation

Documentation lives in `docs/`. Update the relevant page and add it to
`mkdocs.yml` navigation if you create a new file. Keep the README as the short
entry point and put detail in `docs/`.

## License

By contributing you agree that your contributions are licensed under the
[MIT License](LICENSE).
