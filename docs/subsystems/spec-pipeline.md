# SS-SPEC

The specification pipeline generates and checks a layered spec tree before any
code is written. It registers job handlers as scheduler plugins.

## Stages

| # | Job | Purpose |
|---|-----|---------|
| -1 | `intake` | Product-manager questions and refined brief before design (WEB-1) |

The decomposed component map is also stored in project settings so that the
later interfaces contract can replace the root contract without losing the
component names needed by `component_spec`.
| 1 | `decompose` | Component map (children, shared entities, external I/O) |
| 2 | `select_map` | Optional candidate selection/merge (D2: 3 at root, 1 elsewhere) |
| 3 | `interfaces` | Contract: entities, components, scenarios |
| 4 | `check_interfaces` | Walk scenarios; find missing providers/calls/entities |
| 5 | `component_spec` | Per-child spec markdown, requirements, edge cases, example |
| 6 | `lint` | Mechanical validation (SPEC-4) |
| 7–11 | reconcile / recursion | Sibling reconciliation and recursion to X9 `max_depth` |
| 0 | `clarify` | Optional clarification questions |

## Structured outputs (X1)

Every model job calls `call_structured_json()` with a schema from
`nai/spec/schemas.py`. The D9 chain is JSON-schema `response_format` → GBNF
`grammar` → validate-only; validation always runs in Python (with `jsonschema`).
Failures trigger up to three repair rounds with the validation error in the
prompt. Prompts are versioned files in `nai/spec/prompts/`.

## Scenario step schema

A scenario step has **exactly four properties**; `additionalProperties` is false:

```json
{
  "caller": "auth",
  "callee": "orders",
  "call": "create_order",
  "fields": ["User.id", "Order.total"]
}
```

`caller` and `callee` are component ids (or `user`/`external`), `call` is the
function name without parentheses, and `fields` is an array of `Entity.field`
references. There are no free-text fallbacks: constrained decoding enforces the
shape and the mechanical checker consumes the explicit values.

## Checkers

```python
from nai.spec.checkers import check_interfaces, lint_spec

findings = check_interfaces(contract)
```

`check_interfaces` verifies caller/callee existence, that `call` appears in the
callee's `provides`, that field entities exist and are producible, dependency
cycles and component naming. `lint_spec` checks structure, unique requirement
and edge-case ids, vague wording, dependencies against the contract and X9
limits.

## Schemas source of truth

`nai/spec/schemas.py` defines the schemas in Python. The JSON files in
`nai/spec/schemas/` are generated artifacts:

```bash
python -m nai.spec.schemas
```

CI verifies the generated files are in sync.

## Size limits

X9 values (`max_components_per_level=7`, `max_depth=4`,
`max_requirements_per_component=8`, `max_public_functions_per_component=10`)
are read from project settings, then `[limits]` in TOML, then defaults. The old
hard-coded `3 if root else 1` limit was removed; D2 candidate counts are
separate fields in the same config table.

Tests: `tests/spec/` (checkers, schemas, structured-output fallback).
