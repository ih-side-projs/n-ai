# Troubleshooting

## Subsystems pass tests but the integrated system does not

Run the offline integration self-check first:

```bash
nai doctor
```

It reports failed imports, missing CLI subcommands, planner/handler API drift,
invalid artifact kinds, stale schema artifacts, missing packaged data, store
migration failures and tool-registry gaps. Use `nai doctor --json` for CI logs.

## `ModuleNotFoundError` for `fastapi`, `jsonschema`, etc.

Install all extras used in development:

```bash
pip install -e '.[dev,web,eval]'
```

Core dependencies include `openai`, `fastapi`, `httpx`, `jinja2`, `mistune`,
`bleach`, `jsonschema` and `python-multipart`. `pydantic` comes with `eval`.

## `Missing dependency: pip install openai`

The LLM module imports `openai` lazily; a real model call needs it. Tests do not.
Install with `pip install openai` or reinstall the project.

## Tests fail with "Form data requires python-multipart"

FastAPI needs `python-multipart` for POST form endpoints. Install the project
again with web/eval extras (it is now a core dependency).

## `nai` cannot reach the model server

- Confirm `LLAMA_SWAP_URL` ends with `/v1` (e.g. `http://127.0.0.1:8080/v1`).
- Check `curl $LLAMA_SWAP_URL/models` returns JSON.
- If using llama-swap, check `GET /running` for swap detection.
- Proxy variables can intercept loopback traffic; unset `HTTP_PROXY`/`HTTPS_PROXY`
  or set `NO_PROXY=127.0.0.1,localhost`.

## JSON parsing fails with `<think>` text in the response

`call_structured_json()` strips leading `<think>...</think>` blocks and markdown
fences before `json.loads`. If your server emits long reasoning *after* the JSON
or splits it across fields, check that llama-server separates reasoning into
`reasoning_content` and that streamed content is used for parsing. The call log
(`--call-log FILE`) contains the full response for diagnosis.

## "unsupported response_format" from the model server

This is expected on older llama-server builds. The D9 chain automatically falls
back to GBNF grammar, then validate-only mode. If all three fail, verify the
schema is valid draft-07 and inspect the repair messages in the call log.

## Components limited to one child

That was the old bug mixing D2 candidate counts with X9 limits. Current defaults
come from `[limits] max_components_per_level = 7` in `nai.toml` or the project's
`settings`; D2 candidates (`candidates_root`, `candidates_nonroot`) are separate.

## Interface checker reports missing provider/call

Scenario steps must use exactly `caller`, `callee`, `call` and `fields`.
Free-text `actor`/`action`/`expects` steps are rejected by the schema. Ensure:

- `call` matches a name in the callee's `provides`,
- every `Entity.field` in `fields` belongs to a known entity,
- the entity is an external input or is returned by a function of a used callee.

## Web dashboard shows a bearer-token login page

`NAI_WEB_TOKEN` is set. Supply the token in the form, as `Authorization: Bearer …`,
or via a `nai_auth` cookie. Unset the variable for fully local single-user use.

## CSRF errors on dashboard actions

Load the page first (it sets the `nai_csrf` cookie) and include the token in the
form, or send `X-Csrf-Token` with fetch requests. Requests must be same-origin.

## Files not visible in the workspace browser

The browser ignores hidden files, symlinks and directories named `.git`, `.nai`,
`__pycache__`, `node_modules`, `venv`, `.venv`, `env`, `.env`. Registered
artifacts under `.nai/artifacts` are available via the artifacts page instead.

## Schema JSON files out of sync

`nai/spec/schemas.py` is the source of truth. Regenerate artifacts:

```bash
python -m nai.spec.schemas
git diff --exit-code nai/spec/schemas/
```

## Still stuck?

Open an issue with the [bug report template](https://github.com/ih-side-projs/n-ai/issues/new/choose)
and include Python version, command, config, and a redacted call log.
