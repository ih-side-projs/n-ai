"""FastAPI read-only dashboard for N-AI (WP7 / SS-WEB v1)."""

from __future__ import annotations

import asyncio
import html
import json
import mimetypes
import os
import sqlite3
import time
from collections import Counter
from collections.abc import Generator
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any

import bleach
import mistune
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from nai.store import Store

from ..workflow import (
    DESIGN,
    IMPLEMENTATION,
    INFO_GATHERING,
    filter_tree_to_phase,
    next_step,
    node_phase,
    project_phase_summary,
)
from .metrics import _all_nodes, build_project_metrics
from .models import ModelServerClient, ModelServerError
from .security import new_csrf_token, verify_bearer_or_cookie, verify_same_origin, verify_token

PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = PACKAGE_DIR / "templates"
STATIC_DIR = PACKAGE_DIR / "static"
MARKDOWN = mistune.create_markdown(escape=True, plugins=["table", "strikethrough", "task_lists"])
ALLOWED_TAGS = set(bleach.sanitizer.ALLOWED_TAGS) | {
    "p", "pre", "code", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote",
    "ul", "ol", "li", "table", "thead", "tbody", "tr", "th", "td", "input", "del", "br",
}
ALLOWED_ATTRS = {
    **bleach.sanitizer.ALLOWED_ATTRIBUTES,
    "a": ["href", "title", "rel"],
    "code": ["class"],
    "th": ["align"], "td": ["align"],
    "input": ["type", "checked", "disabled"],
}


@dataclass(frozen=True)
class WebSettings:
    database_path: Path
    llama_swap_url: str | None = None
    llama_swap_key: str | None = None
    runner_status_file: Path | None = None
    bearer_token: str | None = None
    event_poll_seconds: float = 1.0
    max_view_bytes: int = 1_000_000
    output_roots: tuple[Path, ...] = ()

    @classmethod
    def from_env(cls, database_path: str | Path | None = None) -> WebSettings:
        db_path = Path(database_path or os.environ.get("NAI_DB_PATH", ".nai/nai.sqlite3")).expanduser().resolve()
        status = os.environ.get("NAI_RUNNER_STATUS_FILE")
        raw_roots = os.environ.get("NAI_OUTPUT_ROOTS", "")
        output_roots = tuple(
            Path(item).expanduser().resolve()
            for item in raw_roots.split(os.pathsep)
            if item.strip()
        )
        return cls(
            database_path=db_path,
            llama_swap_url=os.environ.get("NAI_LLAMA_SWAP_URL") or os.environ.get("LLAMA_SWAP_URL"),
            llama_swap_key=os.environ.get("NAI_LLAMA_SWAP_KEY") or os.environ.get("LLAMA_SWAP_KEY"),
            runner_status_file=Path(status).expanduser().resolve() if status else None,
            bearer_token=os.environ.get("NAI_WEB_TOKEN"),
            event_poll_seconds=max(0.25, float(os.environ.get("NAI_WEB_EVENT_POLL_SECONDS", "1"))),
            output_roots=output_roots,
        )


def create_app(
    database_path: str | Path | None = None,
    *,
    settings: WebSettings | None = None,
    llama_swap_url: str | None = None,
    llama_swap_key: str | None = None,
    runner_status_file: str | Path | None = None,
    bearer_token: str | None = None,
    event_poll_seconds: float = 1.0,
) -> FastAPI:
    """Create the WP7 app. The web process only opens read-only Store connections."""
    if settings is None:
        settings = WebSettings.from_env(database_path)
        overrides = {}
        if llama_swap_url is not None:
            overrides["llama_swap_url"] = llama_swap_url
        if llama_swap_key is not None:
            overrides["llama_swap_key"] = llama_swap_key
        if runner_status_file is not None:
            overrides["runner_status_file"] = Path(runner_status_file).expanduser().resolve()
        if bearer_token is not None:
            overrides["bearer_token"] = bearer_token
        if event_poll_seconds != 1.0:
            overrides["event_poll_seconds"] = max(0.25, event_poll_seconds)
        if overrides:
            settings = WebSettings(**{**settings.__dict__, **overrides})
    else:
        settings = WebSettings(
            database_path=Path(settings.database_path).expanduser().resolve(),
            llama_swap_url=settings.llama_swap_url,
            llama_swap_key=settings.llama_swap_key,
            runner_status_file=Path(settings.runner_status_file).expanduser().resolve() if settings.runner_status_file else None,
            bearer_token=settings.bearer_token,
            event_poll_seconds=max(0.25, settings.event_poll_seconds),
            max_view_bytes=settings.max_view_bytes,
            output_roots=tuple(Path(p).expanduser().resolve() for p in settings.output_roots),
        )

    app = FastAPI(title="N-AI", version="0.1.0", docs_url="/api/docs", redoc_url=None)
    app.state.settings = settings
    app.state.model_client = ModelServerClient(settings.llama_swap_url, settings.llama_swap_key)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["pretty_json"] = lambda value: json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True)
    templates.env.filters["markdown"] = render_markdown
    templates.env.filters["duration"] = _format_duration
    app.state.templates = templates

    @app.middleware("http")
    async def security_headers_and_csrf(request: Request, call_next):
        csrf_cookie = request.cookies.get("nai_csrf")
        csrf = csrf_cookie or new_csrf_token()
        request.state.csrf_token = csrf

        if settings.bearer_token and request.url.path not in {"/auth", "/healthz"} and not request.url.path.startswith("/static/"):
            if not verify_bearer_or_cookie(request, settings.bearer_token):
                if request.url.path.startswith("/api/") or request.headers.get("accept", "").startswith("application/json"):
                    return JSONResponse({"detail": "Authentication required"}, status_code=401)
                return RedirectResponse(url="/auth", status_code=303)

        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            try:
                verify_same_origin(request)
                supplied = request.headers.get("x-csrf-token")
                if not supplied:
                    # Read the form for CSRF, then replay the raw body so the
                    # endpoint can still bind Form(...) parameters.
                    raw = await request.body()
                    form = await request.form()
                    request.state.parsed_form = form
                    supplied = str(form.get("csrf_token") or "")

                    async def replay_receive():
                        return {"type": "http.request", "body": raw, "more_body": False}

                    request._receive = replay_receive
                verify_token(csrf_cookie, supplied)
            except HTTPException as exc:
                return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

        response = await call_next(request)
        if not csrf_cookie and request.method in {"GET", "HEAD"}:
            response.set_cookie("nai_csrf", csrf, httponly=False, secure=request.url.scheme == "https",
                                samesite="strict", path="/", max_age=86400 * 30)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'")
        return response

    def open_store() -> Store:
        if not settings.database_path.is_file():
            raise HTTPException(status_code=503, detail=f"N-AI database not found: {settings.database_path}. Start the store/runner or initialize the database first.")
        try:
            return Store(settings.database_path, read_only=True)
        except (OSError, sqlite3.Error, RuntimeError) as exc:
            raise HTTPException(status_code=503, detail=f"Could not open N-AI database read-only: {exc}") from exc

    def get_store() -> Generator[Store, None, None]:
        store = open_store()
        try:
            yield store
        finally:
            store.close()

    def render(request: Request, name: str, context: dict[str, Any] | None = None, status_code: int = 200):
        payload = {"request": request, "csrf_token": request.state.csrf_token, **(context or {})}
        response = templates.TemplateResponse(request=request, name=name, context=payload, status_code=status_code)
        # Control pages reflect live store state; never let a browser cache or
        # bfcache silently show an old filter/view after a command is processed.
        response.headers["Cache-Control"] = "no-store"
        return response

    def project_or_404(store: Store, project_id: str) -> dict[str, Any]:
        project = store.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        return project

    def node_or_404(store: Store, project_id: str, node_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        project = project_or_404(store, project_id)
        node = store.get_node(node_id)
        if node is None or node["project_id"] != project_id:
            raise HTTPException(status_code=404, detail="Node not found")
        return project, node

    def runner_status() -> dict[str, Any]:
        # Runner (SCH-9) writes runner.json into its state dir, which defaults
        # to the database's folder. Match that default exactly.
        path = settings.runner_status_file or (settings.database_path.parent / "runner.json")
        if not path.is_file():
            return {"status": "not-reported", "message": "Runner heartbeat is not available yet (WP3 integration)."}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("expected a JSON object")
            return raw
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            return {"status": "unavailable", "message": f"Runner status file could not be read: {exc}"}

    def project_tree(store: Store, project_id: str) -> list[dict[str, Any]]:
        seen: set[str] = set()

        def make(node: dict[str, Any]) -> dict[str, Any]:
            if node["id"] in seen:
                return {**node, "children": [], "subtree_counts": {}}
            seen.add(node["id"])
            children = [make(child) for child in store.list_nodes(project_id, parent_id=node["id"])]
            counts = Counter([node["status"]])
            for child in children:
                counts.update(child["subtree_counts"])
            return {**node, "children": children, "subtree_counts": dict(counts)}

        return [make(root) for root in store.list_nodes(project_id, parent_id=None)]

    def project_summary(store: Store, project: dict[str, Any]) -> dict[str, Any]:
        nodes = _all_nodes(store, project["id"])
        counts = Counter(n["status"] for n in nodes)
        done = counts.get("done", 0)
        total = len(nodes)
        jobs = store.list_jobs(project["id"], limit=10000)
        running = [j for j in jobs if j.get("status") == "running"]
        last_event = store.conn.execute(
            "SELECT time, message, type FROM events WHERE project_id=? ORDER BY time DESC, id DESC LIMIT 1",
            (project["id"],),
        ).fetchone()
        settings_block = project.get("settings") or {}
        budget = settings_block.get("budget") or settings_block.get("budgets") or {}
        if not isinstance(budget, dict):
            budget = {}
        budget_limit = budget.get("max_tokens", budget.get("max_total_tokens", budget.get("total_tokens")))
        try:
            budget_limit = int(budget_limit) if budget_limit is not None else None
            if budget_limit is not None and budget_limit < 0:
                budget_limit = None
        except (TypeError, ValueError):
            budget_limit = None
        tokens = sum(int(j.get("tokens_in") or 0) + int(j.get("tokens_out") or 0) for j in jobs)
        budget_percent = round(tokens * 100 / budget_limit, 1) if budget_limit else None
        return {
            **project,
            "node_count": total,
            "node_counts": dict(counts),
            "percent_done": round(done * 100 / total, 1) if total else 0.0,
            "last_activity": dict(last_event) if last_event else None,
            "current_job": running[0] if running else None,
            "job_count": len(jobs),
            "tokens_used": tokens,
            "budget": budget,
            "budget_limit_tokens": budget_limit,
            "budget_percent": budget_percent,
            "next_step": next_step(project, nodes, jobs),
            **project_phase_summary(
                nodes,
                {nid: [j for j in jobs if j.get("node_id") == nid] for nid in (n["id"] for n in nodes)},
                project,
            ),
        }

    def node_page_data(store: Store, project_id: str, node: dict[str, Any]) -> dict[str, Any]:
        requirements = store.list_requirements(node["id"])
        tests = store.list_test_records(node["id"])
        jobs = [j for j in store.list_jobs(project_id, limit=10000) if j.get("node_id") == node["id"]]
        artifacts = store.list_artifacts(project_id, node_id=node["id"])
        approvals = store.list_approvals(node["id"])
        matrix = []
        for requirement in requirements:
            matched = [test for test in tests if requirement["id"] in test.get("requirement_ids", [])]
            matrix.append({"requirement": requirement, "tests": matched})
        return {
            "requirements": requirements, "tests": tests, "jobs": jobs,
            "artifacts": artifacts, "approvals": approvals, "traceability": matrix,
            "approval_stages": {a.get("stage"): a.get("decision") for a in approvals},
        }

    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "database_configured": settings.database_path.is_file()}

    @app.get("/auth", response_class=HTMLResponse)
    def auth_page(request: Request):
        if not settings.bearer_token:
            return RedirectResponse("/", status_code=303)
        return render(request, "auth.html", {"error": None})

    @app.post("/auth")
    async def auth_submit(request: Request):
        if not settings.bearer_token:
            return RedirectResponse("/", status_code=303)
        form = getattr(request.state, "parsed_form", None) or await request.form()
        supplied = str(form.get("token") or "")
        if not supplied or not __import__("hmac").compare_digest(supplied, settings.bearer_token):
            return render(request, "auth.html", {"error": "The token was not accepted."}, status_code=401)
        response = RedirectResponse("/", status_code=303)
        response.set_cookie("nai_auth", settings.bearer_token, httponly=True,
                            secure=request.url.scheme == "https", samesite="strict", path="/", max_age=86400 * 7)
        return response

    @app.get("/", response_class=HTMLResponse)
    @app.get("/projects", response_class=HTMLResponse)
    def projects_page(request: Request, show_archived: str | None = None,
                      store: Store = Depends(get_store)):
        show = str(show_archived or "").lower() in {"1", "true", "yes"}
        all_projects = store.list_projects()
        archived_count = sum(1 for p in all_projects if p.get("status") == "archived")
        visible = all_projects if show else [p for p in all_projects if p.get("status") != "archived"]
        projects = [project_summary(store, p) for p in visible]
        return render(request, "projects.html", {
            "projects": projects,
            "archived_count": archived_count,
            "show_archived": show,
            "output_roots": [str(path) for path in settings.output_roots],
            "active_page": "projects",
        })

    @app.post("/projects/{project_id}/archive")
    async def project_archive(project_id: str):
        _enqueue_command(project_id, "archive")
        return RedirectResponse(url="/projects", status_code=303)

    @app.post("/projects/{project_id}/unarchive")
    async def project_unarchive(project_id: str):
        _enqueue_command(project_id, "unarchive")
        return RedirectResponse(url="/projects", status_code=303)

    @app.post("/projects/create")
    async def projects_create(
        request: Request,
        name: str = Form(...),
        brief: str = Form(""),
        language: str = Form(""),
        interface_type: str = Form(""),
        target_users: str = Form(""),
        core_workflows: str = Form(""),
        non_goals: str = Form(""),
        runtime_deployment: str = Form(""),
        stack_libraries: str = Form(""),
        accessibility_security: str = Form(""),
        testing_expectations: str = Form(""),
        output_root: str = Form(""),
        spec_file: UploadFile | None = File(None),
    ):
        """Create a project with structured Info Gathering guardrails."""
        name = name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="Project name is required")
        if not settings.database_path.is_file():
            raise HTTPException(status_code=503, detail="N-AI database not found")
        uploaded_spec = ""
        if spec_file is not None and spec_file.filename:
            raw = await spec_file.read(2_000_001)
            if len(raw) > 2_000_000:
                raise HTTPException(status_code=413, detail="Spec file exceeds 2 MB")
            decoded = raw.decode("utf-8", errors="replace")
            uploaded_spec = decoded if decoded.strip() else ""
        description = brief.strip() or (uploaded_spec.strip()[:500] if uploaded_spec else "")
        guardrails = {
            "description": description,
            "source": "uploaded-spec" if uploaded_spec else "dialog",
            "language": language.strip(),
            "interface_type": interface_type.strip(),
            "target_users": target_users.strip(),
            "core_workflows": core_workflows.strip(),
            "non_goals": non_goals.strip(),
            "runtime_deployment": runtime_deployment.strip(),
            "stack_libraries": stack_libraries.strip(),
            "accessibility_security": accessibility_security.strip(),
            "testing_expectations": testing_expectations.strip(),
        }
        project_settings: dict[str, Any] = {
            "intake": guardrails,
            "intake_complete": False,
            "intake_waiting": False,
        }
        if guardrails["language"]:
            project_settings["target_env"] = {"language": guardrails["language"]}
        if uploaded_spec:
            project_settings["intake_complete"] = True
            project_settings["clarified"] = True
        selected_root: Path | None = None
        if output_root.strip():
            candidate = Path(output_root).expanduser().resolve()
            allowed = [Path(item).expanduser().resolve() for item in settings.output_roots]
            if not any(candidate == root or root in candidate.parents for root in allowed):
                raise HTTPException(status_code=400, detail="output root is not allowed")
            selected_root = candidate
        elif settings.output_roots:
            selected_root = settings.output_roots[0]
        store = Store(settings.database_path, read_only=False)
        try:
            try:
                pid = store.create_project(
                    name, description, settings=project_settings, output_root=selected_root,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            root_id = store.create_node(
                pid, "root", "system",
                objective=description or f"Root specification for {name}",
                spec_text=uploaded_spec or description,
            )
            if uploaded_spec:
                store.add_artifact(pid, "spec", uploaded_spec, node_id=root_id,
                                   filename=spec_file.filename or "SPEC.md")
                store.record_event(pid, "project.spec_uploaded",
                                   f"Existing spec uploaded for '{name}'", level="info")
            store.record_event(pid, "project.created_via_web", f"Project '{name}' created from the dashboard", level="info")
        finally:
            store.close()
        return RedirectResponse(url=f"/projects/{pid}", status_code=303)

    @app.post("/projects/{project_id}/intake/answers")
    async def project_intake_answers(project_id: str, request: Request):
        form = getattr(request.state, "parsed_form", None) or await request.form()
        answers = {
            key[2:]: str(value).strip()
            for key, value in form.multi_items()
            if key.startswith("q_") and str(value).strip()
        }
        _enqueue_command(project_id, "answer_intake", payload={"answers": answers})
        return RedirectResponse(url=f"/projects/{project_id}", status_code=303)

    @app.post("/projects/{project_id}/intake/begin")
    async def project_intake_begin(project_id: str):
        _enqueue_command(project_id, "begin_design")
        return RedirectResponse(url=f"/projects/{project_id}", status_code=303)

    @app.get("/projects/{project_id}", response_class=HTMLResponse)
    def project_page(project_id: str, request: Request, phase: str | None = None,
                     store: Store = Depends(get_store)):
        project = project_summary(store, project_or_404(store, project_id))
        tree = project_tree(store, project_id)
        # Named workflow phase view: design (specs/approvals) or implementation
        # (code/tests/integration). "all" (default) keeps both visible.
        selected_phase = phase if phase in (INFO_GATHERING, DESIGN, IMPLEMENTATION) else "all"
        if selected_phase in (DESIGN, IMPLEMENTATION):
            tree = filter_tree_to_phase(tree, selected_phase, project.get("per_node"))
        return render(request, "project.html", {"project": project, "tree": tree,
                      "runner": runner_status(), "active_page": "projects",
                      "selected_phase": selected_phase,
                      "phase_constants": {
                          "info_gathering": INFO_GATHERING,
                          "design": DESIGN,
                          "implementation": IMPLEMENTATION,
                      }})

    @app.get("/projects/{project_id}/nodes/{node_id}", response_class=HTMLResponse)
    def node_page(project_id: str, node_id: str, request: Request, store: Store = Depends(get_store)):
        project, node = node_or_404(store, project_id, node_id)
        data = node_page_data(store, project_id, node)
        node_phase_value = node_phase(node, data.get("jobs"))
        return render(request, "node.html", {"project": project, "node": node, **data,
                      "spec_html": render_markdown(node.get("spec_text") or ""),
                      "phase": node_phase_value,
                      "phase_label": "Design" if node_phase_value == DESIGN else "Implementation",
                      "active_page": "projects"})

    @app.get("/projects/{project_id}/logs", response_class=HTMLResponse)
    def logs_page(project_id: str, request: Request, job_id: str | None = None, store: Store = Depends(get_store)):
        project = project_or_404(store, project_id)
        jobs = store.list_jobs(project_id, limit=10000)
        if job_id and not any(j["id"] == job_id for j in jobs):
            raise HTTPException(status_code=404, detail="Job not found")
        events = _read_events(store, project_id, job_id=job_id, limit=100)
        return render(request, "logs.html", {"project": project, "jobs": jobs, "selected_job": job_id,
                      "events": events, "active_page": "logs"})

    @app.get("/projects/{project_id}/metrics", response_class=HTMLResponse)
    def metrics_page(project_id: str, request: Request, store: Store = Depends(get_store)):
        project = project_or_404(store, project_id)
        metrics = build_project_metrics(store, project)
        return render(request, "metrics.html", {"project": project, "metrics": metrics, "active_page": "metrics"})

    @app.get("/models", response_class=HTMLResponse)
    def models_page(request: Request, store: Store = Depends(get_store)):
        from ..config import load_config

        profiles = _list_profiles(store)
        stored_names = {p["name"] for p in profiles}
        cfg = load_config()
        # Include the built-in default profile so model ids are always editable,
        # even before any profile has been saved to the store.
        known = set(stored_names)
        for name, profile in cfg.profiles.items():
            if name not in known:
                profiles.append({"name": name, "reasoner": profile.reasoner.to_dict(),
                                 "coder": profile.coder.to_dict(),
                                 "swap_cost_seconds": profile.swap_cost_seconds})
        snapshot = app.state.model_client.snapshot()
        model_ids = []
        for model in snapshot.get("models") or []:
            if isinstance(model, dict) and model.get("id"):
                model_ids.append(str(model["id"]))
        return render(request, "models.html", {"profiles": profiles, "snapshot": snapshot,
                      "active_page": "models", "smoke_result": None, "message": None,
                      "stored_names": stored_names, "model_ids": model_ids})

    @app.post("/models/profile")
    async def models_save_profile(request: Request):
        """Persist a model profile (default: built-in ids) from the dashboard."""
        form = getattr(request.state, "parsed_form", None) or await request.form()
        name = str(form.get("name") or "default").strip() or "default"
        role = str(form.get("role") or "reasoner")
        model_id = str(form.get("model_id") or "").strip()
        if not model_id:
            raise HTTPException(status_code=400, detail="model_id is required")
        if role not in ("reasoner", "coder"):
            raise HTTPException(status_code=400, detail="role must be reasoner or coder")

        from ..config import load_config
        from ..models import ModelProfile, default_profile

        store = Store(settings.database_path, read_only=False)
        try:
            # Merge into the *stored* profile first, so editing one role does not
            # reset the other role (or ctx/sampling) back to config defaults.
            raw = store.get_model_profile(name)
            cfg = load_config()
            if raw:
                profile = ModelProfile.from_dict(name, {
                    "reasoner": raw.get("reasoner") or {},
                    "coder": raw.get("coder") or {},
                    "swap_cost_seconds": raw.get("swap_cost_seconds"),
                })
            elif name in cfg.profiles:
                profile = cfg.profiles[name]
            else:
                profile = default_profile()
            reasoner = profile.reasoner.to_dict()
            coder = profile.coder.to_dict()
            if role == "reasoner":
                reasoner["model_id"] = model_id
            else:
                coder["model_id"] = model_id
            sw = form.get("swap_cost_seconds")
            swap_cost = float(sw) if sw not in (None, "") else profile.swap_cost_seconds
            store.save_model_profile(name, reasoner, coder, swap_cost_seconds=swap_cost)
        finally:
            store.close()
        return RedirectResponse(url="/models", status_code=303)

    @app.post("/models/smoke-test", response_class=HTMLResponse)
    def models_smoke_test(request: Request, store: Store = Depends(get_store)):
        form = getattr(request.state, "parsed_form", None)
        if form is None:
            raise HTTPException(status_code=400, detail="Expected form data")
        model_id = str(form.get("model_id") or "").strip()
        if not model_id or len(model_id) > 300:
            raise HTTPException(status_code=400, detail="Select a valid model id")
        client: ModelServerClient = app.state.model_client
        snapshot = client.snapshot()
        available = {str(m.get("id")) for m in snapshot.get("models", []) if isinstance(m, dict) and m.get("id")}
        if model_id not in available:
            raise HTTPException(status_code=400, detail="Model id is not present in the configured /v1/models listing")
        try:
            result = client.smoke_test(model_id)
        except ModelServerError as exc:
            result = {"ok": False, "error": str(exc), "model_id": model_id}
        profiles = _list_profiles(store)
        return render(request, "models.html", {"profiles": profiles, "snapshot": client.snapshot(),
                      "active_page": "models", "smoke_result": result})

    @app.get("/projects/{project_id}/artifacts", response_class=HTMLResponse)
    def artifacts_page(project_id: str, request: Request, store: Store = Depends(get_store)):
        project = project_or_404(store, project_id)
        artifacts = store.list_artifacts(project_id)
        files = _list_project_files(Path(project["workspace_path"]))
        return render(request, "artifacts.html", {"project": project, "artifacts": artifacts,
                      "files": files, "active_page": "artifacts"})

    @app.get("/projects/{project_id}/artifacts/{artifact_id}")
    def artifact_view(project_id: str, artifact_id: str, request: Request, download: bool = False,
                      store: Store = Depends(get_store)):
        project = project_or_404(store, project_id)
        row = store.conn.execute(
            "SELECT * FROM artifacts WHERE project_id=? AND id=?", (project_id, artifact_id)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Artifact not found")
        path = _safe_workspace_path(Path(project["workspace_path"]), row["path"], allow_hidden=True)
        return _view_workspace_file(request, path, row["path"], project=project, download=download,
                                    max_bytes=settings.max_view_bytes, templates=templates,
                                    csrf_token=request.state.csrf_token)

    @app.get("/projects/{project_id}/files/{relative_path:path}")
    def project_file(project_id: str, relative_path: str, request: Request, download: bool = False,
                     store: Store = Depends(get_store)):
        project = project_or_404(store, project_id)
        path = _safe_workspace_path(Path(project["workspace_path"]), relative_path, allow_hidden=False)
        return _view_workspace_file(request, path, relative_path, project=project, download=download,
                                    max_bytes=settings.max_view_bytes, templates=templates,
                                    csrf_token=request.state.csrf_token)

    # ------------------------------------------------------------------
    # JSON API (WEB-15): mirrors the read-only pages above.
    # ------------------------------------------------------------------
    @app.get("/api/projects")
    def api_projects(show_archived: str | None = None, store: Store = Depends(get_store)):
        show = str(show_archived or "").lower() in {"1", "true", "yes"}
        projects = [p for p in store.list_projects() if show or p.get("status") != "archived"]
        return {"projects": [project_summary(store, p) for p in projects]}

    @app.get("/api/projects/{project_id}")
    def api_project(project_id: str, store: Store = Depends(get_store)):
        project = project_summary(store, project_or_404(store, project_id))
        return {"project": project, "tree": project_tree(store, project_id), "runner": runner_status()}

    @app.get("/api/projects/{project_id}/phase")
    def api_phase(project_id: str, phase: str | None = None, store: Store = Depends(get_store)):
        """Named workflow phase state for a project (design/implementation/mixed)."""
        project = project_summary(store, project_or_404(store, project_id))
        tree = project_tree(store, project_id)
        if phase in (DESIGN, IMPLEMENTATION):
            tree = filter_tree_to_phase(tree, phase, project.get("per_node"))
        return {
            "phase": project["phase"],
            "design_count": project["design_count"],
            "implementation_count": project["implementation_count"],
            "selected_phase": phase if phase in (DESIGN, IMPLEMENTATION) else "all",
            "per_node": project["per_node"],
            "tree": tree,
        }

    @app.get("/api/projects/{project_id}/nodes/{node_id}")
    def api_node(project_id: str, node_id: str, store: Store = Depends(get_store)):
        _, node = node_or_404(store, project_id, node_id)
        data = node_page_data(store, project_id, node)
        phase = node_phase(node, data.get("jobs"))
        return {"node": node, **data, "phase": phase,
                "phase_label": "Design" if phase == DESIGN else "Implementation"}

    @app.get("/api/projects/{project_id}/logs")
    def api_logs(project_id: str, job_id: str | None = None, limit: int = 100,
                 store: Store = Depends(get_store)):
        project_or_404(store, project_id)
        if job_id and not _job_belongs_to_project(store, project_id, job_id):
            raise HTTPException(status_code=404, detail="Job not found")
        return {"events": _read_events(store, project_id, job_id=job_id, limit=_clamp(limit, 1, 1000))}

    @app.get("/api/projects/{project_id}/logs/stream")
    async def api_log_stream(project_id: str, request: Request, job_id: str | None = None):
        with open_store() as store:
            project_or_404(store, project_id)
            if job_id and not _job_belongs_to_project(store, project_id, job_id):
                raise HTTPException(status_code=404, detail="Job not found")
        # The generator opens short-lived read-only connections as it polls.
        last_event_id = request.headers.get("last-event-id")
        return StreamingResponse(
            _event_stream(settings, project_id, job_id, last_event_id, request),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
        )

    @app.get("/api/projects/{project_id}/metrics")
    def api_metrics(project_id: str, store: Store = Depends(get_store)):
        project = project_or_404(store, project_id)
        return build_project_metrics(store, project)

    @app.get("/api/models")
    def api_models(store: Store = Depends(get_store)):
        return {"profiles": _list_profiles(store), "server": app.state.model_client.snapshot()}

    @app.get("/api/projects/{project_id}/artifacts")
    def api_artifacts(project_id: str, store: Store = Depends(get_store)):
        project = project_or_404(store, project_id)
        return {"artifacts": store.list_artifacts(project_id),
                "files": _list_project_files(Path(project["workspace_path"]))}

    @app.get("/api/projects/{project_id}/artifacts/{artifact_id}")
    def api_artifact(project_id: str, artifact_id: str, store: Store = Depends(get_store)):
        project = project_or_404(store, project_id)
        row = store.conn.execute(
            "SELECT * FROM artifacts WHERE project_id=? AND id=?", (project_id, artifact_id)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Artifact not found")
        path = _safe_workspace_path(Path(project["workspace_path"]), row["path"], allow_hidden=True)
        result = {"artifact": dict(row), "filename": path.name, "size": path.stat().st_size,
                  "download_url": f"/projects/{project_id}/artifacts/{artifact_id}?download=true"}
        if path.stat().st_size > settings.max_view_bytes:
            result["preview_available"] = False
            return result
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            result["preview_available"] = False
        else:
            result["preview_available"] = True
            result["content"] = content
            result["markdown"] = path.suffix.lower() in {".md", ".markdown"}
        return result

    @app.get("/api/projects/{project_id}/files")
    def api_files(project_id: str, store: Store = Depends(get_store)):
        project = project_or_404(store, project_id)
        return {"files": _list_project_files(Path(project["workspace_path"]))}

    @app.get("/api/projects/{project_id}/files/{relative_path:path}")
    def api_file(project_id: str, relative_path: str, store: Store = Depends(get_store)):
        project = project_or_404(store, project_id)
        path = _safe_workspace_path(Path(project["workspace_path"]), relative_path, allow_hidden=False)
        if path.stat().st_size > settings.max_view_bytes:
            raise HTTPException(status_code=413, detail="File exceeds preview limit")
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=415, detail="File is not UTF-8 text") from exc
        return {"path": relative_path, "filename": path.name, "content": content,
                "markdown": path.suffix.lower() in {".md", ".markdown"}}

    @app.get("/api/runner")
    def api_runner():
        return runner_status()

    # --- WP8 additions: approvals, escalations, tools ---

    @app.get("/projects/{project_id}/settings", response_class=HTMLResponse)
    def project_settings_page(project_id: str, request: Request, store: Store = Depends(get_store)):
        project = store.get_project(project_id)
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        settings = project.get("settings") or {}
        return templates.TemplateResponse(request=request, name="settings.html",
            context={"request": request, "csrf_token": request.state.csrf_token,
                     "project": project, "settings": settings, "active_page": "projects"})

    @app.post("/projects/{project_id}/settings")
    async def project_settings_save(
        project_id: str, request: Request,
        max_requirements: int = Form(8), max_functions: int = Form(10),
        max_components: int = Form(7), max_depth: int = Form(4),
        budget_limit_tokens: int = Form(0), test_commands: str = Form(""),
        backup_location: str = Form(""), target_language: str = Form("Python 3.12"),
    ):
        store = Store(settings.database_path, read_only=False)
        try:
            project = store.get_project(project_id)
            if not project:
                raise HTTPException(status_code=404, detail="Project not found")
            current = dict(project.get("settings") or {})
            limits = dict(current.get("size_limits") or current)
            limits.update({
                "max_requirements_per_component": max_requirements,
                "max_public_functions_per_component": max_functions,
                "max_components_per_level": max_components,
                "max_depth": max_depth,
            })
            current.update({
                "max_requirements_per_component": max_requirements,
                "max_public_functions_per_component": max_functions,
                "max_components_per_level": max_components,
                "max_depth": max_depth,
                "budget_limit_tokens": budget_limit_tokens or None,
                "test_commands": [c.strip() for c in test_commands.splitlines() if c.strip()],
                "backup_location": backup_location.strip() or None,
                "target_env": {**(current.get("target_env") or {}), "language": target_language},
            })
            store.update_project(project_id, settings=current)
            store.record_event(project_id, "project.settings_updated", "Project settings updated via dashboard",
                               level="info", data={"limits": limits})
        finally:
            store.close()
        return RedirectResponse(url=f"/projects/{project_id}/settings", status_code=303)

    @app.get("/approvals", response_class=HTMLResponse)
    def approvals_page(request: Request, store: Store = Depends(get_store)):
        pending = []
        for p in store.list_projects():
            for n in store.list_nodes(p["id"]):
                if n.get("status") == "awaiting_approval":
                    pending.append({"project": p, "node": n})
        return templates.TemplateResponse(request=request, name="approvals.html",
            context={"request": request, "csrf_token": request.state.csrf_token,
                     "pending": pending, "active_page": "approvals"})

    @app.get("/api/approvals")
    def api_approvals(store: Store = Depends(get_store)):
        pending = []
        for p in store.list_projects():
            for n in store.list_nodes(p["id"]):
                if n.get("status") == "awaiting_approval":
                    pending.append({"project": p, "node": n})
        return {"approvals": pending}

    @app.get("/escalations", response_class=HTMLResponse)
    def escalations_page(request: Request, store: Store = Depends(get_store)):
        escalated = []
        for p in store.list_projects():
            for n in store.list_nodes(p["id"]):
                if n.get("status") in ("escalated", "failed", "blocked"):
                    escalated.append({"project": p, "node": n})
        return templates.TemplateResponse(request=request, name="escalations.html",
            context={"request": request, "csrf_token": request.state.csrf_token,
                     "escalated": escalated, "active_page": "escalations"})

    @app.get("/api/escalations")
    def api_escalations(store: Store = Depends(get_store)):
        escalated = []
        for p in store.list_projects():
            for n in store.list_nodes(p["id"]):
                if n.get("status") in ("escalated", "failed", "blocked"):
                    escalated.append({"project": p, "node": n})
        return {"escalations": escalated}

    def _service_socket() -> Path | None:
        import os

        state = Path(os.environ.get("NAI_STATE_DIR", settings.database_path.parent)).expanduser().resolve()
        sock = state / "service.sock"
        return sock if sock.exists() else None

    def _service_call(cmd: str, **kwargs: Any) -> dict[str, Any]:
        from nai.service.protocol import request as service_request

        sock = _service_socket()
        if not sock:
            raise RuntimeError("Service supervisor is not running")
        resp = service_request(sock, cmd, **kwargs)
        if not resp.get("ok"):
            raise RuntimeError(resp.get("error") or "service command failed")
        return resp.get("data") or {}

    @app.get("/service", response_class=HTMLResponse)
    def service_page(request: Request):
        try:
            data = _service_call("status")
        except Exception as exc:  # noqa: BLE001
            data = {"running": False, "error": str(exc), "children": {}, "jobs": {}}
        return templates.TemplateResponse(request=request, name="service.html",
            context={"request": request, "csrf_token": request.state.csrf_token,
                     "service": data, "active_page": "service"})

    @app.get("/service/logs/{name}")
    def service_logs(name: str, request: Request):
        try:
            text = _service_call("logs", name=name, lines=200)
        except Exception as exc:  # noqa: BLE001
            text = f"ERROR: {exc}"
        return HTMLResponse(
            '<!doctype html><html><head><title>nai service log</title>'
            '<link rel="stylesheet" href="/static/app.css"></head><body><main class="container">'
            f'<div class="page-heading"><h1>Log: {escape(name)}</h1><a class="button secondary" href="/service">Back</a></div>'
            f'<pre class="code-block">{html.escape(text)}</pre></main></body></html>'
        )

    @app.post("/service/control")
    async def service_control(request: Request, action: str = Form(...), target: str = Form(""),
                              argv: str = Form("")):
        try:
            if action == "stop":
                data = _service_call("stop")
            elif action == "restart":
                data = _service_call("restart", name=target)
            elif action == "exec":
                # WEB-13: browser must not control dangerous flags. Accept only
                # a spec path and optional -w workspace; the supervisor builds argv.
                import shlex

                try:
                    parts = shlex.split(argv)
                except ValueError as exc:
                    raise ValueError(f"invalid command: {exc}") from exc
                if not parts or any(p.startswith("-") and p != "-w" for p in parts):
                    raise ValueError("only a spec path and optional -w WORKSPACE are allowed")
                safe_argv: list[str] = []
                i = 0
                while i < len(parts):
                    if parts[i] == "-w":
                        if i + 1 >= len(parts):
                            raise ValueError("-w needs a workspace path")
                        safe_argv += ["-w", parts[i + 1]]
                        i += 2
                    else:
                        safe_argv.append(parts[i])
                        i += 1
                data = _service_call("exec", argv=safe_argv, name=target or None)
            elif action == "cancel":
                data = _service_call("cancel", id=target)
            else:
                raise ValueError(f"unknown action: {action}")
            msg = f"Service action '{escape(action)}' completed: {escape(str(data))}"
            level = "success"
        except Exception as exc:  # noqa: BLE001
            msg = f"Service action failed: {escape(str(exc))}"
            level = "error"
        return HTMLResponse(
            f'<div class="notice {level}" role="status">{msg} '
            '<a href="/service">Refresh</a></div>'
        )

    @app.get("/api/service")
    def api_service():
        try:
            return {"running": True, **_service_call("status")}
        except Exception as exc:  # noqa: BLE001
            return {"running": False, "error": str(exc)}

    @app.get("/tools", response_class=HTMLResponse)
    def tools_page(request: Request, store: Store = Depends(get_store)):
        from collections import defaultdict
        tools = _tool_rows(store)
        families = defaultdict(list)
        for t in tools:
            families[t.get("family", "uncategorized")].append(t)
        return templates.TemplateResponse(request=request, name="tools.html",
            context={"request": request, "csrf_token": request.state.csrf_token,
                     "families": dict(families), "active_page": "tools"})

    def _tool_rows(store: Store) -> list[dict[str, Any]]:
        rows = []
        for t in store.list_tool_defs():
            item = dict(t)
            item["parameters"] = t.get("parameters_json") or t.get("parameters") or "{}"
            if isinstance(item["parameters"], str):
                try:
                    item["parameters"] = json.loads(item["parameters"])
                except json.JSONDecodeError:
                    item["parameters"] = {}
            item["side_effects"] = str(t.get("side_effects") or "none") not in ("none", "0", "false", "False", "")
            rows.append(item)
        return rows

    @app.get("/api/tools")
    def api_tools(store: Store = Depends(get_store)):
        return {"tools": _tool_rows(store)}

    @app.post("/tools/call")
    async def tools_call(request: Request, tool_name: str = Form(...), arguments: str = Form("{}"),
                         workspace: str | None = Form(None)):
        """Execute a registered tool through the same registry the MCP server uses.

        The tool runs in a sandbox rooted at the chosen workspace (default: the
        web process state dir). Errors come back as `ERROR:` strings (X3).
        """
        try:
            args = json.loads(arguments)
            if not isinstance(args, dict):
                raise ValueError("arguments must be a JSON object")
        except (json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"Invalid JSON arguments: {exc}") from exc

        ws_root: Path | None = None
        try:
            from nai.tools.registry import get_registry
            from nai.tools.sandbox import Sandbox

            # Only allow sandbox roots that are registered project workspaces.
            # Never trust an arbitrary browser-supplied path (TLS-5).
            allowed_roots: set[Path] = set()
            if settings.database_path.is_file():
                # Open in this coroutine: a sync Store dependency would be created
                # in a worker thread and cannot be used from the event loop.
                with Store(settings.database_path, read_only=True) as store:
                    for proj in store.list_projects():
                        wp = (proj.get("workspace_path") or "").strip()
                        if wp:
                            allowed_roots.add(Path(wp).expanduser().resolve())
            allowed_roots.add((settings.database_path.parent / "tools-workspace").resolve())

            if workspace:
                candidate = Path(workspace).expanduser().resolve()
                if candidate not in allowed_roots:
                    raise ValueError("workspace must be a registered project workspace")
                ws_root = candidate
            else:
                ws_root = settings.database_path.parent / "tools-workspace"
            ws_root.mkdir(parents=True, exist_ok=True)
            sandbox = Sandbox(ws_root)
            registry = get_registry()
            result = registry.call_tool(tool_name, args, sandbox)
            is_error = result.startswith("ERROR:")
        except Exception as exc:  # noqa: BLE001 - tool failures are reported as data
            result = f"ERROR: {type(exc).__name__}: {exc}"
            is_error = True
        return JSONResponse({"tool": tool_name, "arguments": args,
                             "result": result, "isError": is_error,
                             "workspace": str(ws_root) if ws_root else None})

    def _enqueue_command(pid: str, action: str, nid: str | None = None, payload: dict | None = None):
        """All state changes go through the commands table (STO-3)."""
        if not settings.database_path.is_file():
            raise HTTPException(status_code=503, detail="N-AI database not found")
        store = Store(settings.database_path, read_only=False)
        try:
            return store.enqueue_command(pid, action, node_id=nid, payload=payload or {})
        finally:
            store.close()

    def _writable_store() -> Store:
        if not settings.database_path.is_file():
            raise HTTPException(status_code=503, detail="N-AI database not found")
        return Store(settings.database_path, read_only=False)

    @app.get("/projects/{project_id}/nodes/{node_id}/edit", response_class=HTMLResponse)
    def node_edit_page(project_id: str, node_id: str, request: Request, store: Store = Depends(get_store)):
        project = store.get_project(project_id)
        node = store.get_node(node_id)
        if not project or not node or node.get("project_id") != project_id:
            raise HTTPException(status_code=404, detail="Node not found")
        approved_spec = ""
        for approval in reversed(store.list_approvals(node_id)):
            if approval.get("decision") == "approved" and approval.get("data"):
                approved_spec = approval["data"].get("spec_text", "")
                break
        current_spec = node.get("spec_text") or ""
        diff = []
        if approved_spec and approved_spec != current_spec:
            import difflib
            for line in difflib.unified_diff(approved_spec.splitlines(True), current_spec.splitlines(True), n=3):
                kind = "meta" if line.startswith(("---", "+++", "@@")) else \
                       "delete" if line.startswith("-") else \
                       "insert" if line.startswith("+") else "equal"
                diff.append({"type": kind, "text": line.rstrip("\n")})
        return templates.TemplateResponse(request=request, name="node_edit.html",
            context={"request": request, "csrf_token": request.state.csrf_token,
                     "project": project, "node": node, "current_spec": current_spec,
                     "approved_spec": approved_spec, "diff": diff, "active_page": "projects"})

    @app.post("/projects/{project_id}/nodes/{node_id}/edit")
    async def node_edit_submit(project_id: str, node_id: str, request: Request, spec_text: str = Form(...)):
        store = _writable_store()
        try:
            node = store.get_node(node_id)
            if not node or node.get("project_id") != project_id:
                raise HTTPException(status_code=404, detail="Node not found")
            store.update_node_spec(node_id, spec_text)
            store.recompute_project_hashes(project_id)
            store.record_event(project_id, "node.spec_edited", f"Spec updated for node {node_id}",
                               node_id=node_id, level="info", data={"action": "web_edit"})
        finally:
            store.close()
        return RedirectResponse(url=f"/projects/{project_id}/nodes/{node_id}", status_code=303)

    def _read_store() -> Store:
        if not settings.database_path.is_file():
            raise HTTPException(status_code=503, detail="N-AI database not found")
        return Store(settings.database_path, read_only=True)

    @app.post("/projects/{project_id}/nodes/{node_id}/approve")
    async def node_approve(project_id: str, node_id: str, request: Request,
                           note: str = Form(""), stage: str = Form("component-spec")):
        # STO-3: web never writes approvals/status directly; enqueue a command.
        if stage not in ("component-map", "interfaces", "component-spec", "escalation"):
            raise HTTPException(status_code=400, detail="invalid approval stage")
        store = _read_store()
        try:
            node = store.get_node(node_id)
            if not node or node.get("project_id") != project_id:
                raise HTTPException(status_code=404, detail="Node not found")
        finally:
            store.close()
        _enqueue_command(project_id, "approve", node_id, {"stage": stage, "note": note})
        return RedirectResponse(url=f"/projects/{project_id}/nodes/{node_id}", status_code=303)

    @app.post("/projects/{project_id}/nodes/{node_id}/reject")
    async def node_reject(project_id: str, node_id: str, request: Request,
                          note: str = Form(...), stage: str = Form("component-spec")):
        if stage not in ("component-map", "interfaces", "component-spec", "escalation"):
            raise HTTPException(status_code=400, detail="invalid approval stage")
        store = _read_store()
        try:
            node = store.get_node(node_id)
            if not node or node.get("project_id") != project_id:
                raise HTTPException(status_code=404, detail="Node not found")
        finally:
            store.close()
        _enqueue_command(project_id, "reject", node_id, {"stage": stage, "note": note})
        return RedirectResponse(url=f"/projects/{project_id}/nodes/{node_id}", status_code=303)

    @app.post("/projects/{project_id}/nodes/{node_id}/retry")
    async def node_retry(project_id: str, node_id: str, request: Request):
        store = _read_store()
        try:
            node = store.get_node(node_id)
            if not node or node.get("project_id") != project_id:
                raise HTTPException(status_code=404, detail="Node not found")
        finally:
            store.close()
        # CommandProcessor._retry_node resets the budget and does not add an attempt.
        _enqueue_command(project_id, "retry_node", node_id, {})
        return RedirectResponse(url=f"/projects/{project_id}/nodes/{node_id}", status_code=303)

    @app.post("/projects/{project_id}/nodes/{node_id}/rerun_stage")
    async def node_rerun_stage(project_id: str, node_id: str, request: Request, stage: str = Form(...)):
        store = _writable_store()
        try:
            store.enqueue_command(project_id, "rerun_stage", node_id=node_id, payload={"stage": stage})
            store.record_event(project_id, "node.rerun_stage", f"Stage '{stage}' re-run requested",
                               node_id=node_id, level="info", data={"stage": stage})
        finally:
            store.close()
        return RedirectResponse(url=f"/projects/{project_id}/nodes/{node_id}", status_code=303)

    @app.post("/projects/{project_id}/nodes/{node_id}/mark_stale")
    async def node_mark_stale(project_id: str, node_id: str, request: Request, note: str = Form("")):
        store = _writable_store()
        try:
            store.transition_node_status(node_id, "stale", note=note or "Marked stale via web UI")
            store.record_event(project_id, "node.stale", f"Node {node_id} marked stale",
                               node_id=node_id, level="warning", data={"note": note})
        finally:
            store.close()
        return RedirectResponse(url=f"/projects/{project_id}/nodes/{node_id}", status_code=303)

    @app.post("/projects/{project_id}/control/pause")
    async def control_pause(project_id: str, request: Request):
        store = _writable_store()
        try:
            store.enqueue_command(project_id, "pause")
            store.record_event(project_id, "project.paused", "Project paused via web UI", level="info")
        finally:
            store.close()
        return JSONResponse({"status": "ok", "message": "Pause command enqueued"})

    @app.post("/projects/{project_id}/control/resume")
    async def control_resume(project_id: str, request: Request):
        store = _writable_store()
        try:
            store.enqueue_command(project_id, "resume")
            store.record_event(project_id, "project.resumed", "Project resumed via web UI", level="info")
        finally:
            store.close()
        return JSONResponse({"status": "ok", "message": "Resume command enqueued"})

    @app.post("/projects/{project_id}/control/cancel")
    async def control_cancel(project_id: str, request: Request, job_id: str | None = Form(None)):
        store = _writable_store()
        try:
            if job_id:
                store.enqueue_command(project_id, "cancel_job", payload={"job_id": job_id})
                store.record_event(project_id, "job.cancel_requested", f"Job {job_id} cancel requested", level="warning")
            else:
                store.enqueue_command(project_id, "cancel")
                store.record_event(project_id, "project.cancel_requested", "Project cancel requested", level="warning")
        finally:
            store.close()
        return JSONResponse({"status": "ok", "message": "Cancel command enqueued"})

    @app.post("/projects/{project_id}/control/reorder")
    async def control_reorder(project_id: str, request: Request, node_order: str = Form(...)):
        try:
            order = json.loads(node_order)
            if not isinstance(order, list):
                raise ValueError
        except (json.JSONDecodeError, ValueError):
            raise HTTPException(status_code=400, detail="node_order must be a JSON array") from None
        store = _writable_store()
        try:
            store.enqueue_command(project_id, "reorder", payload={"order": order})
            store.record_event(project_id, "project.reordered", "Node priorities reordered", level="info")
        finally:
            store.close()
        return JSONResponse({"status": "ok", "message": "Reorder command enqueued"})

    return app


def render_markdown(source: str) -> str:
    """Render a safe Markdown subset; raw HTML is escaped and output sanitized."""
    rendered = MARKDOWN(source or "")
    return bleach.clean(rendered, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRS,
                        protocols={"http", "https", "mailto"}, strip=True)


def _format_duration(value: Any) -> str:
    if value is None:
        return "—"
    seconds = max(0, int(float(value)))
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {sec}s"
    hours, mins = divmod(minutes, 60)
    return f"{hours}h {mins}m"


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


def _list_profiles(store: Store) -> list[dict[str, Any]]:
    rows = store.conn.execute("SELECT * FROM model_profiles ORDER BY name").fetchall()
    return [{"name": row["name"], "reasoner": json.loads(row["reasoner_json"]),
             "coder": json.loads(row["coder_json"]), "swap_cost_seconds": row["swap_cost_seconds"]}
            for row in rows]


def _job_belongs_to_project(store: Store, project_id: str, job_id: str) -> bool:
    row = store.conn.execute("SELECT 1 FROM jobs WHERE project_id=? AND id=?", (project_id, job_id)).fetchone()
    return row is not None


def _read_events(store: Store, project_id: str, *, job_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    sql = "SELECT * FROM events WHERE project_id=?"
    args: list[Any] = [project_id]
    if job_id:
        sql += " AND job_id=?"
        args.append(job_id)
    sql += " ORDER BY time DESC, id DESC LIMIT ?"
    args.append(_clamp(limit, 1, 1000))
    rows = store.conn.execute(sql, args).fetchall()
    result = []
    for row in reversed(rows):
        item = dict(row)
        item["data"] = json.loads(item.pop("data_json"))
        result.append(item)
    return result


def _events_after(settings: WebSettings, project_id: str, job_id: str | None,
                  last: tuple[str, str] | None) -> list[dict[str, Any]]:
    with Store(settings.database_path, read_only=True) as store:
        sql = "SELECT * FROM events WHERE project_id=?"
        args: list[Any] = [project_id]
        if job_id:
            sql += " AND job_id=?"
            args.append(job_id)
        if last:
            sql += " AND (time > ? OR (time = ? AND id > ?))"
            args.extend([last[0], last[0], last[1]])
        sql += " ORDER BY time, id LIMIT 100"
        rows = store.conn.execute(sql, args).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["data"] = json.loads(item.pop("data_json"))
            result.append(item)
        return result


async def _event_stream(settings: WebSettings, project_id: str, job_id: str | None,
                        last_event_id: str | None, request: Request):
    last: tuple[str, str] | None = None
    if last_event_id and "|" in last_event_id:
        timestamp, event_id = last_event_id.split("|", 1)
        last = (timestamp, event_id)
    else:
        # First connection: emit the latest 100 stored events, in chronological order.
        with Store(settings.database_path, read_only=True) as store:
            backlog = _read_events(store, project_id, job_id=job_id, limit=100)
        for item in backlog:
            event_id = f"{item['time']}|{item['id']}"
            last = (item["time"], item["id"])
            yield _sse_message(item, event_id)

    heartbeat_at = time.monotonic()
    while not await request.is_disconnected():
        try:
            events = await asyncio.to_thread(_events_after, settings, project_id, job_id, last)
        except (OSError, sqlite3.Error, ValueError) as exc:
            yield f"event: stream-error\ndata: {json.dumps({'error': str(exc)})}\n\n"
            await asyncio.sleep(max(2.0, settings.event_poll_seconds))
            continue
        for item in events:
            event_id = f"{item['time']}|{item['id']}"
            last = (item["time"], item["id"])
            yield _sse_message(item, event_id)
        if time.monotonic() - heartbeat_at > 15:
            yield ": keep-alive\n\n"
            heartbeat_at = time.monotonic()
        await asyncio.sleep(settings.event_poll_seconds)


def _sse_message(item: dict[str, Any], event_id: str) -> str:
    payload = {k: item.get(k) for k in ("id", "project_id", "node_id", "job_id", "time", "level", "type", "message", "data")}
    return f"id: {event_id}\nevent: log\ndata: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"


def _list_project_files(workspace: Path, *, max_files: int = 500, max_depth: int = 7) -> list[dict[str, Any]]:
    try:
        root = workspace.resolve(strict=True)
    except OSError:
        return []
    if not root.is_dir():
        return []
    ignored = {".git", ".nai", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
               ".mypy_cache", ".ruff_cache", ".tox", ".nox", "dist", "build", "coverage", ".next"}
    files: list[dict[str, Any]] = []
    for directory, dirs, names in os.walk(root, followlinks=False):
        current = Path(directory)
        depth = len(current.relative_to(root).parts)
        dirs[:] = [d for d in sorted(dirs) if d not in ignored and not d.startswith(".") and not (current / d).is_symlink()]
        if depth >= max_depth:
            dirs[:] = []
        for name in sorted(names):
            if name.startswith(".") or name in {".DS_Store"}:
                continue
            path = current / name
            if path.is_symlink() or not path.is_file():
                continue
            try:
                resolved = path.resolve(strict=True)
                if root not in resolved.parents or resolved.stat().st_size > 100_000_000:
                    continue
                stat = resolved.stat()
            except OSError:
                continue
            relative = resolved.relative_to(root).as_posix()
            files.append({"path": relative, "name": name, "size": stat.st_size,
                          "modified": int(stat.st_mtime)})
            if len(files) >= max_files:
                break
        if len(files) >= max_files:
            break
    return sorted(files, key=lambda x: x["path"].lower())


def _safe_workspace_path(workspace: Path, relative_path: str, *, allow_hidden: bool) -> Path:
    try:
        root = workspace.expanduser().resolve(strict=True)
    except OSError as exc:
        raise HTTPException(status_code=404, detail="Project workspace does not exist") from exc
    if not relative_path or "\x00" in relative_path:
        raise HTTPException(status_code=404, detail="File not found")
    rel = Path(relative_path)
    if rel.is_absolute() or any(part in {"..", ""} for part in rel.parts):
        raise HTTPException(status_code=404, detail="File not found")
    if not allow_hidden and any(part.startswith(".") for part in rel.parts):
        raise HTTPException(status_code=404, detail="File not found")
    try:
        candidate = (root / rel).resolve(strict=True)
    except OSError as exc:
        raise HTTPException(status_code=404, detail="File not found") from exc
    if candidate == root or root not in candidate.parents or not candidate.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return candidate


def _view_workspace_file(request: Request, path: Path, relative_path: str, *, project: dict[str, Any],
                        download: bool, max_bytes: int, templates: Jinja2Templates, csrf_token: str):
    size = path.stat().st_size
    if not download and size > max_bytes:
        raise HTTPException(status_code=413, detail=f"File is too large to preview ({size} bytes; limit {max_bytes})")
    if download:
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return FileResponse(path, media_type=media_type, filename=path.name)
    text_suffixes = {".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".toml", ".yaml", ".yml",
                     ".md", ".markdown", ".sql", ".sh", ".css", ".html", ".xml", ".ini", ".cfg",
                     ".txt", ".log", ".csv", ".rst", ".conf"}
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if not (media_type.startswith("text/") or path.suffix.lower() in text_suffixes):
        return FileResponse(path, media_type=media_type, filename=path.name)
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=415, detail="File is not UTF-8 text; use download instead") from exc
    markdown_html = render_markdown(content) if path.suffix.lower() in {".md", ".markdown"} else None
    return templates.TemplateResponse(request=request, name="file_view.html",
        context={"request": request, "csrf_token": csrf_token, "project": project, "filename": path.name,
                 "relative_path": relative_path, "content": content, "markdown_html": markdown_html,
                 "download_url": request.url.path + "?download=true", "active_page": "artifacts"})
