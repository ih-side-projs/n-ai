# Frequently asked questions

## Is nai a cloud service?

No. It runs entirely on your machine. The only runtime network destination is
the local model server you configure. There is no telemetry and no account.

## Do I need a GPU to work on nai?

No for development and tests — the suite uses scripted stub servers. A GPU is
needed only to run the pipeline against real local models (the reference setup
uses an AMD RX 6600 8 GB with llama-swap).

## Which models are recommended?

Qwen-family models: `qwen3.6-reasoning` for spec/judgment jobs and
`qwen3-coder` for implementation. Model ids are configurable per profile; no
code hard-codes them.

## Can I use a different model server?

Any server implementing the used OpenAI-compatible endpoints
(`/v1/chat/completions`, `/v1/models`, optionally `/running`) should work.
Constrained-output support determines which D9 mode is used.

## What happens when the model returns invalid JSON?

`call_structured_json()` validates against the job schema and sends a repair
prompt containing the validation error, up to three rounds. If all modes fail,
the job fails with a recorded error — code never consumes unchecked model output.

## Why are prompts in text files?

X10: versioned prompt files make prompt changes reviewable and let every job
record the exact prompt id it used. See `nai/spec/prompts/` and `nai/build/prompts/`.

## Why can't the web app write to the database directly?

STO-3: the runner is the only writer. The web app uses read-only connections and
enqueues commands. This keeps a single auditable writer and makes crash recovery
straightforward.

## How do I add a tool?

Create one module under `nai/tools/families/`, decorate functions with
`@register_tool`, add tests, and import the module from `families/__init__.py`.
No other code changes are needed. See [Tools](subsystems/tools.md).

## How do I approve a specification?

Use the dashboard's `/approvals` inbox or the node page. Approval records are
stored with stage and note, then the node transitions to `approved`. Recursion to
children only proceeds after the parent's interfaces are approved.

## What are X9 limits?

Configurable size ceilings: requirements per component (8), public functions
per component (10), components per level (7) and depth (4). They keep leaf jobs
small enough for local models. Tune them with benchmarks (EVAL-4).

## How do I measure whether a prompt change helped?

Run `nai-bench` before and after, with the same profile and quantization, and
compare first-pass success, retries and duration. Reports include the git commit
so results are reproducible. See [Evaluation](subsystems/evaluation.md).

## Is the v1 `devswarm` command still supported?

Yes. `python devswarm.py SPEC.md ...` and `python -m nai SPEC.md ...` both work
and share the same code. The S1–S6 scenarios test both entry points.

## Where do I report a security issue?

Privately, as described in [SECURITY.md](../SECURITY.md). Do not open a public
issue for vulnerabilities.
