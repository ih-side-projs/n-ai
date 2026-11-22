# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Integration fixes

- **Pass 1 (STO-3)**: web approve/reject/retry now enqueue commands through the `commands` table instead of writing approval/status directly. UI offers stage selection (`interfaces`, `component-spec`, `component-map`). `CommandProcessor` handles all three spec stages so an `interfaces` approval unblocks child readiness. Tests assert no direct writes.
- **Pass 2 (runner state & recovery)**: requeued jobs no longer burn attempts (`input_ref.requeued` marker, `Store.update_job(input_ref=...)`); `list_events`/`list_jobs` return the most recent window (was oldest-first, starving pause-state/budget/planner restoration); orphan nodes in `running`/`verifying` are now repaired via `stale → ready`; streaming LLM calls observe `cancel_event`; shell command output is capped at 1 MiB.
- **Pass 3 (tool execution & MCP)**: sandbox is now per-call via `contextvars` (no module globals, safe with `concurrency > 1`); registry enforces argument types, per-tool timeouts (killable process), output limits; MCP no longer calls deprecated `set_sandbox`; removed inline CSP-blocked script from `tools.html` and fixed unescaped `innerHTML` sink in `static/app.js`; removed `|safe` from `service.html`.
- **Pass 4 (structured output & model client)**: single `nai/structured.py` now implements validation (jsonschema + keyword-complete fallback), grammar generation, D9 fallback chain, repair loop, and structured error classification; `nai/spec/structured.py` and `nai/build/structured.py` re-export from it. Bare `400`/`422` no longer triggers a false D9 fallback. Swap detection latency fallback now subtracts estimated prompt prefill so long prompts are not mistaken for a model swap.
- **Pass 5 (daemon hardening)**: stale socket/pid artifacts after a crash are removed on `start_detached()` (live daemon still detected and refused); control socket chmod `0600`; exec job names and explicit log names validated against a safe character set with resolved-path containment checks.
- **Pass 6 (evaluation harness)**: added hold-out test support (EVAL-3) — `Holdout:` spec metadata, tests applied to generated output in a temp copy, pass rate included in success and reports; `nai bench` now drives the v2 spec→build→done pipeline in-process by default (`--mode v1` for the legacy CLI); `--list` no longer requires `--profile`; reports show pipeline mode and hold-out results.
- **Pass 7 (test fidelity & CI)**: scheduler `FakeStore` now imports transitions/actions/job kinds directly from the real store (with a fidelity test); ruff lint added to CI and codebase cleaned up; CI runs the end-to-end stub-LLM test explicitly.
- **Pass 8 (cleanup & self-check)**: added offline `nai doctor` / `nai --doctor` / `nai-doctor` integration check covering imports, CLI registration, JobContext/PlanContext compatibility, handler coverage, artifact kinds, schema artifacts, package data, real SQLite migrations and tool registration. Subcommand import failures are now recorded instead of silently swallowed.
- **Pass 9 (end-to-end coverage)**: added `tests/e2e/` with full CLI workflow tests (both `python -m nai` and `devswarm.py`, scripted stub LLM, verified workspace) and full web workflow tests (create/edit/approve/reject/retry/rerun/reorder/pause/resume/cancel, JSON API parity and tool execution) against the real store and `CommandProcessor`. Fixed missing `cancel`/`rerun_stage`/`reorder` command handlers and a thread-affinity bug in `/tools/call` that silently dropped registered project workspaces.
- Web project controls (pause/resume/cancel) now submit via `fetch()` from `static/app.js` and reload the page, instead of navigating the browser to the endpoint's raw JSON response. Confirmation-only forms (mark stale, stop service) use `data-confirm` from the same external script; all inline `onsubmit` handlers were removed because the app CSP blocks inline scripts.
- **Project lifecycle (WEB-10 amendment)**: added `cancelled` project status; project-wide cancel now sets it so cancelled projects no longer display as Active and the runner treats them as inactive. Added archive/restore commands and a Projects-page "Show archived" toggle (also `?show_archived=1`) so test projects can be hidden without deletion. Migration `0004` widens the project status CHECK while preserving child rows.
- **Spec pipeline now runs end-to-end**: planner uses the real `PlanContext` API; `clarify`, `select_map` and `lint` are valid job kinds (new migration 0003); artifact kinds corrected; `decompose` creates child nodes; `component_spec` persists `spec_text`; `clarify` stores assumptions; repair rounds are bounded.
- `check_interfaces` now reports all mechanical findings (dependency cycles, unconsumed fields), not just scenario steps.
- MCP server fixed: `nai mcp` is registered as a subcommand, `mcp_main()` accepts no argv, and stdio writes text rather than bytes.
- New end-to-end test drives brief → spec → build with the scripted stub LLM and asserts no `planner.error` events.
- Eval harness writes `.nai/metrics.json` for every successful run; `benchmarks/` populated with the six built-in specs.
- Runner: `step()` is guarded against per-pass exceptions; `close()` drains the inbox before joining workers.
- Web retry no longer double-counts attempts; `_enqueue_command` helper added for STO-3 command-queue routing.
- Consult feature fixed (`nai.consult` import, errors surfaced); duplicate attempt increments removed from build handlers.

### Specification amendments

- **§4 / STO-5**: `content_hash` now uses each dependency's *interface hash* (contract only), not its full content hash. Editing a dependency's spec or implementation no longer stales dependents whose interface is unchanged.
- **STO-4 / SCH-5**: `blocked` is now reachable. Budget exhaustion moves `ready`/`running`/`verifying → blocked`, then the failure ladder moves `blocked → failed`. Owner retry can move `blocked → ready` with a fresh budget.
- **SCH-4**: test authoring can be batched ahead of implementation: an approved node with healthy (not done) dependencies may be promoted to `ready`; `implement` still requires all dependencies `done`.

### Security

- `/tools/call` no longer accepts an arbitrary workspace path; only registered project workspaces (or the default sandbox) are allowed.
- Same-origin checks use a fixed host allowlist (loopback plus configured names); `Host` / `X-Forwarded-Host` are no longer trusted, defeating DNS-rebinding bypasses.
- Web server refuses to bind a non-loopback address without `NAI_WEB_TOKEN`; `exports.sh` now defaults to loopback.
- `/service/control exec` only accepts a spec path and optional `-w WORKSPACE` — no arbitrary `nai` flags from the browser; all reflected values are HTML-escaped.
- Command runner strips secret environment variables (`LLAMA_SWAP_KEY`, `NAI_WEB_TOKEN`, …) from executed commands; Docker mode adds `--network none`, memory/CPU caps and a non-root user; cancellation works in Docker mode.
- Tool sandbox timeouts now terminate a child **process** (threads cannot be killed); docs no longer overstate network isolation.
- Build path-policy helpers fail closed instead of silently substituting a `SimpleNamespace`.

### Fixed

- `/projects/{id}/nodes/{id}/reject` now uses the legal `awaiting_approval → stale → draft` transition and records the actual latest approval stage.
- `author_tests` validation now requires failures caused by `NotImplementedError`, not arbitrary errors.
- Implement policy protects root `conftest.py`, `pytest.ini`, `pyproject.toml` and similar test config files.
- `pip --require-hashes` is used when all target-environment pins are hash pins.
- Wheel now contains migrations, spec schemas, build prompts and web templates; CI builds and smoke-tests it in a clean venv.

### Changed

- `tests/web` and `tests/web2` merged into a single web suite.
- `deploy/nai.service` renamed to `deploy/nai@.service` (systemd template unit).

### Fixed

- `/service` page now correctly reports a running supervisor (status payload was missing the `running` flag).
- Models page summary cards no longer wrap text awkwardly; health/count cards have separate value and detail lines.
- Web tools page now executes registered tools through the shared registry in a sandbox (previously echoed arguments).
- "New project" form now has a working `/projects/create` route that creates the project and root node.
- Dashboard runner status now reads `runner.json`, matching what SCH-9 actually writes.
- CSRF middleware replays the request body so form endpoints can bind `Form(...)` parameters.

### Added

- Named workflow phases: `nai.workflow` derives `design` / `implementation` per node from status and job history; projects report `design`, `implementation` or `mixed` (phases may overlap).
- Web Design / Implementation tabs with filtered spec tree, phase badges and explanatory banners; `/api/projects/{id}/phase` API.
- Web `/service` page: supervisor status, child restart, nai job submission/cancel and log viewing.
- Web `/projects/{id}/settings` page for X9 limits, budgets, test commands, backup location and target language (WEB-12).
- Web `/models` page can edit and persist model profile ids (reasoner/coder) to the store so the runner uses them.
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
