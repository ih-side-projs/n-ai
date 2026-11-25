# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Integration fixes

- **Remote build/test workers (`nai/remote`)**: build, test and integration steps can run on another machine (for example a MacBook over the LAN) while Linux stays the only orchestrator. New migration `0008_remote_workers.sql` adds `workers` and `remote_tasks`; `nai worker add NAME` prints a bearer token once and stores only its sha256 digest (constant-time compare, `revoke` supported). `nai remote serve` runs the worker API on its own listener (`/worker/v1/heartbeat|claim|snapshot/<sha>|result`), defaults to `127.0.0.1`, and refuses a non-loopback bind without TLS unless `--insecure-lan` is given. A claim is one `BEGIN IMMEDIATE` transaction; leases last 60s and are extended by heartbeats; expired leases requeue and the third redelivery fails the task as an `infrastructure:` error. Snapshots are deterministic content-addressed `tar.gz` archives of the project minus the adapter's `snapshot_ignore`, kept in `.nai/snapshots` (last N), and extraction rejects absolute paths, `..`, links, devices and oversized archives. Workers execute only named recipes from `worker.toml` (argv templates, no shell, parameter allowlists validated on both sides, scrubbed environment); `nai-worker init|run|doctor` plus `deploy/nai-worker.plist` provide the macOS agent, SIGTERM and the heartbeat cancel flag kill the process group, and the work directory is replaced per snapshot except adapter cache dirs. `RemoteTarget` implements `ExecutionTarget`; `target_for` routes `macos` components (and `integrate` jobs whose subtree has one) to a worker, disables the coder's `run_command` for those nodes, and adds an adapter syntax pre-check to `implement`. Waiting for a capable worker (`waiting for worker (caps)`) and infrastructure failures requeue without counting an attempt (`remote.waiting`/`remote.infrastructure` events, bounded by `runner.remote_infra_retries`). New read-only `/workers` dashboard page and `nai remote status`.
- **Single execution seam (`nai/exec`)**: every command now runs through one `ExecutionTarget` (`nai/exec/target.py`) exposing `run(argv, cwd, env_overrides, timeout, cancel_event, output_cap)`, `capabilities()`, `describe()` and `close()`, implemented by `LocalTarget` (`local.py`) and `DockerTarget` (`docker.py`) and chosen by `make_target`. Routed callers: target-env creation and package installs, contract/author-test/verify/integrate steps, the model-facing `run_command` tool, the runner, workspace snapshots, git checkpoints, language toolchain probes, hold-out test runs and scheduler jobs. Hardening: the child environment is scrubbed to an allowlist (`PATH`, `HOME`, `LANG`, …) plus a secret-prefix denylist so generated code never sees `LLAMA_SWAP_KEY` and friends; setup runs with network access while run-phase commands are network-isolated (`unshare -rn` locally, `docker --network none`, with a missing `unshare` recorded once as weaker isolation); Docker adds `--memory`, `--cpus`, `--pids-limit`, `--cap-drop ALL`, `--security-opt no-new-privileges` and `--user uid:gid`, while local runs apply `prlimit`/`ulimit` limits; output is streamed with a head+tail cap instead of buffered `communicate()`; cancellation kills the local process group or the named container (`docker kill`), including a setup container. `bash -lc` is gone — pipeline steps pass argv and explicit shell steps use `bash -o pipefail -c`. New `execution-target` `nai doctor` check; target warnings (weaker isolation) are recorded as `note` events.
- **Language support model**: "supported" is derived from the registry (`nai/langs/support.py`) instead of a list: a language needs an adapter, a complete knowledge pack, a declared toolchain probe and an e2e fixture project. `Store.create_project`/`update_project`/`import_project`, `nai project create --language`, the web intake and settings dropdowns and the contract JSON schema (`language` enum generated from the registry, `platform` enum per component) all read the same source; unsupported ids are rejected and the clarify stage asks the owner rather than substituting. Added per-job knowledge packs in `nai/langs/knowledge/<lang>/` (`spec`, `stub`, `test`, `impl`, `pitfalls`, `compile_errors`), each capped at ~600 tokens by the `chars/4` estimate and enforced by test, injected by job kind in the context-pack builder. New `nai languages` command lists supported languages and probes toolchains locally and on named worker roots. New `SPEC3_PLATFORM` checker rejects `any`/`linux` components that consume `macos` components; spec prompts now instruct platform isolation and carry the supported-language list. Intentional behavior changes: the web language fields submit adapter ids (`python`) while `target_env.language` keeps the human stack string (`Python 3.12`); the contract schema gains optional `language` and per-component `platform`.
- **Language adapters**: added `nai/langs/` (`base.py` protocol, `registry.py`, `python.py`). The build pipeline no longer hard-codes Python: contract stubs, test authoring, verification commands, result classification and implement path policy route through the adapter selected by `project.settings.language`. Migration `0007_project_language.sql` defaults existing projects to `python`; `Store.create_project` fills the same default. New equivalence tests in `tests/langs/` pin the adapter to the previous behaviour. Intentional behavior changes: none for Python projects — commands, stubs, layout and path policy are byte-identical. The adapter seam is additive and `settings.language` is now always present.
- **Project export/import tree reconstruction**: `Store.export_project` now bundles manifest-tracked project files (excluding `.nai/`, the marker and the manifest), and `Store.import_project` rebuilds the tree: `SPEC.md`, `contract.json`, `specs/<node-path>/SPEC.md`, `interfaces.json`, bundled source/test files, and a fresh `.nai-manifest.json`. Imported projects keep the artifacts-only path as well. Added `nai project create` (Store-backed, honours `--output-dir`/`NAI_OUTPUT_ROOT`).
- **Deterministic project layout**: added `nai/layout.py` (pure path functions), `nai/paths.py` (root/marker/symlink guards and atomic writes) and `nai/manifest.py` (sha256 manifest, hand-edit detection, force-accept). Migration `0006_project_layout.sql` adds `project.slug`, `project.output_root` and `node.slug` with deterministic Python backfill. New projects live at `<output_root>/<slug>` with `.nai-project`, `.nai-manifest.json`, root `SPEC.md`/`contract.json`, `specs/<node-path>/SPEC.md`, and node source/test directories; spec, stub, test, implement and integrate writers route through the layout. Hand edits emit `project.hand_edit` and mark the owning node stale; jobs fail instead of overwriting unless `force_manifest` is set. Added `nai where`/`nai relocate`, web output-root allowlisting via `NAI_OUTPUT_ROOTS`, and node-done git checkpoints. Intentional behavior changes: child specs move from `SPEC-<node>/SPEC.md` to `specs/<node-path>/SPEC.md`; the default root moves from `.nai/workspaces/<id>` to `outputs/<slug>`; `SPEC.md` is written when a root node is created; existing projects are backfilled in place rather than moved.
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
- **Model server inspection**: `ModelServerClient` now normalizes an OpenAI base URL ending in `/v1`, so `/health` and `/running` are queried from the llama-swap root while `/v1/models` and chat completions use the API base. The Models page now reports a healthy server when `LLAMA_SWAP_URL=http://host:8080/v1`.
- The archived-project filter uses explicit `show_archived=0/1` links and rendered pages send `Cache-Control: no-store` so stale cached pages cannot hide filter changes.
- **Structured project intake (WEB-1 amendment)**: New Project is now a modal collecting name, a large description, language, interface type and optional guardrails (target users, core workflows, non-goals/constraints, runtime/deployment, stack/libraries, accessibility/security, testing expectations). New projects start in `info_gathering`; the `intake` job asks focused questions, stores answers and decides when design can begin. Owners can force design at any time; if answers exist, a final forced intake job writes the refined brief to the root node first. Uploading an existing spec seeds the root node, stores a `spec` artifact and skips Info Gathering. Added migration `0005`, prompt/schema, workflow phase support and tests.
- Models page reasoner/coder ids are now dropdowns populated from llama-swap's model catalogue, with a fallback option for a configured id that is not currently listed.
- Same-origin handling now accepts an opaque `Origin: null` only when the dashboard's own JavaScript sent `X-Requested-With: fetch`; ordinary POST forms are routed through `fetch()`. The sign-in page now loads the same JS shim, which was missing and caused plain `/auth` posts to be rejected. External origins remain rejected. Rejection logs now include raw `Origin`, parsed host, `Sec-Fetch-Site`, and `X-Requested-With`.
- Info Gathering UI polish: guardrail fields use balanced full/equal columns, submitted answers are shown read-only until the model asks a new round, completion bars use grey for incomplete and blue only for completed, and a Next Step panel tells the owner exactly where to go after intake and other workflow states.
- Fixed decompose dependency creation: component names in `depends_on` are resolved to real child node IDs, and older projects with missing children are reconciled on runner startup without overwriting existing nodes/specs. Completed component specs now move to `awaiting_approval` instead of repeatedly re-queueing; approving a spec grants a fresh node budget. Pause/Resume buttons now follow the project's stored status.
- Node pages now cap each major section height and give each section its own vertical scroll area, keeping the overall page compact while preserving access to long specs, contracts, traceability, jobs, and artifacts.
- Added `tests/e2e/test_pipeline_python.py` as the canonical imported-spec → spec → stubs → tests → implement → verify → done contract test. Fixes exposed by it: recovery no longer treats imported root specs as completed component specs; the build planner recognizes any successful implementation for a hash instead of only checking the newest job; implement/integrate results record `content_hash`; `Store.update_project_status()` now exists so BLD-12 can mark the project done.
- **E2E contract test hardening**: the canonical test now asserts the full node tree is `done`, every node's `built_hash == content_hash`, every job succeeded, exact source bytes on disk, authored test bytes/hashes and read-only mode, fully consumed stub scripts, and no `planner.error` or failure events. Removing the test's hand-written integration stub exposed a real bug: `author_tests` rejected the expected `ModuleNotFoundError` for an integration node's not-yet-created wiring module. It now accepts that exact failure (BLD-7) while keeping other import errors in the repair loop.
- **TENV-2 deprecation allowlist**: allowlist entries were emitted as `-W default::<message>`, which puts the message in the category slot; Python ignored the filter, so allowlisted deprecations still aborted verification. Filters now use `default:<message>::` for both `-W` and `PYTHONWARNINGS`.
- Approval controls moved onto the artifact sections they affect: root component map (`component-map`), written spec (`component-spec`), and contract (`interfaces`). The ambiguous approval-stage dropdown was removed from the Actions panel, and sequential root approvals (component map then interfaces) are supported.
- Removed implicit approval cascade: approving a parent no longer approves/rejects child nodes or records approvals for artifacts the owner did not review. Childless `component` nodes no longer receive parent-only `interfaces` jobs and are treated as leaves by the build planner.
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
- **STO-5 / BLD-10 / SCH-7**: hash updates on design-stage nodes no longer mark them stale; stale nodes are auto-promoted only when BLD-10's `built_hash == content_hash` plus frozen-test conditions hold; runner recovery returns an unapproved root from an implementation status to `draft`. This fixes projects that entered implementation directly after Info Gathering. The decomposed component map is preserved in project settings so later interface contracts do not erase the child names needed by `component_spec`.
- `.gitignore` now ignores only root-level `/build/`, so the integrated `nai/build/` package and `tests/build/` suite are tracked.

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
