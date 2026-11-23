# nai (N-AI): Overall Specification

## 0. How to use this document

This is the master specification for nai (N-AI): a local-first system that turns a project specification into a tested codebase using locally served LLMs, with a web interface for tracking projects and a library of helper tools the LLMs can call. It is written to be handed to several separate frontier-model conversations, each working on one part.

If you are such a conversation:

- Read sections 1 to 5 in full (shared context), then only the subsystem section(s) named in your work package in section 14.
- Do not silently change shared definitions (sections 4 and 5). If you need a change, list it as a numbered proposal at the end of your output.
- Do not ask questions unless an open decision in section 15 blocks you. Otherwise choose a sensible default, state it, and continue.
- Deliver what your work package lists: code, tests, and a short README for your part. Everything must be runnable and tested on the environment in section 2.
- Requirement ids (for example STO-3) are stable. Reference them in code comments and test names.

## 1. Purpose, goals, non-goals

**Purpose.** The owner writes (or dictates) a rough project description. nai turns it, entirely on local models, into a layered specification (system, subsystems, components), then into working, tested code, with the owner approving the plan at defined gates. It also provides a web interface to track every project and a library of Python tools (documents, spreadsheets, PDFs and so on) the models can call.

**Goals.**

- G1. Build programs larger than fit in one model context, by decomposing specs recursively and implementing the smallest units with a small local coder model.
- G2. Run with no data leaving the machine. Project content (specs, code, logs) never goes to an external service at runtime.
- G3. Be resumable, inspectable and incremental: every decision, job and result is stored; a change to a spec rebuilds only what depends on it.
- G4. Use deterministic Python for control flow, checks and context assembly, and use the models only for narrow, well-defined generation and judgment jobs.
- G5. Expose tools to models through one registry that serves both the orchestration code and an MCP server.
- G6. Provide a web UI for project tracking, approvals, live logs and tool/model status.
- G7. Measure itself: a benchmark suite and metrics determine the safe unit sizes and show whether a change to the pipeline helps.

**Non-goals.** Multi-user hosting, cloud deployment, Windows-native support, training or fine-tuning models, running several projects' GPU jobs at the same instant, automatic use of external AI services at runtime.

## 2. Environment and constraints

- Hardware: AMD Radeon RX 6600 (8 GB VRAM), 64 GB system RAM. One GPU, so at most one model resident at a time unless llama-swap groups say otherwise. Model loads are slow and must be minimised.
- Model serving: llama-swap in front of llama-server, OpenAI-compatible API (`/v1/chat/completions`, `/v1/models`). Models are Qwen-family: a reasoning model and a coding model. Context windows are small (default 32768). Do not assume features beyond llama-server's: tool calling, streaming, and JSON-schema or grammar-constrained output.
- OS and language: Linux, macOS or WSL; Python 3.10+ (tested on 3.12). Web UI served locally.
- Dependencies: prefer the standard library; allowed third-party packages are listed per subsystem. No dependency may require network access at runtime.
- Confidentiality: runtime makes no network calls except to the configured local model server and, for dependency installation only, the package index or local wheelhouse described in TENV-2 (package names and versions, never project content). Any feature that could send content elsewhere (see ESC-4) is off by default and requires explicit per-use confirmation.
- Existing code: `devswarm.py` (v1, about 880 lines) and its as-built spec `SPEC.md` are the starting point. v1 components (text-utils, terminal-output, verification-gate, git-checkpoint, llm-client, command-runner, workspace-tools, coder-agent, consult-threads, session-state, orchestrator-loop, cli) are carried into v2 as described in section 6. Conversations working on SS-CORE must be given the v1 spec and source.

## 3. Architecture overview

| Id | Subsystem | Purpose | Depends on |
| --- | --- | --- | --- |
| SS-CORE | core library | LLM client, confined workspace, command runner, verification gate, git checkpoints, text utilities | none |
| SS-STORE | store | SQLite persistence of projects, nodes, jobs, events, artifacts | SS-CORE |
| SS-SCHED | scheduler | Chooses and runs jobs; batches by model; budgets; crash recovery | SS-STORE, SS-CORE |
| SS-SPEC | spec pipeline | Locally generates, checks and recursively decomposes specifications | SS-SCHED, SS-STORE, SS-CORE |
| SS-BUILD | build pipeline | Context packs, contract stubs, tests, implementation, integration, failure handling | SS-SCHED, SS-STORE, SS-CORE, SS-TOOLS |
| SS-TOOLS | tool library | Tool registry, MCP server, document/spreadsheet/PDF tools | SS-CORE |
| SS-WEB | web interface | API and UI for projects, tree, logs, approvals, tools, models | SS-STORE, SS-SCHED, SS-TOOLS |
| SS-EVAL | evaluation | Benchmarks, hold-out tests, leaf-size measurement, regression runs | all |

Data flow: the owner submits a project brief (SS-WEB or CLI) -> SS-SPEC produces an approved tree of spec nodes with frozen interface contracts -> SS-BUILD turns each leaf into stubs, tests and code, then integrates upward -> results, logs and metrics are in SS-STORE and shown by SS-WEB. SS-SCHED runs all model jobs. SS-TOOLS is available to jobs through allowlists.

Control principle: Python code owns control flow and state. A model never decides which file to read for a job, which job runs next, or whether tests passed.

## 4. Shared data model

All persistent entities live in SQLite (SS-STORE). Ids are short unique strings. Times are UTC ISO-8601. JSON columns hold UTF-8 JSON.

- **Project**: `id`, `name`, `brief` (owner's original text), `workspace_path`, `status` (`active`, `paused`, `done`, `cancelled`, `archived`), `created_at`, `settings` (JSON: model profile name, size limits, test commands, target language and stack, and the target environment of TENV-1). `cancelled` is set by a project-wide cancel; `archived` hides a project from the default Projects view without deleting it.
- **Node**: `id`, `project_id`, `parent_id` (null for the root), `kind` (`system`, `subsystem`, `component`, `leaf`, `integration`), `name` (lowercase-hyphenated), `objective` (one or two sentences), `spec_text` (markdown), `contract` (JSON, see below), `depends_on` (list of node ids, siblings only), `status`, `content_hash`, `attempts`, `test_command`, `created_at`, `updated_at`.
- **Node status**: `draft` (being written), `awaiting_approval`, `approved`, `ready` (all dependencies done), `running`, `verifying`, `done`, `failed`, `escalated`, `blocked`, `stale` (an input changed). Allowed transitions are specified in STO-4.
- **Contract** (the interface model; per parent node, describing its children): JSON with `entities` (name, fields: name, type, required, description), `components` (child node name, `provides`: functions or endpoints with signature, input entities, output entities, errors; `consumes`: references to other components' provides), `external_inputs`, `external_outputs`, `scenarios` (id, title, ordered steps each naming caller, callee, call, and fields passed). This structure is validated by a JSON schema stored in the repo (SPEC-5).
- **Requirement**: `id` (for example `R3`, `E2`), `node_id`, `text`, `testable` (bool). Parsed from spec text using the headings and id format `- R1:` and `- E1:` under each component, matching the v1 and decomposable-spec conventions.
- **TestRecord**: `node_id`, `requirement_ids`, `file_path`, `frozen_hash`, `last_result` (`pass`, `fail`, `error`, `none`), `last_run_at`.
- **Job**: `id`, `project_id`, `node_id`, `kind`, `model_role` (`reasoner` or `coder`), `status` (`queued`, `running`, `succeeded`, `failed`, `cancelled`), `attempt`, `input_ref` (JSON: what was given), `result_ref` (JSON), `tokens_in`, `tokens_out`, `started_at`, `ended_at`, `error`.
- **Job kinds**: `intake`, `decompose`, `interfaces`, `check_interfaces`, `component_spec`, `reconcile`, `contract_stub`, `author_tests`, `implement`, `verify`, `integrate`, `diagnose`, `review`.
- **Event**: append-only log row: `id`, `project_id`, `node_id` (nullable), `job_id` (nullable), `time`, `level`, `type`, `message`, `data` (JSON). Everything the UI shows as history comes from events.
- **Artifact**: `id`, `project_id`, `node_id`, `kind` (`spec`, `contract`, `code`, `test`, `output-file`, `report`, `log`), `path`, `sha1`, `created_at`.
- **Approval**: `id`, `node_id`, `stage` (`component-map`, `interfaces`, `component-spec`, `escalation`), `decision` (`approved`, `rejected`, `edited`), `note`, `time`.
- **ModelProfile**: `name`, `reasoner` and `coder` blocks each with `model_id`, `ctx`, `max_tokens`, sampling (`temperature`, `top_p`, `top_k`, extras), plus `swap_cost_seconds` (measured, optional).
- **ToolDef**: `name`, `description`, `parameters` (JSON schema), `family`, `side_effects` (`none`, `writes-outputs`), `timeout_seconds`.

**Content hash.** `content_hash(node) = sha1(spec_text + canonical_json(contract) + interface_hashes of its depends_on nodes + content_hash of parent contract)`, where `interface_hash(dep) = sha1(canonical_json(dep.contract))`. Only a dependency's *interface* participates: editing a dependency's internal implementation or spec text must not invalidate dependents whose interface is unchanged. A node whose hash differs from the hash it was last built against is `stale`.

## 5. Cross-cutting rules

- X1. **Structured outputs.** Any model job whose result is consumed by code uses constrained JSON output against a schema where the server supports it, and is validated in Python regardless. A validation failure triggers a repair prompt containing the validation errors, at most 3 rounds, then the job fails.
- X2. **Determinism where possible.** Context packs, dependency ordering, hashing, size checks, interface-coverage checks and test-id traceability are plain code with unit tests, never model calls.
- X3. **Tool results** are strings; failures start with `ERROR:` and never raise out of the tool layer (carried from v1).
- X4. **Resumability.** Every job is idempotent or has a recorded partial state. After a crash, jobs left `running` return to `queued` on startup.
- X5. **Budgets.** Every node has configurable limits: max attempts, max wall-clock seconds, max tokens. Exceeding one moves the node to `failed` or `escalated`; it never loops forever.
- X6. **Logging.** Every model call records model id, prompt token count, completion token count, wall time, and whether a model swap preceded it. Full prompts and responses are stored as artifacts so any job can be replayed.
- X7. **Configuration** lives in a TOML file plus environment overrides (`LLAMA_SWAP_URL`, `LLAMA_SWAP_KEY`) plus per-project settings in the store. No secrets in the store.
- X8. **Testing.** Every subsystem ships its own tests runnable with `pytest -q`, using a scripted stub model server (see EVAL-2) so no real model is needed. v1's scenarios S1 to S6 must still pass.
- X9. **Size limits are config, not constants:** `max_requirements_per_component` (default 8), `max_public_functions_per_component` (default 10), `max_components_per_level` (default 7), `max_depth` (default 4). EVAL-4 tunes them.
- X10. **Prompts** live in versioned text files, not inline strings, with the version recorded on every job.

## 6. SS-CORE: core library

Purpose: the reusable base. Refactor of v1 into a package (`nai/`), preserving behaviour.

- CORE-1. Split `devswarm.py` along v1 component lines into modules: `textutil`, `terminal`, `gate`, `gitcp`, `llm`, `runner`, `workspace`, `coder`, plus later modules added by other subsystems. A thin `devswarm.py` or `python -m nai` entry point keeps the v1 command line working.
- CORE-2. `llm.chat` supports an optional `response_format` or grammar argument for constrained JSON output, and returns usage (prompt and completion tokens) and whether the call triggered a model swap (detected by response latency of the first token against a configurable threshold, or by llama-swap's running-models endpoint if available).
- CORE-3. `Workspace` gains a per-job **path policy**: lists of writable globs and read-only globs. Writes outside the writable set return `ERROR: path not writable for this job: <path>`. Frozen test files are read-only to the coder.
- CORE-4. The verification gate is generalised to per-node test commands: a node's pass is recorded against the node's command and the workspace fingerprint; the v1 recognition rules are kept for the auto mode.
- CORE-5. Git checkpoints are tagged with node id and job kind (commit message `<node>: <job kind> attempt <n>`), so any node's state can be restored.
- CORE-6. Docker mode and local mode of the command runner are kept. The Docker `/work` prefix-stripping quirk listed in v1 known limitations is fixed.
- CORE-7. A `ModelClient` abstraction selects model id and sampling from a ModelProfile by role, so no module hard-codes model ids or sampling.
- CORE-8. v1 behaviour is a regression baseline: the six scenarios in the v1 spec pass against the refactored package.

## 7. SS-STORE: persistence

Stack: standard library `sqlite3`, WAL mode, plain SQL migrations. No ORM.

- STO-1. Schema implements the entities in section 4, with foreign keys on and indexes on `(project_id, status)`, `(node_id)`, `(job status, model_role)` and event `(project_id, time)`.
- STO-2. Migrations are numbered SQL files applied in order at startup; the applied version is stored; downgrade is not supported.
- STO-3. One writer process (the scheduler/runner) writes; the web server opens the database read-only for queries and sends control commands (pause, approve, retry) by inserting rows into a `commands` table that the writer polls.
- STO-4. Node status transitions are enforced in one function. Allowed: `draft -> awaiting_approval -> approved -> ready -> running -> verifying -> done`; `ready or running or verifying -> blocked` when a budget is exhausted (SCH-5); `blocked -> failed` when the failure ladder takes over, `blocked -> ready` on owner retry, or `blocked -> stale` on content change; `running or verifying -> failed`; `failed -> ready` (retry) or `escalated`; `escalated -> ready` after resolution; any status except `done` -> `stale` when the content hash changes; `stale -> draft or ready` by owner action or rebuild; `done -> stale` likewise. Any other transition raises an error and writes an event.
- STO-5. `content_hash` is recomputed and compared whenever a node spec, a contract, or a dependency changes; stale propagation marks all transitive dependents stale in one transaction. Hash changes on `draft` or `awaiting_approval` nodes update the hash without moving the node to `stale`, because design nodes have not been built yet.
- STO-6. Prompts, responses, spec files and code are stored as artifacts on disk under the project workspace in `.nai/artifacts/`; the database holds paths and hashes.
- STO-7. Project export and import: a project can be exported to a single folder or archive (database rows as JSON plus artifacts) and re-imported on another machine.
- STO-8. Daily automatic backup copy of the database file to a configured directory, keeping the last N copies.

## 8. SS-SCHED: scheduler and runner

- SCH-1. A long-running runner process (`nai run`) polls the store for ready nodes and queued jobs and executes them. It is the only process that calls models or runs commands.
- SCH-2. **Model-affinity batching.** Jobs carry a `model_role`. The scheduler drains all runnable jobs for the currently loaded role before switching, up to a configurable cap on consecutive jobs or elapsed time, so model swaps are minimised. Swap count and swap seconds are recorded.
- SCH-3. Within a role, jobs run in priority order: higher-level nodes first, then older first. Concurrency defaults to 1 and is configurable (for same-model parallel slots, see decision D4).
- SCH-4. Dependency rule: a node becomes `ready` when its parent's contract is approved and either every node in `depends_on` is `done`, or its next scheduled job is `author_tests` (BLD-8 batches test authoring ahead of implementation, since tests are written from the spec and stubs, not from dependency implementations). `implement` jobs still require all dependencies `done`.
- SCH-5. Per-node budgets (X5) are enforced; on exhaustion the node moves to `blocked` (a visible, named state), then `failed` as the failure ladder (BLD-9) takes over. The owner may retry a blocked node (`blocked -> ready`) with a fresh budget.
- SCH-6. Commands: `pause`, `resume`, `cancel job`, `retry node`, `approve`, `reject`. Pause finishes the current job, then stops. Cancel kills a running command's process group.
- SCH-7. Crash recovery per X4. On startup the runner reports what it recovered as events. It also returns a root `system` node that is in an implementation status without an `interfaces` approval, children, or a contract back to `draft`, so a broken lifecycle cannot strand a project.
- SCH-8. Scheduling windows: an optional config allows work only in given hours (for example overnight) and an `--until` option for a one-off.
- SCH-9. The runner exposes health: current job, loaded model, queue depth per role, and a heartbeat timestamp the UI reads.

## 9. SS-SPEC: local spec pipeline

Purpose: with local models only, turn a brief into an approved tree of specs with frozen interfaces. The pipeline is a sequence of narrow conversations, each starting from a fresh context built by Python.

**Stages for one parent node** (the root is a node of kind `system`):

1. **Component map** (`decompose` job, reasoner). Input: the node's brief or spec, size limits, target stack. Output (JSON): 3 to `max_components_per_level` children, each with name, one-to-two sentence objective, and `depends_on`. Also a list of shared entity names and a list of external inputs and outputs. Nothing else.
2. **Candidate selection** (optional, setting `candidates`, default 1 for non-root levels and 3 for the root). Generate N maps in separate calls at higher temperature; a `review` job compares them against the brief and picks or merges; the owner sees all and the recommendation.
3. **Approval gate** `component-map` (owner).
4. **Interface model** (`interfaces` job, reasoner, fresh context). Input: approved map and brief only. Output: the Contract JSON (section 4), including scenarios, validated by schema.
5. **Mechanical interface checks** (code, no model): see SPEC-3.
6. **Scenario walk-through** (`check_interfaces` job, reasoner). Input: contract and scenarios. For each scenario step the model names the provider, the call and the fields used; code verifies each named call and field exists in the contract. Gaps loop back to stage 4 with the gap list, at most 3 rounds.
7. **Approval gate** `interfaces` (owner).
8. **Component specs** (`component_spec` jobs, reasoner, one per child, each in a fresh context). Input: the child's objective, the shared entities and interfaces it touches, and size limits. Output: spec markdown in the project spec template (purpose, interface, requirements R1.., edge cases E1.., at least one exact example, dependencies).
9. **Lint** (code): see SPEC-4.
10. **Reconciliation** (`reconcile` job, different prompt persona): given all sibling specs' requirement lists, report contradictions and duplicated or missing responsibilities. Findings loop to the affected `component_spec` jobs, at most 2 rounds.
11. **Recursion.** Any child failing the size limits in lint is treated as a parent and the stages repeat for it, until `max_depth`. Children that pass become `leaf` nodes (kind set accordingly).
12. **Approval gate** `component-spec` (owner), per level or for the whole tree, per setting.

Requirements:

- SPEC-1. Each stage is a job kind with its own prompt file (X10), JSON schema (X1) and retry budget, and is individually re-runnable from the UI.
- SPEC-2. Output files are written into the workspace: root `SPEC.md` (architecture, shared types, scenarios), `SPEC-<subsystem>.md` per non-leaf child, and `interfaces.json` per parent. File layout and headings follow the decomposable-spec convention: components are `### <lowercase-name>` under `## Components`, requirements `- R1:` and edge cases `- E1:`, scenarios `### S1 <title>`.
- SPEC-3. Mechanical interface checks, each reported as a list of findings with node and field references: every consumed field has a producer or is an external input; every produced field has a consumer or is an external output; no dependency cycles among children; every call named in a scenario exists in a component's `provides`; every entity referenced exists; every component appears in at least one scenario; names are unique and lowercase-hyphenated.
- SPEC-4. Spec lint (a Python port of the decomposable-spec validator's checks): structure and headings, unique ids, vague wording ('fast', 'robust', 'handles errors' and similar), dependency names exist, no cycles, a component mentions only listed dependencies, requirement and function counts within X9 limits, at least one example per component.
- SPEC-5. The Contract JSON schema is a repository file with its own tests; stage 4 uses it for constrained decoding and for validation.
- SPEC-6. Owner edits: the owner can edit any spec or contract in the UI. Saving recomputes hashes (STO-5), marks dependents stale, and records an `edited` approval.
- SPEC-7. Brief intake: the pipeline starts from free text. An optional `clarify` step lets the reasoner propose up to 5 questions with defaults; the owner may answer or accept defaults; the language version and the dependency list (TENV-1) are always among the questions. Accepted defaults are recorded in an Assumptions section.
- SPEC-8. Spec-writing size ceiling: the component-spec stage records, per job, requirement count produced and lint failures, so EVAL-4 can find the largest component a local model can spec reliably.
- SPEC-9. A project can also be started from an existing hand-written or imported spec, skipping stages 1 to 3 (and 4 to 7 if it provides a contract), entering at lint.

## 10. SS-BUILD: build pipeline

Purpose: turn approved leaf specs into tested code, then integrate upward.

- BLD-1. **Context pack.** For each build job, Python assembles the prompt context: the node's spec text, the contract stubs of its direct dependencies (signatures and docstrings only, never implementations), the shared entity definitions it touches, the style guide and the environment block of TENV-4 for the target stack, and the allowed tool list. The model is not asked what to read. Packs are stored as artifacts and must fit within a configurable fraction of the coder context (default 0.5); if not, the node is flagged for further decomposition.
- BLD-2. **Contract stubs** (`contract_stub` job, coder or code generation): for every component, generate source files containing the signatures, types and docstrings from the frozen contract, raising `NotImplementedError`. Stubs are checked to import and type-check where the stack supports it.
- BLD-3. **Test authoring** (`author_tests` job, reasoner or coder, a separate call from implementation, from the spec only): produces tests with one or more tests per requirement id, named to include the id (for example `test_R3_...`). Tests are validated to run and to fail against the stubs (they must not pass against `NotImplementedError` stubs). On success tests are frozen: `frozen_hash` recorded, path marked read-only (CORE-3).
- BLD-4. **Traceability check** (code): every testable requirement id in the node appears in at least one test name; missing ids are reported and send the node back to BLD-3.
- BLD-5. **Implementation** (`implement` job, coder, via the v1 coder loop with the path policy): the coder writes only the node's own source files. Allowed tools are the node's allowlist.
- BLD-6. **Verification** (`verify` job, code): run the node's test command; the node passes only when the command exits 0, the gate's rules hold (CORE-4), frozen test hashes are unchanged, the traceability check passes, and the lint gate of TENV-3 passes. The result updates `TestRecord`.
- BLD-7. **Integration nodes.** After all children of a parent are `done`, an `integration` node runs that parent's scenario tests against the assembled children. Scenario tests are authored at spec time from the parent's scenarios (stage 6 output) by the same test-authoring rules, and frozen. Integration proceeds bottom-up to the root. Root integration passing means the project is `done`.
- BLD-8. **Contract change requests.** If a child cannot be implemented against its frozen contract, it creates a change request (what is missing and why). The parent node returns to `draft`, owner approval is required, and all dependents become stale.
- BLD-9. **Failure ladder** per node: (1) retry the implement job with the failing test output appended, up to `max_attempts_coder` (default 3); (2) a `diagnose` job (reasoner) reads the failure and produces a revised brief or a note naming a likely spec or test fault, then implement retries with the new brief, up to `max_attempts_diagnosed` (default 2); (3) status `escalated`: first a longer local reasoning pass with a larger token budget, then the owner (see ESC below). Test faults found by diagnose go to BLD-3 for re-authoring, with owner approval since tests are frozen.
- BLD-10. **Incremental rebuild.** When a node is `stale`, rebuild only it and its stale dependents, in dependency order. A node whose recomputed hash equals the hash it last passed with is marked `done` without rebuilding. The planner may auto-promote `stale → ready` only when `built_hash == content_hash` and frozen test files still exist; otherwise the stale node remains in the design/owner loop.
- BLD-11. **Review job** (optional setting, default off): the reasoner reviews finished code against the spec and lists mismatches; findings become new failing tests or change requests, never direct edits.
- BLD-12. **Final acceptance:** all nodes `done`, all frozen tests unchanged and passing, the root integration scenarios pass, and the project's own `--test-cmd` list (if any) exits 0 after the last change.
- ESC-1. Escalation never sends content anywhere by itself. An `escalated` node appears in the UI inbox with a local diagnosis, the failing output and the relevant files.
- ESC-2. The owner may resolve an escalation by editing the spec, the contract, or the code, then clicking retry.
- ESC-3. The UI can build an **escalation packet** (a single markdown file with spec section, contract, code, failing output) for the owner's own use.
- ESC-4. Packets are built only on explicit request, show a redaction preview where the owner can remove text, and are never transmitted by nai.

### Target environment (TENV, part of SS-BUILD)

Purpose: generated code must match the library and language versions it will actually run on, regardless of what the local model memorised during training. Written for Python; other stacks substitute their own tools through configuration.

- TENV-1. **Environment block.** Each project has a `target_env` setting: Python version (for example 3.12), operating system family, and a lockfile or requirements file with exact pinned versions of every third-party dependency. SPEC-7 asks for it at intake and defaults to the host Python and an empty dependency list. Dependencies change only by editing this block (UI or CLI). A node that needs an unpinned package raises a change request (BLD-8) instead of adding it silently.
- TENV-2. **Real environment.** The runner builds a virtual environment per project from the lockfile (or a Docker image, per D7), and every implement and verify command runs inside it. Test commands run with deprecation warnings treated as errors (for Python, `-W error::DeprecationWarning`, with a per-project allowlist the owner can extend), so use of removed or deprecated APIs fails with the library's own message, which goes into the retry prompt. Installing packages is the only permitted outbound network access besides the model server: it goes to the package index or a local wheelhouse or mirror, carries package names and versions only, and is replaced by a local wheelhouse when the project's `offline` setting is on (installation then fails if a wheel is missing).
- TENV-3. **Lint gate.** Verification (BLD-6) also runs a configured lint and type-check command set inside the environment. Default for Python: Ruff with the pyupgrade (`UP`) and bugbear (`B`) rule sets, plus a type check (mypy or pyright if installed). A non-zero exit fails verification, and the first N findings (default 20) are appended to the retry prompt.
- TENV-4. **Environment facts in the pack.** Every context pack (BLD-1) begins with a fixed block stating the language version, the pinned package list with versions, and the rule: use only these packages and write for these versions.
- TENV-5. **API notes.** A project may hold short notes per package in `.nai/apinotes/<package>.md` (an excerpt of the pinned version's documentation, or a short list of 'use X, not Y'). The context pack includes the notes for packages that the node's spec or contract mentions, up to a token cap (default 1500 tokens per node, configurable). Notes are written by the owner or drafted by a model and approved by the owner. nai never fetches documentation at runtime.
- TENV-6. **Staleness tracking.** A job that fails with an import error, an attribute error on a library object, or a deprecation warning records a `stale_api` tag with the package name on its event. The metrics page (WEB-6) shows counts per package, so the owner knows which API notes to write next.
- TENV-7. **Tests.** Environment creation, the warnings-as-errors behaviour, the lint gate and the notes inclusion are tested with a local wheel cache and no network access.

## 11. SS-TOOLS: tool library and MCP

Purpose: a collection of Python tools the models can call, defined once and served two ways.

- TLS-1. **Registry.** Tools are ordinary Python functions with type hints and docstrings, registered with a decorator giving name, family, side effects and timeout. The registry generates (a) OpenAI-style function schemas for the orchestration code and (b) MCP tool definitions. One definition, two consumers.
- TLS-2. **MCP server.** `nai mcp` serves the registry over stdio, and optionally over local HTTP bound to 127.0.0.1. Verified against a standard MCP client.
- TLS-3. **Allowlists.** Each job's context pack names the tools it may use (typically 3 to 5). Tools outside the list are not sent to the model and are refused if called.
- TLS-4. **Discovery for small models.** Two meta-tools exist, `list_tools(family)` and `describe_tool(name)`, used only when a job's allowlist includes them. A short `SKILL.md` per family explains conventions and is loaded on demand into the context pack.
- TLS-5. **Sandboxing.** Tools write only inside the project's `outputs/` directory (or another path the job's policy permits), refuse path escapes with `ERROR:`, have per-tool timeouts and output size limits, and make no network calls.
- TLS-6. **Initial tool families**, each with tests and example calls:
  - Markdown: render markdown to HTML; convert markdown to PDF; extract headings and tables.
  - Excel: create a workbook from structured data (sheets, header styles, number formats, formulas, simple charts); read a sheet to JSON or CSV; append rows; apply conditional formatting; recalculate and check for formula errors where possible.
  - Word: create a .docx from structured content (headings, paragraphs, lists, tables, images, page breaks, headers and footers); read text and tables from a .docx.
  - PDF: create a PDF from structured content or HTML; extract text and tables; merge, split, rotate; fill simple forms.
  - Data and files: CSV and JSON read, validate, transform; create charts as PNG; zip and unzip inside the workspace; diff two text files.
  - Project introspection: list spec nodes, read a node's contract stub, read the requirement list (read-only, for jobs that need it).
- TLS-7. Each tool returns a short human-readable string; where it creates a file it returns the relative path and size. Errors start with `ERROR:` (X3).
- TLS-8. Adding a tool is one file plus tests, with no other code changes; the UI tool catalog (WEB-7) lists it automatically.
- TLS-9. Optional third-party libraries (for example openpyxl, python-docx, reportlab, a markdown library, matplotlib) are declared per family and the tool reports `ERROR: dependency not installed: <name>` when missing, rather than failing at import time.

## 12. SS-WEB: web interface

Stack (default, see D1): FastAPI, server-rendered templates, HTMX and server-sent events, minimal JavaScript. Bound to 127.0.0.1 by default; optional bearer token for LAN use.

- WEB-1. **Project list:** name, status, node counts by status, percent done, last activity, current job, and a New Project dialog. The dialog collects name, a large description, language, interface type and optional product/engineering guardrails; blank options are omitted from the model's brief. A project starts in the `info_gathering` phase: the `intake` job asks focused product-manager questions, stores the answers, and decides when to begin design. The owner may force design at any time; if answers already exist, a forced `intake` job first folds them into a refined brief for the root node. Uploading an existing Markdown/text spec seeds the root spec directly, stores it as a `spec` artifact, and skips `info_gathering`.
- WEB-2. **Project page:** a spec tree (collapsible, status colour per node, counts per subtree), a header with progress, budget use, and the runner state.
- WEB-3. **Node page:** spec text (editable, with diff against the last approved version), contract view, requirements with their tests and last results (traceability matrix), jobs and attempts, artifacts, and buttons: approve, reject with note, retry, re-run stage, mark stale.
- WEB-4. **Approvals inbox:** all pending gates across projects, each showing exactly what to review (component map, interface table with scenario walk-through, specs) and the mechanical check findings.
- WEB-5. **Live log:** per project and per job, streaming via SSE: model thinking summaries (hidden by default), tool calls, command output, verification results.
- WEB-6. **Metrics page:** tokens and wall time per project, node and model; model swap count and time; success rate and retries per node kind; trend over time.
- WEB-7. **Tools page:** the registry catalog with schemas, family, last test result, and a try-it form that calls a tool with sample arguments in the sandbox.
- WEB-8. **Models page:** configured ModelProfiles, the server's `/v1/models` list, which model is loaded, health, and a button to run a quick smoke test and a latency probe. Reasoner/coder model ids are selected from the known model catalogue, with a fallback option for a configured id not currently listed.
- WEB-9. **Artifacts browser:** project files, outputs and logs with download, plus a markdown and code viewer.
- WEB-10. **Controls:** pause, resume, cancel, archive, restore, and priority reordering, implemented through the commands table (STO-3). A project-wide cancel moves the project to `cancelled`; archived projects are hidden from the default Projects page and available through a "Show archived" toggle.
- WEB-11. **Escalation inbox** per ESC-1 to ESC-4.
- WEB-12. **Settings:** model profiles, size limits, test commands, scheduling window, backup location.
- WEB-13. All state-changing actions are POST requests with CSRF protection; no endpoint executes arbitrary shell input from the browser. Same-origin POSTs are recognised by `Sec-Fetch-Site`, an exact configured `Origin` host, or the dashboard's `X-Requested-With: fetch` header for browsers/extensions that emit an opaque `Origin: null`.
- WEB-14. The UI works at laptop width and on a phone; accessible colour contrast; no external CDN assets (everything is served locally).
- WEB-15. A JSON API mirrors every page's data (`/api/...`) so scripts and other tools can read project state.

## 13. SS-EVAL: evaluation

- EVAL-1. **Benchmark specs.** A set of non-confidential specs at graded sizes and archetypes (library, CLI, web service, data pipeline, AI-powered feature), written by frontier models. Includes the v1 devswarm spec as a self-hosting benchmark.
- EVAL-2. **Stub model server:** a scripted OpenAI-compatible server for deterministic tests of every subsystem, as in the v1 scenarios, extended to serve streaming and constrained-JSON requests.
- EVAL-3. **Hold-out tests** live outside the workspace and the store. After a run they are applied to the output. Their pass rates, per component and per scenario, are the primary quality metric.
- EVAL-4. **Size-ceiling experiments:** for implementation and for spec-writing, run graded sizes (for example 3, 6, 10 requirements) and record first-pass success and average retries. Output: recommended X9 limits per model profile, stored with the profile.
- EVAL-5. **Regression runs:** any change to prompts, schemas, sampling or scheduler can be re-run on the benchmark set, and results compared against the previous run in the metrics page.
- EVAL-6. A single command (`nai bench`) runs a chosen benchmark set with a chosen ModelProfile and writes a report (JSON and markdown).
- EVAL-7. Reports record the model ids, quantisation notes entered by the owner, prompt versions (X10) and the git commit of nai.

## 14. Work packages for frontier conversations

Each row is one conversation (or a small number). Paste sections 0 to 5 plus the listed sections and inputs. Deliverables are runnable code with tests, a README, and a list of proposed changes to shared definitions.

| WP | Work | Paste | Extra inputs | Delivers | Needs first |
| --- | --- | --- | --- | --- | --- |
| WP1 | Core refactor and extensions | 6 | v1 `SPEC.md`, `devswarm.py` | `nai/` package, CORE tests, S1 to S6 passing | none |
| WP2 | Store | 7 | none | schema, migrations, status machine, hashing, export/import, tests | none (uses section 4) |
| WP3 | Scheduler and runner | 8 | WP1 and WP2 interfaces | runner, batching, commands, crash recovery, tests | WP1, WP2 |
| WP4 | Spec pipeline | 9 | WP1 to WP3 interfaces | stage jobs, schemas, prompts, checkers, lint, tests against the stub server | WP1, WP2, WP3 |
| WP5 | Build pipeline | 10 | WP1 to WP3 interfaces | context packs, stubs, test authoring, verification, integration, failure ladder | WP1, WP2, WP3 |
| WP6 | Tool registry, MCP, first tool families | 11 | none beyond registry rules | registry, MCP server, tools and tests | none |
| WP7 | Web v1 (read-only) | 12 (WEB-1, 2, 3 read parts, 5, 6, 8, 9, 15) | WP2 schema | app, templates, SSE, tests | WP2 |
| WP8 | Web v2 (controls, approvals, editing, escalation, tools page) | 12 (rest) | WP3, WP4, WP5, WP6 interfaces | complete UI | WP7 and others |
| WP9 | Evaluation harness and benchmark specs | 13 | all subsystem interfaces | stub server, `nai bench`, benchmark set, reports | WP1 for the stub, rest later |
| WP10 | Prompt engineering for local models | 9, 10, X1, X10 | transcripts and failures from real local runs (non-confidential benchmarks only) | revised prompt files with measured improvements | WP4, WP5, WP9 |

Suggested order: WP1, WP2 and WP6 in parallel; then WP3; then WP4, WP5 and WP7 in parallel; then WP9 and WP8; WP10 is iterative from then on. Before WP3 and later start, collect the interfaces produced by earlier packages into one short `INTERFACES.md` and paste it into the later conversations so they do not guess.

## 15. Open decisions (defaults in brackets)

- D1. Web stack. \[FastAPI + HTMX + SSE; change only if a conversation has a strong reason.\]
- D2. Candidate count for the root component map. \[3 at the root, 1 elsewhere.\]
- D3. Approval granularity for component specs. \[Per level, not per node.\]
- D4. Same-model parallel slots in llama-server for concurrent jobs of one role. \[Off; concurrency 1.\]
- D5. Target languages and stacks for generated programs. \[Python first; add one more (for example TypeScript) after the benchmarks pass on Python.\]
- D6. Whether consult-style side conversations (v1 `consult_reasoner`) remain. \[Kept only inside integration and diagnose jobs, with transcripts stored as artifacts.\]
- D7. Run generated code locally or in Docker by default. \[Docker if available, local otherwise; configurable per project.\]
- D8. Whether spec-writing and coding use the same reasoner model with different sampling. \[Same model id, separate sampling per job kind.\]
- D9. Structured-output method. \[JSON schema through llama-server where supported, otherwise grammar, otherwise validate and repair.\]

## 16. Future, not in scope now

Multi-user access, remote workers, fine-tuning on accumulated successful runs, a plugin system for third-party tool packs, voice or chat intake of briefs, automatic cost/benefit routing between several local models, support for more than one GPU.
