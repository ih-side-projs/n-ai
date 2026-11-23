"""Info Gathering handler and planner tests."""
from __future__ import annotations

from types import SimpleNamespace

from nai.spec import jobs as spec_jobs
from nai.spec import plugin as spec_plugin


class _Store:
    def __init__(self):
        self.settings = {}
        self.created_jobs = []
        self.spec_text = None
        self.nodes = [{"id": "root", "name": "root", "status": "draft", "parent_id": None}]
        self._seq = 0

    def update_project(self, project_id, *, settings):
        self.settings = settings

    def list_nodes(self, project_id, parent_id=None):
        return [n for n in self.nodes if n.get("parent_id") == parent_id]

    def create_node(self, project_id, name, kind, *, parent_id=None, objective="", depends_on=()):
        self._seq += 1
        node = {"id": f"n{self._seq}", "name": name, "kind": kind, "parent_id": parent_id,
                "status": "draft", "objective": objective, "depends_on": list(depends_on)}
        self.nodes.append(node)
        return node["id"]

    def transition_node_status(self, node_id, status, note=""):
        node = next(n for n in self.nodes if n["id"] == node_id)
        node["status"] = status

    def get_node(self, node_id):
        return next((n for n in self.nodes if n["id"] == node_id), None)

    def update_node_spec(self, node_id, text):
        self.spec_text = text

    def create_job(self, project_id, kind, role, *, node_id=None, attempt=1, input_ref=None):
        self.created_jobs.append(kind)
        return f"j-{kind}"


class _PC:
    def __init__(self, project, jobs=None):
        self.store = _Store()
        self.project = project
        self.nodes = [{"id": "root", "name": "root", "status": "draft", "parent_id": None}]
        self.jobs = jobs or []

    def active_jobs(self, node_id):
        return [j for j in self.jobs if j.get("node_id") == node_id]


def _ctx(store, settings):
    events = []
    return SimpleNamespace(
        check=lambda: None,
        project={"id": "p", "brief": "Build a thing", "settings": settings},
        store=store,
        job={"id": "j", "input_ref": {}},
        event=lambda *a, **kw: events.append((a, kw)),
    ), events


def test_intake_handler_stores_questions_and_waits(monkeypatch):
    store = _Store()
    ctx, events = _ctx(store, {"intake": {"description": "Build a thing"}, "intake_complete": False})
    monkeypatch.setattr(spec_jobs, "_call_structured", lambda *a, **kw: {
        "ready_for_design": False,
        "questions": [{"id": "Q1", "question": "Who will use it?", "why": "Audience changes scope"}],
        "readiness_summary": "Need audience",
        "refined_brief": "A thing for users",
        "guardrails": {"target_users": ""},
    })

    result = spec_jobs.handle_intake(ctx)

    assert result.get("ready_for_design") is False
    assert store.settings["intake_waiting"] is True
    assert store.settings["intake_complete"] is False
    assert store.settings["intake_questions"][0]["id"] == "Q1"
    assert store.spec_text is None
    assert any(args[0] == "spec.intake_questions" for args, _ in events)


def test_intake_handler_refines_brief_and_enters_design_when_ready(monkeypatch):
    store = _Store()
    ctx, events = _ctx(store, {"intake": {"description": "Build a thing"}, "intake_complete": False})
    monkeypatch.setattr(spec_jobs, "_call_structured", lambda *a, **kw: {
        "ready_for_design": True,
        "questions": [],
        "readiness_summary": "Enough detail",
        "refined_brief": "Build a CLI for developers that manages tasks",
        "guardrails": {"language": "Python", "interface_type": "CLI"},
    })

    result = spec_jobs.handle_intake(ctx)

    assert result.get("ready_for_design") is True
    assert store.settings["intake_complete"] is True
    assert store.settings["clarified"] is True
    assert store.settings["intake_waiting"] is False
    assert store.spec_text.startswith("Build a CLI")
    assert any(args[0] == "spec.intake_ready" for args, _ in events)


def test_forced_intake_writes_refined_brief_even_if_model_would_ask(monkeypatch):
    store = _Store()
    ctx, _ = _ctx(store, {"intake": {"description": "x"}, "intake_complete": False})
    ctx.job["input_ref"] = {"force_refine": True}
    monkeypatch.setattr(spec_jobs, "_call_structured", lambda *a, **kw: {
        "ready_for_design": False,
        "questions": [{"id": "Q1", "question": "Who is it for?", "why": "scope"}],
        "readiness_summary": "Forced by owner",
        "refined_brief": "Refined brief from the owner's answers",
        "guardrails": {},
    })

    result = spec_jobs.handle_intake(ctx)

    assert result.get("ready_for_design") is True
    assert result.get("questions") == []
    assert store.settings["intake_complete"] is True
    assert store.spec_text == "Refined brief from the owner's answers"


def test_planner_queues_intake_once_then_waits_for_owner():
    project = {"id": "p", "settings": {"intake": {"description": "x"}, "intake_complete": False}}
    pc = _PC(project)
    spec_plugin.plan(pc)
    assert pc.store.created_jobs == ["intake"]

    project["settings"]["intake_waiting"] = True
    pc2 = _PC(project)
    spec_plugin.plan(pc2)
    assert pc2.store.created_jobs == []


def test_planner_resumes_spec_pipeline_after_intake_completes():
    project = {"id": "p", "settings": {"intake": {"description": "x"}, "intake_complete": True, "clarified": True}}
    pc = _PC(project)
    spec_plugin.plan(pc)
    assert pc.store.created_jobs == ["decompose"]


def test_ensure_component_children_resolves_names_to_node_ids():
    store = _Store()
    component_map = {"components": [
        {"name": "a", "objective": "A", "depends_on": []},
        {"name": "b", "objective": "B", "depends_on": ["a"]},
        {"name": "c", "objective": "C", "depends_on": ["b"]},
    ]}
    created, unresolved = spec_jobs.ensure_component_children(store, "p", "root", component_map)
    assert unresolved == []
    assert len(created) == 3
    children = store.list_nodes("p", parent_id="root")
    by_name = {n["name"]: n for n in children}
    assert by_name["b"]["depends_on"] == [by_name["a"]["id"]]
    assert by_name["c"]["depends_on"] == [by_name["b"]["id"]]


def test_planner_moves_written_spec_to_review_instead_of_requeueing():
    store = _Store()
    child = {"id": "child", "name": "child", "status": "draft", "parent_id": "root",
             "spec_text": "# done", "contract": {}, "kind": "component"}
    store.nodes.append(child)
    project = {"id": "p", "settings": {"intake_complete": True}}
    store.nodes[0]["contract"] = {"components": []}
    pc = SimpleNamespace(store=store, project=project, nodes=[store.nodes[0], child], jobs=[])
    pc.active_jobs = lambda node_id: []

    spec_plugin.plan(pc)

    assert store.created_jobs == []
    assert store.get_node(child["id"])["status"] == "awaiting_approval"

