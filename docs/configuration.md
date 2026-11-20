# Configuration

nai follows a single precedence rule (X7):

```
CLI option  >  environment variable  >  TOML file  >  built-in default
```

## Finding the config file

1. `--config FILE` on the CLI
2. `NAI_CONFIG` environment variable
3. `./nai.toml` in the current directory (if present)
4. Built-in defaults

## Full example

```toml
default_profile = "default"

[server]
base_url = "http://127.0.0.1:8080/v1"
api_key = "none"
timeout = 3600
stream = true
swap_threshold_seconds = 10.0

[profiles.default]
swap_cost_seconds = 25

[profiles.default.reasoner]
model_id = "qwen3.6-reasoning"
ctx = 32768
max_tokens = 16384
temperature = 0.6
top_p = 0.95
top_k = 20

[profiles.default.coder]
model_id = "qwen3-coder"
ctx = 32768
max_tokens = 16384
repeat_penalty = 1.05      # unknown keys are forwarded as sampling parameters

[limits]
max_requirements_per_component = 8
max_public_functions_per_component = 10
max_components_per_level = 7
max_depth = 4
candidates_root = 3        # D2 candidate count, distinct from X9
candidates_nonroot = 1

[runner]
poll_seconds = 1.0
plan_interval_seconds = 2.0
heartbeat_seconds = 2.0
concurrency = 1
max_consecutive_jobs = 25
max_batch_seconds = 1800
windows = []               # e.g. ["22:00-06:00"]
plugins = ["nai.spec", "nai.build"]
threaded = true
record_call_artifacts = true
record_log_events = true
max_log_events_per_job = 2000
require_parent_contract_approval = true

[runner.budgets]
max_attempts = 8
max_wall_seconds = 7200
max_tokens = 2000000
```

## Environment variables

| Variable | Purpose |
|----------|---------|
| `LLAMA_SWAP_URL` | OpenAI-compatible base URL |
| `LLAMA_SWAP_KEY` | API key for the local server |
| `REASONER_MODEL` | Override reasoner model id |
| `CODER_MODEL` | Override coder model id |
| `NAI_CONFIG` | Path to TOML config |
| `NAI_DB_PATH` | Store path used by the web app |
| `NAI_WEB_TOKEN` | Optional bearer token for the dashboard |
| `NAI_WEB_HOST` / `NAI_WEB_PORT` | Dashboard bind address (default `127.0.0.1:8080`) |
| `NAI_RUNNER_STATUS_FILE` | Runner heartbeat JSON (default `<db dir>/runner-status.json`) |
| `NAI_LLAMA_SWAP_URL` / `NAI_LLAMA_SWAP_KEY` | Dashboard-specific model server overrides |
| `NAI_WEB_EVENT_POLL_SECONDS` | SSE polling interval (default 1.0) |

## Per-project settings

The store's `projects.settings` JSON can override X9 limits for one project
(`max_components_per_level`, `max_depth`, …), plus `target_env` and
`budget_limit_tokens`. Project values override the TOML `[limits]` table.

## Structured-output method (D9)

`nai.spec.structured.call_structured_json()` tries, in order:

1. JSON-schema `response_format`
2. GBNF `grammar` generated from the schema
3. Validate-only mode with the schema included in the prompt

A rejected mode is switched immediately rather than retried. Validation always
runs in Python, with `jsonschema` when available and a conservative built-in
validator otherwise.

Next: [CLI reference](cli-reference.md).
