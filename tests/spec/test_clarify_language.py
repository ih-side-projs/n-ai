"""Clarify escalates unsupported language requests instead of substituting."""

from __future__ import annotations

from types import SimpleNamespace

from nai.spec import jobs as spec_jobs


class _Store:
    def __init__(self) -> None:
        self.settings: dict | None = None

    def update_project(self, project_id: str, *, settings: dict) -> None:
        self.settings = settings


def _ctx(store: _Store, brief: str, settings: dict | None = None):
    events: list[tuple] = []
    ctx = SimpleNamespace(
        check=lambda: None,
        project={"id": "p1", "brief": brief, "settings": settings or {}},
        store=store,
        job={"id": "j1", "input_ref": {}},
        event=lambda *args, **kwargs: events.append((args, kwargs)),
    )
    return ctx, events


def _clarify_result(*questions: dict) -> dict:
    return {"questions": list(questions)}


def test_clarify_asks_when_the_brief_requests_an_unsupported_language(monkeypatch):
    monkeypatch.setattr(
        spec_jobs,
        "_call_structured",
        lambda *a, **kw: _clarify_result(
            {"id": "Q1", "question": "Which Python version?", "default_answer": "Python 3.12", "category": "language"},
            {"id": "Q2", "question": "What is the CLI called?", "default_answer": "tool", "category": "scope"},
        ),
    )
    store = _Store()
    ctx, events = _ctx(store, "Build a Kotlin CLI for parsing logs")

    result = spec_jobs.handle_clarify(ctx)

    questions = result["questions"]
    assert questions[0]["id"] == "Q1"
    assert questions[0]["category"] == "language"
    assert "Kotlin" in questions[0]["question"]
    assert "python" in questions[0]["question"]
    assert [q["id"] for q in questions] == ["Q1", "Q2"]
    assert store.settings is not None
    assert store.settings["language_request"] == ["Kotlin"]
    # Never substitute a language: the project setting is untouched.
    assert "language" not in store.settings
    assert any(args[0] == "spec.unsupported_language" for args, _ in events)


def test_clarify_keeps_the_model_questions_for_a_supported_language(monkeypatch):
    monkeypatch.setattr(
        spec_jobs,
        "_call_structured",
        lambda *a, **kw: _clarify_result(
            {"id": "Q1", "question": "Which Python version?", "default_answer": "Python 3.12", "category": "language"},
        ),
    )
    store = _Store()
    ctx, events = _ctx(store, "Build a Python CLI for parsing logs")

    result = spec_jobs.handle_clarify(ctx)

    assert result["questions"] == [
        {"id": "Q1", "question": "Which Python version?", "default_answer": "Python 3.12", "category": "language"}
    ]
    assert "language_request" not in (store.settings or {})
    assert not any(args[0] == "spec.unsupported_language" for args, _ in events)


def test_clarify_language_question_keeps_five_question_limit(monkeypatch):
    monkeypatch.setattr(
        spec_jobs,
        "_call_structured",
        lambda *a, **kw: _clarify_result(
            *[
                {"id": f"Q{i}", "question": f"Question number {i}?", "default_answer": "x", "category": "scope"}
                for i in range(1, 6)
            ]
        ),
    )
    store = _Store()
    ctx, _ = _ctx(store, "Rewrite our service in Ruby")

    result = spec_jobs.handle_clarify(ctx)

    questions = result["questions"]
    assert len(questions) == 5
    assert [q["id"] for q in questions] == ["Q1", "Q2", "Q3", "Q4", "Q5"]
    assert questions[0]["category"] == "language"
    assert "Ruby" in questions[0]["question"]


def test_clarify_detects_a_language_in_the_intake_guardrails(monkeypatch):
    monkeypatch.setattr(spec_jobs, "_call_structured", lambda *a, **kw: _clarify_result())
    store = _Store()
    settings = {"intake": {"language": "Kotlin"}}
    ctx, _ = _ctx(store, "Build a mobile backend", settings=settings)

    result = spec_jobs.handle_clarify(ctx)

    assert result["questions"][0]["category"] == "language"
    assert "Kotlin" in result["questions"][0]["question"]


def test_structured_calls_carry_the_supported_language_list(monkeypatch):
    from nai.spec import structured

    captured: dict = {}

    def _fake_call(client, role, messages, schema, **kwargs):
        captured["messages"] = messages
        return {}

    monkeypatch.setattr(structured, "call_structured_json", _fake_call)
    ctx = SimpleNamespace(client=object(), job={"id": "j1"})

    spec_jobs._call_structured(ctx, spec_jobs.CLARIFY, {"brief": "x"}, {}, "clarify")

    system = captured["messages"][0]["content"]
    assert "## Supported Languages" in system
    assert "`python`" in system
    assert "Never substitute" in system


def test_contract_language_is_normalised_to_the_project_language():
    project = {"id": "p1", "settings": {"language": "python"}}

    contract = {"language": "python", "entities": [], "components": [], "scenarios": []}
    assert spec_jobs._normalise_contract_language(contract, project) is None
    assert contract["language"] == "python"

    contract = {"language": "rust", "entities": [], "components": [], "scenarios": []}
    assert spec_jobs._normalise_contract_language(contract, project) == "rust"
    assert contract["language"] == "python"

    contract = {"entities": [], "components": [], "scenarios": []}
    assert spec_jobs._normalise_contract_language(contract, project) is None
    assert "language" not in contract
