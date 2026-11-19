from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generator
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from nai.store import Store
from .models import ModelServerClient
from .security import new_csrf_token, verify_bearer_or_cookie, verify_same_origin, verify_token


class CSRFMiddleware(BaseHTTPMiddleware):
    """Middleware to generate and attach CSRF token to request state."""
    async def dispatch(self, request: Request, call_next):
        # Generate CSRF token if not present
        if not hasattr(request.state, "csrf_token"):
            request.state.csrf_token = new_csrf_token()
        response = await call_next(request)
        return response
PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = PACKAGE_DIR / "templates"
STATIC_DIR = PACKAGE_DIR / "static"
@dataclass(frozen=True)
class WebSettings:
    database_path: Path
    llama_swap_url: str | None = None
    llama_swap_key: str | None = None
    runner_status_file: Path | None = None
    bearer_token: str | None = None
    event_poll_seconds: float = 1.0
    max_view_bytes: int = 1_000_000
    @classmethod
    def from_env(cls, database_path: str | Path | None = None) -> "WebSettings":
        db_path = Path(database_path or os.environ.get("NAI_DB_PATH", ".nai/nai.sqlite3")).expanduser().resolve()
        status = os.environ.get("NAI_RUNNER_STATUS_FILE")
        return cls(database_path=db_path, llama_swap_url=os.environ.get("NAI_LLAMA_SWAP_URL") or os.environ.get("LLAMA_SWAP_URL"), llama_swap_key=os.environ.get("NAI_LLAMA_SWAP_KEY") or os.environ.get("LLAMA_SWAP_KEY"), runner_status_file=Path(status).expanduser().resolve() if status else None, bearer_token=os.environ.get("NAI_WEB_TOKEN"), event_poll_seconds=max(0.25, float(os.environ.get("NAI_WEB_EVENT_POLL_SECONDS", "1"))))
def create_app(database_path: str | Path | None = None, *, settings: WebSettings | None = None, llama_swap_url: str | None = None, llama_swap_key: str | None = None, runner_status_file: str | Path | None = None, bearer_token: str | None = None, event_poll_seconds: float = 1.0) -> FastAPI:
    if settings is None:
        settings = WebSettings.from_env(database_path)
        overrides = {}
        if llama_swap_url is not None: overrides["llama_swap_url"] = llama_swap_url
        if llama_swap_key is not None: overrides["llama_swap_key"] = llama_swap_key
        if runner_status_file is not None: overrides["runner_status_file"] = Path(runner_status_file).expanduser().resolve()
        if bearer_token is not None: overrides["bearer_token"] = bearer_token
        if overrides:
            settings = WebSettings(database_path=settings.database_path, llama_swap_url=overrides.get("llama_swap_url", settings.llama_swap_url), llama_swap_key=overrides.get("llama_swap_key", settings.llama_swap_key), runner_status_file=overrides.get("runner_status_file", settings.runner_status_file), bearer_token=overrides.get("bearer_token", settings.bearer_token), event_poll_seconds=max(0.25, overrides.get("event_poll_seconds", settings.event_poll_seconds)), max_view_bytes=settings.max_view_bytes)
    app = FastAPI(title="N-AI", version="0.2.0", docs_url="/api/docs", redoc_url=None)
    app.state.settings = settings
    app.state.model_client = ModelServerClient(settings.llama_swap_url, settings.llama_swap_key)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["pretty_json"] = lambda value: __import__("json").dumps(value, indent=2, ensure_ascii=False, sort_keys=True)
    app.state.templates = templates

    # Add CSRF middleware
    app.add_middleware(CSRFMiddleware)

    def open_store() -> Store:
        if not settings.database_path.is_file():
            raise HTTPException(status_code=503, detail=f"N-AI database not found: {settings.database_path}")
        try: return Store(settings.database_path, read_only=True)
        except Exception as exc: raise HTTPException(status_code=503, detail=f"Could not open N-AI database read-only: {exc}") from exc
    def get_store() -> Generator[Store, None, None]:
        store = open_store()
        try: yield store
        finally: store.close()
    def render(request: Request, template: str, context: dict[str, Any], status_code: int = 200) -> HTMLResponse:
        context.setdefault("request", request)
        context.setdefault("csrf_token", request.state.csrf_token)
        return templates.TemplateResponse(request=request, name=template, context=context, status_code=status_code)
    @app.get("/healthz")
    def healthz(): return {"status": "ok", "database_configured": settings.database_path.is_file()}
    @app.get("/", response_class=HTMLResponse)
    @app.get("/projects", response_class=HTMLResponse)
    def projects_page(request: Request, store: Store = Depends(get_store)):
        from collections import Counter
        projects = []
        for p in store.list_projects():
            nodes = store.list_nodes(p["id"])
            jobs = store.list_jobs(p["id"], limit=10000)
            events = store.list_events(p["id"], limit=10000)
            counts = Counter(n["status"] for n in nodes)
            total = len(nodes)
            done = counts.get("done", 0)
            running = [j for j in jobs if j["status"] == "running"]
            tokens = sum((j.get("tokens_in") or 0) + (j.get("tokens_out") or 0) for j in jobs)
            last_event = events[0] if events else None
            projects.append({**p, "node_count": total, "node_counts": dict(counts), "percent_done": round(done * 100 / total, 1) if total else 0.0, "last_activity": dict(last_event) if last_event else None, "current_job": running[0] if running else None, "job_count": len(jobs), "tokens_used": tokens})
        return render(request, "projects.html", {"projects": projects, "active_page": "projects"})
    @app.get("/healthz")
    def healthz(): return {"status": "ok", "database_configured": settings.database_path.is_file()}
    @app.get("/api/projects")
    def api_projects(store: Store = Depends(get_store)): return {"projects": store.list_projects()}
    @app.get("/approvals", response_class=HTMLResponse)
    def approvals_page(request: Request, store: Store = Depends(get_store)):
        pending = []
        for p in store.list_projects():
            for n in store.list_nodes(p["id"]):
                if n.get("status") == "awaiting_approval":
                    pending.append({"project": p, "node": n})
        return render(request, "approvals.html", {"pending": pending, "active_page": "approvals"})

    @app.get("/escalations", response_class=HTMLResponse)
    def escalations_page(request: Request, store: Store = Depends(get_store)):
        escalated = []
        for p in store.list_projects():
            for n in store.list_nodes(p["id"]):
                if n.get("status") in ("escalated", "failed", "blocked"):
                    escalated.append({"project": p, "node": n})
        return render(request, "escalations.html", {"escalated": escalated, "active_page": "escalations"})

    @app.get("/tools", response_class=HTMLResponse)
    def tools_page(request: Request, store: Store = Depends(get_store)):
        from collections import defaultdict
        tools = store.list_tool_defs()
        families = defaultdict(list)
        for t in tools:
            families[t["family"]].append(t)
        return render(request, "tools.html", {"families": dict(families), "active_page": "tools"})

    @app.get("/models", response_class=HTMLResponse)
    def models_page(request: Request, store: Store = Depends(get_store)):
        # Query model_profiles table directly (no list_model_profiles method on Store)
        rows = store.conn.execute("SELECT * FROM model_profiles ORDER BY name").fetchall()
        profiles = [dict(row) for row in rows]
        from .models import ModelServerClient
        client = ModelServerClient(settings.llama_swap_url, settings.llama_swap_key)
        try:
            snapshot = client.get_models()
        except Exception:
            snapshot = {"models": [], "running": None}
        return render(request, "models.html", {"profiles": profiles, "snapshot": snapshot, "active_page": "models"})

    return app
