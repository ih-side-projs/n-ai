# Command execution

`nai/exec/` is the only place that spawns a child process. Everything else asks
a target to run a command, so the environment is scrubbed, limits are applied,
output is streamed with a cap, and cancellation actually stops the work.

## Modules

| Module | Responsibility |
|--------|----------------|
| `target.py` | `ExecutionTarget` protocol, `RunResult`, `Capabilities`, `OutputCapture`, `scrub_env`, `needs_shell`, `shell_argv` |
| `local.py` | `LocalTarget` — one process group, `prlimit`/`ulimit`, optional `unshare -rn` |
| `docker.py` | `DockerTarget` — named containers, `--network none`, `docker kill` |
| `__init__.py` | `make_target`, `capabilities_for`, re-exports |

## The contract

```python
result = target.run(
    argv,                 # list[str]; a string is only accepted through as_argv()
    cwd=None,             # defaults to the target root
    env_overrides=None,   # mapping, filtered like every other variable
    timeout=None,         # seconds; expiry exits 124
    cancel_event=None,    # threading.Event; cancellation exits 130
    output_cap=None,      # characters retained (head + tail)
    phase=PHASE_RUN,      # PHASE_SETUP or PHASE_RUN
    network=None,         # override the phase default
)
```

`RunResult` carries `argv`, `exit_code`, `output`, `duration`, `timed_out`,
`cancelled`, `truncated`, `mode`, `network`, `weaker_isolation`, `error` and
`notes`. `target.capabilities()` returns a `Capabilities` record of what the
target can actually enforce and `target.describe()` renders it for humans;
`nai doctor` reports it as the `execution-target` check.

## Selection

```python
from nai.exec import make_target

target = make_target(workspace_root, docker_image=settings.docker_image)
```

`docker_image` selects `DockerTarget`; otherwise `make_target` returns
`LocalTarget`. Both accept `on_note(event_type, message)` so a caller can record
degraded isolation in the event log, and `sink(chunk)` for live streaming.

## Environment scrubbing

The child sees only `ENV_ALLOWLIST` (`PATH`, `HOME`, `LANG`, `LC_ALL`,
`LC_CTYPE`, `TERM`, `TMPDIR`, `TZ`, `USER`, `LOGNAME`, `SHELL`) plus the
deterministic `DEFAULT_ENV` (`CI`, `PYTHONUNBUFFERED`, `GIT_TERMINAL_PROMPT=0`,
`PIP_NO_INPUT`, …). Adapter needs such as a venv `PATH` or `PYTHONPATH` are
passed as `env_overrides`, and every name is filtered through `is_secret()`,
which rejects `LLAMA_SWAP_KEY`, `NAI_*`, `*_TOKEN`, `*_PASSWORD`, `AWS_*` and
friends. Generated code never sees a credential.

## Two phases

`PHASE_SETUP` may use the network (toolchain installs); `PHASE_RUN` is
network-isolated. `DockerTarget` runs setup in a one-shot container with the
default bridge and keeps the `run` container at `--network none`. `LocalTarget`
wraps run-phase commands in `unshare -rn` when the kernel permits it; when it
does not, the command still runs but the result is marked
`weaker_isolation=True`, a note is emitted once, and `capabilities()` reports
`network_off=False`.

## Limits and cancellation

| Control | `LocalTarget` | `DockerTarget` |
|---------|---------------|----------------|
| memory | `prlimit --as` (or `ulimit -v`) | `--memory` |
| CPU | `prlimit --cpu` (or `ulimit -t`) | `--cpus` |
| processes | `prlimit --nproc` inside a user namespace | `--pids-limit` |
| privileges | user namespace from `unshare -rn` | `--cap-drop ALL --security-opt no-new-privileges --user uid:gid` |
| filesystem | host paths | bind-mounted workspace |
| cancel | `os.killpg` on the process group | `docker kill` on the named container |

Cancellation is checked before the exit status, so a kill that races with a
normal exit is still reported as cancelled. Output is read on a thread and
retained as a head+tail window (`OutputCapture`) instead of buffering with
`communicate()`, so a chatty command cannot exhaust memory.

## Shell steps

`bash -lc` is not used: a login shell reads profiles and changes the
environment unpredictably. Pipeline steps pass argv. The model-facing
`run_command` tool is a shell step by definition and uses
`bash -o pipefail -c`; `needs_shell()` decides whether a string command really
needs a shell (unquoted `|`, `>`, `&&`, `$(` …) before that wrapper is applied.
