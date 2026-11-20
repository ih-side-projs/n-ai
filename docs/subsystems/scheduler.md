# SS-SCHED

The scheduler and runner: chooses jobs, batches by model, enforces budgets,
recovers from crashes and exposes a plugin registry for job handlers.

## Starting the runner

```bash
nai run --db .nai/nai.sqlite3 --poll 1 --concurrency 1
```

`--drain` exits when the queue is empty; `--window` and `--until` restrict when
new jobs may start (SCH-8). Ctrl-C once stops gracefully; twice cancels and
requeues running jobs.

## Public API

```python
from nai.sched import Runner, SchedConfig, HandlerRegistry, JobResult, read_health

class Handler:
    def run(self, ctx):
        ctx.check()                     # cancellation / budget / deadline
        ctx.event("custom.step", "doing work", level="info")
        return JobResult.success(text="done")

runner = Runner(store, llm, sched=SchedConfig(concurrency=1))
runner.registry.register("custom", Handler(), uses_model=False)
stats = runner.run(drain=True)
```

`ctx` is `PlanContext`/`RunContext` with `node`, `project`, `job`, `store`,
`client`, `ws`, `check()`, `event()` and `artifact()`.

## Plugins

Default plugins are `nai.spec` and `nai.build`; both expose `register(runner)`.
Pass `--plugin MODULE` to replace the list. A plugin registers handlers by job
kind via `runner.registry.register(kind, handler, uses_model=True|False)`.

## Batching and budgets

- Jobs are grouped by model role/id to minimise model loads; `--max-consecutive`
  and `--max-batch-seconds` stop a long run on one model.
- Budgets (`[runner.budgets]`) cap attempts, wall seconds and tokens per node;
  exceeding a budget fails or escalates the node (X5).
- Handler exceptions are recorded as job errors; transient server failures are
  requeued with exponential backoff.

## Crash recovery (SCH-7)

On startup `Runner.recover()`:

1. Requeues jobs left `running` and emits `runner.recovered_job`.
2. Reports nodes in `running`/`verifying` with no active job as orphans.
3. Restores pause state from the store/events and writes a heartbeat file.

`runner-status.json` is consumed by the web dashboard (`/api/runner`).

## Health and locking

`read_health()` reads the heartbeat; `RunnerAlreadyRunning` is raised if the
lock file is held by a live process. The lock is released on exit.

Tests: `tests/sched/` (pure logic, CLI, commands, lifecycle, budgets, store contracts).
