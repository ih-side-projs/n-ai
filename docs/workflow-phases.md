# Workflow phases: design and implementation

nai's project workflow has two named phases. They are **derived from existing
state**, not stored as a separate flag, and they may **overlap** at the project
level.

## The two phases

| Phase | What happens | Node statuses | Job kinds |
|-------|--------------|---------------|-----------|
| **Design** | generating and approving the spec, requirements, sub-specs and contracts | `draft`, `awaiting_approval`, `approved` | `clarify`, `decompose`, `select_map`, `interfaces`, `check_interfaces`, `component_spec`, `lint`, `reconcile` |
| **Implementation** | coding, testing, verification and integration | `ready`, `running`, `verifying`, `done`, `failed`, `escalated`, `blocked` | `contract_stub`, `author_tests`, `implement`, `verify`, `integrate`, `diagnose`, `review` |

`stale` nodes are disambiguated by their most recent job kind (falls back to
design). A project with nodes in both phases is reported as **mixed** — starting
code on one component does not mean design must be finished for the rest.

## API

```python
from nai.workflow import node_phase, phase_summary, filter_tree_to_phase

phase = node_phase(node, jobs)          # "design" or "implementation"
summary = phase_summary(nodes, jobs_by_node)
# {"phase": "mixed", "design_count": 3, "implementation_count": 2, "per_node": {...}}
design_tree = filter_tree_to_phase(tree, "design", summary["per_node"])
```

The web API exposes it at:

- `GET /api/projects/{id}/phase` — phase summary, per-node map and filtered tree
- `GET /api/projects/{id}/phase?phase=design` (or `implementation`)
- `GET /api/projects/{id}/nodes/{nid}` now includes `phase` and `phase_label`

## Web interface

The project page has three tabs: **All**, **Design**, **Implementation**, with
live counts. Selecting Design or Implementation:

- filters the specification tree to nodes in that phase (ancestors of matching
  nodes are shown as *context*),
- shows a prominent banner explaining what that phase means,
- marks every node with a phase badge.

Node pages also show a phase badge and banner, so it is always clear whether you
are editing design artefacts or watching implementation. The project list shows
the aggregate phase badge (`design`, `implementation` or `mixed`).

No schema change or migration was needed: the phase is a projection of node
status and job history, so it can never drift out of sync with the pipeline.

## Design decision

We chose derivation over an explicit stored phase because the runner already
owns the authoritative state machine. Adding a parallel `phase` column would
create a second source of truth that could disagree with node statuses. The
trade-off is that a node whose status is ambiguous (`stale` with no jobs) is
classified as design — which matches the next action (revisiting its spec).
