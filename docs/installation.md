# Installation

## Requirements

- **Python 3.10+** (tested on 3.12 and 3.14; CI covers 3.10–3.12)
- **Linux, macOS or WSL** (Windows-native is a non-goal)
- Optional: a local OpenAI-compatible model server (llama-swap in front of llama-server)

No GPU is needed for development or tests — the suite uses a scripted stub server.

## Install from source

```bash
git clone https://github.com/ih-side-projs/n-ai.git
cd n-ai
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev,web,eval]'
```

Extras:

| Extra | Contents |
|-------|----------|
| `dev` | pytest, pytest-asyncio, httpx |
| `web` | FastAPI, Jinja2, Mistune, Bleach, uvicorn (+ core web deps) |
| `eval` | pydantic for the benchmark stub server |
| `markdown`, `excel`, `word`, `pdf`, `charts` | optional tool families |

`openai`, `jsonschema`, `httpx`, `fastapi`, `jinja2`, `mistune`, `bleach` and
`python-multipart` are installed as core dependencies.

## Verify the installation

```bash
pytest -q
# 485 passed, 1 skipped

nai --help
nai run --help
nai bench --list
python devswarm.py --help
```

## Configure the local model server

nai expects an OpenAI-compatible API:

- `POST /v1/chat/completions` (tool calls, streaming, JSON schema or grammar)
- `GET /v1/models`
- Optional `GET /running` from llama-swap (used for model swap detection)

Point nai at it with either the environment variable or config:

```bash
export LLAMA_SWAP_URL=http://127.0.0.1:8080/v1
export LLAMA_SWAP_KEY=none          # only if your server needs a key
```

Or in `nai.toml`:

```toml
[server]
base_url = "http://127.0.0.1:8080/v1"
api_key = "none"
```

Recommended models (Qwen family, ~32k context):

- reasoner: `qwen3.6-reasoning`
- coder: `qwen3-coder`

See [Configuration](configuration.md) for profile overrides.

## Optional: Docker command mode

The build pipeline can run generated code in a container:

```bash
nai run --plugin nai.build ...        # commands from job context
python devswarm.py SPEC.md --docker python:3.12
```

Docker mode is hardened: commands run in a named container with `--network none`
(run phase), memory/CPU/process caps, `--cap-drop ALL`,
`--security-opt no-new-privileges` and a non-root user; setup installs run in a
separate one-shot container with network access. See
[Command execution](subsystems/execution.md). Tests drive Docker mode through a
fake `docker` executable, so no daemon is required for development; a single
real-daemon round-trip test skips when `docker` is unavailable.

## Optional: remote build workers

Platform-specific (`macos`) components can build and test on another machine
over the LAN; Linux keeps the store, the model calls and result parsing. On the
orchestrator, `nai worker add macbook --capabilities macos,python` prints a token
once and `nai remote serve --host 0.0.0.0 --insecure-lan` exposes the worker API
on a trusted network (prefer `--certfile/--keyfile`). On the Mac, install the
package, run `nai-worker init --name macbook --server URL --token TOKEN`, check
with `nai-worker doctor`, then `nai-worker run` (or load
`deploy/nai-worker.plist`). See [Remote workers](subsystems/remote-workers.md).

## Development install for docs

Documentation is plain Markdown; to preview it with MkDocs:

```bash
pip install mkdocs mkdocs-material
mkdocs serve
```

See the repository [CONTRIBUTING.md](../CONTRIBUTING.md) for the development workflow.
