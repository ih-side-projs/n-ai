# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Thin `nai service` supervisor: starts and watches the web dashboard and runner,
  tracks on-demand `nai` instances, exposes a local JSON control socket, and
  provides `start|stop|restart|status|logs|exec|jobs|cancel|shell` commands.
- `nai-service` console script and systemd/launchd deployment examples under `deploy/`.
- `nai web` now accepts `--host`, `--port` and `--db` flags.

### Planned

- Optional tool families beyond the stdlib set (markdown, Excel, Word, PDF, charts)
- HTTP transport for the MCP server (currently stdio only)
- HTMX partial-page interactions in the web dashboard
- Regression reports comparing benchmark runs against a stored baseline

## [2.0.0.dev0] - 2026-11-20

### Added

- Unified `nai/` package integrating all eight work packages plus the evaluation harness:
  - SS-CORE: LLM client with constrained output, swap detection, call logging, workspace policies, verification gate, git checkpoints, v1 CLI
  - SS-STORE: SQLite store with numbered migrations, status machine, content hashing, export/import, backups
  - SS-SCHED: job runner with batching, budgets, crash recovery, scheduling windows and plugin registry
  - SS-SPEC: recursive spec pipeline with JSON schemas, versioned prompts and mechanical checkers
  - SS-BUILD: context packs, contract stubs, test authoring, verification, escalation and target environments
  - SS-TOOLS: tool registry, sandbox, MCP server over stdio, and stdlib tool families
  - SS-WEB: local dashboard with project tree, approvals, editing with diff, controls, escalation inbox, tools page, SSE logs, metrics and model smoke tests
  - SS-EVAL: benchmark suites, scripted stub server, `nai bench` and JSON/Markdown reports
- Scenario step schema now uses explicit `caller`, `callee`, `call` and `fields` properties; free-text fallbacks removed.
- D9 structured-output fallback chain: JSON-schema `response_format` → GBNF grammar → validate-and-repair.
- Configurable X9 size limits (`[limits]` TOML table), including `max_components_per_level` (default 7).
- Integrated test suites from every work package: **485 passed, 1 skipped**, no model or network required.
- Repository documentation: README, CONTRIBUTING, CODE_OF_CONDUCT, SECURITY, CHANGELOG, `docs/` guide and GitHub templates.

### Changed

- `schemas.py` is the single source of truth; `schemas/*.json` are generated via `python -m nai.spec.schemas`.
- Web process keeps read-only store connections; all state changes go through the command queue.
- Store migration `0002` extends command actions for web controls and adds tool schema columns.

### Fixed

- `nai.eval` import failure (missing `field` import) and `BenchmarkResult` defaults.
- `terminal.log_context()` restored for scheduler job log tagging.
- Structured output no longer retries an unsupported constraint mode; strips `<think>` blocks and markdown fences before parsing.
- Interface checker no longer depends on regexes over free-text actions.

## [1.0.0] - 2026-11-16

### Added

- v1 `devswarm` program: spec-driven orchestration with a reasoner and coder model, workspace tools, verification gate and git checkpoints.

[Unreleased]: https://github.com/ih-side-projs/n-ai/compare/2.0.0.dev0...HEAD
[2.0.0.dev0]: https://github.com/ih-side-projs/n-ai/compare/1.0.0...2.0.0.dev0
[1.0.0]: https://github.com/ih-side-projs/n-ai/releases/tag/1.0.0
