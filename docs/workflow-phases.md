# Workflow phases: Info Gathering, design and implementation

nai's project workflow has three named phases:

| Phase | What happens | Scope | Job kinds |
|-------|--------------|-------|-----------|
| **Info Gathering** | structured intake; the model asks focused product-manager questions and the owner answers them | project | `intake` |
| **Design** | generating and approving the spec, requirements, sub-specs and contracts | nodes | `clarify`, `decompose`, `select_map`, `interfaces`, `check_interfaces`, `component_spec`, `lint`, `reconcile` |
| **Implementation** | coding, testing, verification and integration | nodes | `contract_stub`, `author_tests`, `implement`, `verify`, `integrate`, `diagnose`, `review` |

Design and implementation are **derived from node state** and may overlap at the
project level. A project with nodes in both is reported as **mixed**.

Info Gathering is a project-level gate stored in project settings:

```json
{
  "intake": {"description": "...", "language": "Python 3.12", "interface_type": "CLI"},
  "intake_complete": false,
  "intake_waiting": false
}
```

The `intake` job stores questions in `intake_questions`. Submitted answers are
merged into `intake.answers`, which clears `intake_waiting` so the planner can
run another intake round. The model sets `intake_complete` when it has enough
information. The owner can force design with **Begin design / spec-ing now**. If
answers already exist, forcing design enqueues one final `intake` job with
`force_refine=true` so the reasoner folds those answers into the root node's
refined brief before design jobs are allowed to run.

`stale` nodes are disambiguated by their most recent job kind (falls back to
design). A node whose most recent job is `intake` is also design-phase.

## API

```python
from nai.workflow import (
    node_phase, phase_summary, project_phase_summary, filter_tree_to_phase,
)

phase = node_phase(node, jobs)          # "design" or "implementation"
summary = phase_summary(nodes, jobs_by_node)
project_summary = project_phase_summary(nodes, jobs_by_node, project)
# project_summary["phase"] is "info_gathering", "design", "implementation" or "mixed"
design_tree = filter_tree_to_phase(tree, "design", summary["per_node"])
```

The web API exposes it at:

- `GET /api/projects/{id}/phase` — phase summary, per-node map and filtered tree
- `GET /api/projects/{id}/phase?phase=design` (or `implementation`)
- `GET /api/projects/{id}/nodes/{nid}` includes `phase` and `phase_label`

## Web interface

The project page has **All**, **Design** and **Implementation** tabs, plus an
**Info Gathering** tab while that gate is active. Selecting Design or
Implementation filters the specification tree (ancestors of matching nodes are
shown as context) and shows an explanatory banner.

While Info Gathering is active, the project page shows the original guardrails,
the model's readiness summary, an answer form for each pending question, and a
button to force design. Submitted questions and answers are shown read-only
until the model asks the next round. Every project page also shows a **Next
step** panel; after intake completes it points to the Design tab or the
appropriate approval/escalation/log view.

## Design decision

Design and implementation remain derived from node status and job history so
they cannot drift from the runner's authoritative state machine. Info Gathering
is project-level and lives in settings because it precedes any nodes in the
spec tree; it is still changed only through queued commands.
