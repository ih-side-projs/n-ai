"""The worker API: its own listener, separate from the dashboard.

Workers pull work: they heartbeat, long-poll ``claim``, download a snapshot and
post a result.  Authentication is a per-worker bearer token (stored hashed),
and a claim is one ``BEGIN IMMEDIATE`` transaction in the store, so two workers
can never receive the same task.

Binding a non-loopback address requires TLS unless ``insecure_lan`` is set
explicitly; see :func:`check_bind`.
"""

from __future__ import annotations

import ipaddress
import json
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .errors import RecipeError
from .recipes import spec_for, validate_params
from .snapshots import SHA_RE

API_PREFIX = "/worker/v1"
MAX_OUTPUT_BYTES = 2 * 1024 * 1024


class HeartbeatBody(BaseModel):
    """What a worker reports on every heartbeat."""

    task_id: str | None = None
    capabilities: list[str] | None = None
    toolchains: dict[str, Any] | None = None


class ClaimBody(BaseModel):
    """A claim request; ``wait`` long-polls for up to that many seconds."""

    capabilities: list[str] = []
    wait: float = 20.0


class ResultBody(BaseModel):
    """The outcome of one recipe run, parsed later on Linux."""

    task_id: str
    exit_code: int
    output: str = ""
    duration: float = 0.0
    timed_out: bool = False
    toolchains: dict[str, Any] | None = None
    error: str = ""
    infrastructure: bool = False


def is_loopback(host: str) -> bool:
    """True for loopback addresses and ``localhost``."""
    text = str(host or "").strip().strip("[]")
    if text.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


def check_bind(
    host: str, *, certfile: str | None = None, keyfile: str | None = None, insecure_lan: bool = False
) -> None:
    """Refuse a non-loopback bind without TLS or an explicit opt-in."""
    if is_loopback(host) or insecure_lan:
        return
    if certfile and keyfile:
        return
    raise ValueError(
        f"refusing to bind {host!r}: a non-loopback worker API needs --certfile/--keyfile "
        "or an explicit --insecure-lan"
    )


def _validate_queued(task: dict[str, Any]) -> str | None:
    """Server-side re-validation of a queued task; returns an error or None."""
    blob = task.get("params")
    if not isinstance(blob, dict):
        return "task params are not an object"
    nested = blob.get("params")
    params = nested if isinstance(nested, dict) else blob
    try:
        spec = spec_for(str(task.get("recipe") or ""))
        validate_params(spec, params)
    except RecipeError as exc:
        return str(exc)
    return None


class _SerializedStore:
    """Share one sqlite connection across FastAPI's thread pool, one call at a time.

    ``BEGIN IMMEDIATE`` transactions cannot interleave on a single connection,
    so every store call takes the lock.  The connection is opened with
    ``allow_thread_sharing=True``; no other component relaxes sqlite3's
    thread-affinity check.
    """

    def __init__(self, store: Any):
        self._store = store
        self._lock = threading.Lock()

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._store, name)
        if not callable(attr):
            return attr

        def call(*args: Any, **kwargs: Any) -> Any:
            with self._lock:
                return attr(*args, **kwargs)

        return call

    def close(self) -> None:
        with self._lock:
            self._store.close()


def create_worker_app(
    store: Any,
    *,
    snapshot_dir: str | Path,
    lease_seconds: float = 60.0,
    max_redeliveries: int = 3,
    poll_seconds: float = 0.2,
    max_wait: float = 30.0,
    on_note: Callable[[str, str], None] | None = None,
) -> FastAPI:
    """Build the worker-facing app around a writable :class:`nai.store.Store`."""
    if not getattr(store, "allows_thread_sharing", False):
        raise ValueError(
            "the worker API needs Store(..., allow_thread_sharing=True): "
            "requests are served from a thread pool"
        )
    store = _SerializedStore(store)
    snapshots = Path(snapshot_dir).expanduser().resolve()
    app = FastAPI(title="N-AI worker API", docs_url=None, redoc_url=None, openapi_url=None)

    def note(event: str, message: str) -> None:
        if on_note is not None:
            try:
                on_note(event, message)
            except Exception:  # noqa: BLE001 - a note must never break a worker call
                pass

    def authenticate(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        scheme, _, token = str(authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise HTTPException(status_code=401, detail="missing bearer token")
        worker = store.worker_for_token(token.strip())
        if worker is None:
            raise HTTPException(status_code=401, detail="unknown or revoked token")
        return worker

    @app.get(f"{API_PREFIX}/health")
    def health() -> dict[str, Any]:
        return {"ok": True}

    @app.post(f"{API_PREFIX}/heartbeat")
    def heartbeat(body: HeartbeatBody, worker: dict[str, Any] = Depends(authenticate)) -> dict[str, Any]:
        store.touch_worker(worker["id"], capabilities=body.capabilities, toolchains=body.toolchains)
        state: dict[str, Any] | None = None
        if body.task_id:
            state = store.heartbeat_remote_task(
                body.task_id, worker["id"], lease_seconds=lease_seconds, max_redeliveries=max_redeliveries
            )
        return {
            "ok": True,
            "worker": worker["name"],
            "cancel": bool(state and state.get("cancel")),
            "task": state,
        }

    @app.post(f"{API_PREFIX}/claim")
    def claim(body: ClaimBody, worker: dict[str, Any] = Depends(authenticate)) -> Response:
        capabilities = body.capabilities or worker.get("capabilities") or []
        deadline = time.monotonic() + max(0.0, min(float(body.wait or 0.0), max_wait))
        while True:
            task = store.claim_remote_task(
                worker["id"], capabilities, lease_seconds=lease_seconds, max_redeliveries=max_redeliveries
            )
            if task is not None:
                problem = _validate_queued(task)
                if problem:
                    note("remote.task_rejected", f"rejected task {task['id']}: {problem}")
                    store.finish_remote_task(
                        task["id"], worker["id"],
                        {"infrastructure": True, "error": f"infrastructure: {problem}"},
                    )
                else:
                    return Response(content=json.dumps(task), media_type="application/json")
            if time.monotonic() >= deadline:
                return Response(status_code=204)
            time.sleep(max(0.01, float(poll_seconds)))

    @app.get(f"{API_PREFIX}/snapshot/{{sha}}")
    def snapshot(sha: str, worker: dict[str, Any] = Depends(authenticate)) -> FileResponse:
        if not SHA_RE.match(str(sha)):
            raise HTTPException(status_code=400, detail="invalid snapshot address")
        path = snapshots / f"{sha}.tar.gz"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="snapshot not found")
        return FileResponse(path, media_type="application/gzip", filename=f"{sha}.tar.gz")

    @app.post(f"{API_PREFIX}/result")
    def result(body: ResultBody, worker: dict[str, Any] = Depends(authenticate)) -> dict[str, Any]:
        if body.infrastructure:
            message = str(body.error or "worker could not run the task")
            if not store.fail_remote_task(body.task_id, worker["id"], message):
                raise HTTPException(status_code=409, detail="task is not leased to this worker")
            return {"ok": True, "infrastructure": True}
        output = body.output if isinstance(body.output, str) else ""
        truncated = len(output.encode("utf-8", errors="replace")) > MAX_OUTPUT_BYTES
        payload = {
            "exit_code": int(body.exit_code),
            "output": output,
            "duration": float(body.duration or 0.0),
            "timed_out": bool(body.timed_out),
            "toolchains": body.toolchains or {},
            "error": str(body.error or ""),
            "truncated": truncated,
        }
        if not store.finish_remote_task(body.task_id, worker["id"], payload):
            raise HTTPException(status_code=409, detail="task is not leased to this worker")
        return {"ok": True}

    app.state.worker_store = store
    app.state.snapshot_dir = snapshots
    return app


def serve_worker_api(
    store: Any,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    snapshot_dir: str | Path,
    lease_seconds: float = 60.0,
    certfile: str | None = None,
    keyfile: str | None = None,
    insecure_lan: bool = False,
) -> None:  # pragma: no cover - exercised by the CLI, not unit tests
    """Run the worker API with uvicorn on its own listener."""
    import uvicorn

    check_bind(host, certfile=certfile, keyfile=keyfile, insecure_lan=insecure_lan)
    app = create_worker_app(store, snapshot_dir=snapshot_dir, lease_seconds=lease_seconds)
    uvicorn.run(app, host=host, port=int(port), log_level="info",
                ssl_certfile=certfile or None, ssl_keyfile=keyfile or None)

