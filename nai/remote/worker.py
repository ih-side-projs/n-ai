"""The worker agent: ``nai-worker init|run|doctor``.

A worker only executes recipes from its own ``worker.toml``.  It never sees a
model-supplied command, never invokes a shell, and runs with a scrubbed
environment.  The work directory is replaced per snapshot except for the
adapter's cache directories, and SIGTERM (or a ``cancel`` flag in a heartbeat
response) kills the whole process group.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ..exec.target import TIMEOUT_EXIT, OutputCapture, scrub_env
from .errors import RecipeError, SnapshotError
from .recipes import RecipeSpec, check_template, render_argv, spec_for
from .snapshots import extract_snapshot

OUTPUT_CAP = 2 * 1024 * 1024
TEMPLATE_VARIABLES = ("python", "workdir")
DEFAULT_HEARTBEAT = 15.0
DEFAULT_CLAIM_WAIT = 20.0
DEFAULT_RECIPE_TIMEOUT = 900.0


def _load_toml(path: Path) -> dict[str, Any]:
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ModuleNotFoundError:
            raise RecipeError("reading worker.toml needs Python 3.11+ or the 'tomli' package") from None
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except OSError as exc:
        raise RecipeError(f"cannot read {path}: {exc}") from None
    except tomllib.TOMLDecodeError as exc:
        raise RecipeError(f"{path} is not valid TOML: {exc}") from None
    if not isinstance(data, dict):
        raise RecipeError(f"{path} must contain a TOML table")
    return data


@dataclass(frozen=True)
class RecipeConfig:
    """One recipe template from ``worker.toml``."""

    id: str
    argv: tuple[str, ...]
    timeout: float = DEFAULT_RECIPE_TIMEOUT
    spec: RecipeSpec | None = None


def _narrow_spec(spec: RecipeSpec, allowed: Any, recipe_id: str) -> RecipeSpec:
    """Apply the worker's own parameter allowlist (it may narrow, never widen)."""
    if allowed is None:
        return spec
    if not isinstance(allowed, Sequence) or isinstance(allowed, str) or not allowed:
        raise RecipeError(f"worker.toml: [recipes.{recipe_id}].params must be a non-empty array")
    names = {str(name) for name in allowed}
    unknown = sorted(names - set(spec.params))
    if unknown:
        raise RecipeError(
            f"worker.toml: [recipes.{recipe_id}].params names unknown parameters: {', '.join(unknown)}"
        )
    dropped = sorted(name for name, param in spec.params.items() if param.required and name not in names)
    if dropped:
        raise RecipeError(
            f"worker.toml: [recipes.{recipe_id}].params drops required parameters: {', '.join(dropped)}"
        )
    return replace(spec, params={name: param for name, param in spec.params.items() if name in names})


@dataclass
class WorkerConfig:
    """Everything the agent needs, validated against the shared recipe registry."""

    server_url: str
    token: str
    name: str
    work_dir: Path
    capabilities: tuple[str, ...] = ("python",)
    python: str = "python3"
    heartbeat_seconds: float = DEFAULT_HEARTBEAT
    claim_wait: float = DEFAULT_CLAIM_WAIT
    env: dict[str, str] = field(default_factory=dict)
    cache_dirs: tuple[str, ...] = ()
    recipes: dict[str, RecipeConfig] = field(default_factory=dict)
    source: Path | None = None

    @property
    def variables(self) -> dict[str, str]:
        return {"python": self.python, "workdir": str(self.work_dir)}

    def recipe(self, recipe_id: str) -> RecipeConfig:
        try:
            return self.recipes[str(recipe_id)]
        except KeyError:
            raise RecipeError(
                f"unknown recipe {recipe_id!r} (configured: {', '.join(sorted(self.recipes)) or 'none'})"
            ) from None

    @classmethod
    def from_toml(cls, path: str | Path) -> WorkerConfig:
        """Parse and validate ``worker.toml``; unknown recipes/params are errors."""
        source = Path(path).expanduser().resolve()
        data = _load_toml(source)
        block = data.get("worker")
        if not isinstance(block, dict):
            raise RecipeError("worker.toml needs a [worker] table")
        server = str(block.get("server") or "").strip()
        if not server:
            raise RecipeError("worker.toml: [worker].server is required")
        token = str(block.get("token") or "").strip()
        token_file = str(block.get("token_file") or "").strip()
        if not token and token_file:
            token = Path(token_file).expanduser().read_text(encoding="utf-8").strip()
        if not token:
            raise RecipeError("worker.toml: [worker].token or token_file is required")
        name = str(block.get("name") or "").strip()
        if not name:
            raise RecipeError("worker.toml: [worker].name is required")
        work_dir = Path(str(block.get("work_dir") or "~/.nai-worker")).expanduser()
        capabilities = tuple(
            str(c).strip().lower() for c in (block.get("capabilities") or ["python"]) if str(c).strip()
        )
        env = block.get("env") or {}
        if not isinstance(env, dict):
            raise RecipeError("worker.toml: [worker].env must be a table")
        cache_dirs = tuple(str(c) for c in (block.get("cache_dirs") or ()) if str(c).strip())
        recipes: dict[str, RecipeConfig] = {}
        table = data.get("recipes") or {}
        if not isinstance(table, dict) or not table:
            raise RecipeError("worker.toml needs at least one [recipes.<id>] table")
        for recipe_id, entry in table.items():
            if not isinstance(entry, dict):
                raise RecipeError(f"worker.toml: [recipes.{recipe_id}] must be a table")
            spec = spec_for(recipe_id)
            argv = entry.get("argv")
            if isinstance(argv, str) or not isinstance(argv, Sequence) or not argv:
                raise RecipeError(f"worker.toml: [recipes.{recipe_id}].argv must be a non-empty array")
            template = tuple(str(part) for part in argv)
            spec = _narrow_spec(spec, entry.get("params"), recipe_id)
            check_template(spec, template, TEMPLATE_VARIABLES)
            recipes[recipe_id] = RecipeConfig(
                id=recipe_id,
                argv=template,
                timeout=float(entry.get("timeout") or spec.timeout or DEFAULT_RECIPE_TIMEOUT),
                spec=spec,
            )
        return cls(
            server_url=server.rstrip("/"),
            token=token,
            name=name,
            work_dir=work_dir,
            capabilities=capabilities,
            python=str(block.get("python") or "python3"),
            heartbeat_seconds=max(1.0, float(block.get("heartbeat_seconds") or DEFAULT_HEARTBEAT)),
            claim_wait=max(0.0, float(block.get("claim_wait") or DEFAULT_CLAIM_WAIT)),
            env={str(k): str(v) for k, v in env.items()},
            cache_dirs=cache_dirs,
            recipes=recipes,
            source=source,
        )



@dataclass(frozen=True)
class ExecOutcome:
    """One recipe run's raw outcome, posted back to the orchestrator."""

    exit_code: int
    output: str
    duration: float
    timed_out: bool = False
    truncated: bool = False
    cancelled: bool = False


def _kill_group(proc: subprocess.Popen, grace: float = 3.0) -> None:
    """SIGTERM the process group, then SIGKILL whatever survives the grace."""
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + (grace if sig == signal.SIGTERM else 0.0)
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return
            time.sleep(0.05)
    try:
        proc.wait(timeout=1.0)
    except subprocess.TimeoutExpired:  # pragma: no cover - SIGKILL is not ignorable
        pass


def run_process(
    argv: Sequence[str],
    *,
    cwd: str | Path,
    env: Mapping[str, str],
    timeout: float,
    cancel_event: threading.Event | None = None,
) -> ExecOutcome:
    """Run argv with no shell, a capped output window and a real cancel path."""
    capture = OutputCapture(cap=OUTPUT_CAP)
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            [str(part) for part in argv],
            cwd=str(cwd),
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            bufsize=1,
            start_new_session=True,
        )
    except OSError as exc:
        return ExecOutcome(exit_code=127, output=f"cannot start recipe: {exc}", duration=0.0)

    def _pump() -> None:
        stream = proc.stdout
        if stream is None:  # pragma: no cover - PIPE is always set above
            return
        for chunk in stream:
            capture.feed(chunk)

    reader = threading.Thread(target=_pump, name="nai-worker-output", daemon=True)
    reader.start()
    timed_out = cancelled = False
    while True:
        try:
            proc.wait(timeout=0.1)
            break
        except subprocess.TimeoutExpired:
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                _kill_group(proc)
                break
            if time.monotonic() - started >= timeout:
                timed_out = True
                _kill_group(proc)
                break
    reader.join(timeout=2.0)
    duration = time.monotonic() - started
    if timed_out:
        code = TIMEOUT_EXIT
    elif cancelled:
        code = 130
    else:
        code = int(proc.returncode or 0)
    return ExecOutcome(
        exit_code=code,
        output=capture.text(),
        duration=duration,
        timed_out=timed_out,
        truncated=capture.truncated,
        cancelled=cancelled,
    )


def prepare_workdir(
    base: str | Path,
    archive: str | Path,
    *,
    cache_dirs: Sequence[str] = (),
) -> Path:
    """Replace *base* with the snapshot, carrying adapter caches across."""
    base = Path(base)
    staging = base.parent / f".{base.name}.incoming"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    extract_snapshot(archive, staging)
    for rel in cache_dirs:
        source = base / rel
        if not source.exists():
            continue
        target = staging / rel
        if target.exists():
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            else:
                target.unlink(missing_ok=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
    if base.exists():
        shutil.rmtree(base, ignore_errors=True)
    os.replace(staging, base)
    return base


def _project_dir(work_dir: Path, project_id: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "-._" else "_" for ch in str(project_id)) or "project"
    return work_dir / "projects" / safe



class WorkerAgent:
    """Claim, execute and report recipes until stopped."""

    def __init__(self, config: WorkerConfig, *, client: Any = None, log: Callable[[str], None] = print):
        self.config = config
        self.log = log
        self._client = client
        self._cancel = threading.Event()
        self._stop = threading.Event()
        self._task_id: str | None = None
        self._lock = threading.Lock()

    # -- transport -----------------------------------------------------------
    @property
    def client(self) -> Any:
        if self._client is None:
            import httpx

            self._client = httpx.Client(
                base_url=self.config.server_url,
                headers={"Authorization": f"Bearer {self.config.token}"},
                timeout=httpx.Timeout(120.0, connect=10.0),
            )
        return self._client

    def _post(self, path: str, payload: dict[str, Any], timeout: float = 120.0) -> dict[str, Any]:
        response = self.client.post(path, json=payload, timeout=timeout)
        if response.status_code == 401:
            raise RecipeError("worker token was rejected (revoked or wrong)")
        response.raise_for_status()
        return response.json() if response.content else {}

    def heartbeat_once(self) -> bool:
        """Report liveness and pick up a cancel flag for the running task."""
        with self._lock:
            task_id = self._task_id
        data = self._post(
            "/worker/v1/heartbeat",
            {"capabilities": list(self.config.capabilities), "task_id": task_id},
        )
        if data.get("cancel"):
            self.log("server asked the task to stop")
            self._cancel.set()
        return bool(data.get("ok"))

    def claim(self) -> dict[str, Any] | None:
        response = self.client.post(
            "/worker/v1/claim",
            json={"capabilities": list(self.config.capabilities), "wait": self.config.claim_wait},
            timeout=self.config.claim_wait + 60.0,
        )
        if response.status_code == 204:
            return None
        if response.status_code == 401:
            raise RecipeError("worker token was rejected (revoked or wrong)")
        response.raise_for_status()
        return response.json()

    def download(self, sha: str, dest: Path) -> Path:
        response = self.client.get(f"/worker/v1/snapshot/{sha}", timeout=600.0)
        response.raise_for_status()
        data = response.content
        if hashlib.sha256(data).hexdigest() != sha:
            raise SnapshotError("snapshot digest does not match its address")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return dest


    # -- execution -----------------------------------------------------------
    def toolchains(self) -> dict[str, str]:
        """Versions the orchestrator records with each result."""
        versions: dict[str, str] = {}
        try:
            outcome = run_process((self.config.python, "--version"), cwd=self.config.work_dir,
                                  env=scrub_env(), timeout=30.0)
            lines = (outcome.output or "").strip().splitlines()
            versions["python"] = lines[0] if lines else "unknown"
        except Exception:  # noqa: BLE001 - a missing toolchain is reported, not fatal
            versions["python"] = "unavailable"
        return versions

    def execute_task(self, task: dict[str, Any]) -> dict[str, Any] | None:
        """Run one claimed task; returns the result payload or None when cancelled."""
        recipe = self.config.recipe(str(task.get("recipe") or ""))
        blob = task.get("params") if isinstance(task.get("params"), dict) else {}
        params = blob.get("params") if isinstance(blob.get("params"), dict) else {}
        cache_dirs = blob.get("cache_dirs") or self.config.cache_dirs
        sha = str(task.get("snapshot_sha") or "")
        archive = self.config.work_dir / "downloads" / f"{sha}.tar.gz"
        self.download(sha, archive)
        workdir = prepare_workdir(
            _project_dir(self.config.work_dir, str(task.get("project_id") or "project")),
            archive,
            cache_dirs=tuple(str(c) for c in cache_dirs),
        )
        spec = recipe.spec or spec_for(recipe.id)
        argv = render_argv(spec, recipe.argv, params, self.config.variables)
        self._cancel.clear()
        self.log(f"running {recipe.id}: {' '.join(argv)}")
        outcome = run_process(
            argv,
            cwd=workdir,
            env=scrub_env(extra=self.config.env),
            timeout=recipe.timeout,
            cancel_event=self._cancel,
        )
        if outcome.cancelled or self._cancel.is_set():
            # Never post a "done" result for a cancelled task: the orchestrator
            # already knows, and a shutdown must requeue rather than fail.
            return None
        return {
            "exit_code": outcome.exit_code,
            "output": outcome.output,
            "duration": outcome.duration,
            "timed_out": outcome.timed_out,
            "truncated": outcome.truncated,
            "toolchains": self.toolchains(),
            "error": "",
        }

    def run_once(self) -> bool:
        """Claim at most one task and report it; True when a task was handled."""
        task = self.claim()
        if task is None:
            return False
        with self._lock:
            self._task_id = str(task["id"])
        try:
            result = self.execute_task(task)
            if result is not None:
                self._post("/worker/v1/result", {"task_id": task["id"], **result})
            return True
        finally:
            with self._lock:
                self._task_id = None

    def request_stop(self) -> None:
        """SIGTERM/SIGINT: kill the running recipe group and stop claiming."""
        self._cancel.set()
        self._stop.set()

    def start_heartbeat(self) -> None:
        """Heartbeat in the background so the server can cancel a running task."""
        if getattr(self, "_heartbeat", None) is not None:
            return
        self.config.work_dir.mkdir(parents=True, exist_ok=True)

        def beat() -> None:
            while not self._stop.wait(self.config.heartbeat_seconds):
                try:
                    self.heartbeat_once()
                except Exception as exc:  # noqa: BLE001 - keep the loop alive
                    self.log(f"heartbeat failed: {exc}")

        self._heartbeat = threading.Thread(target=beat, name="nai-worker-heartbeat", daemon=True)
        self._heartbeat.start()

    def run_forever(self, *, max_tasks: int | None = None) -> int:
        """Heartbeat in the background while claiming tasks."""
        self.start_heartbeat()
        handled = 0
        try:
            while not self._stop.is_set():
                try:
                    ran = self.run_once()
                except Exception as exc:  # noqa: BLE001 - the agent must keep running
                    self.log(f"task error: {exc}")
                    with self._lock:
                        task_id = self._task_id
                    if task_id:
                        try:
                            self._post("/worker/v1/result", {
                                "task_id": task_id, "exit_code": 1, "output": str(exc),
                                "error": f"infrastructure: {exc}", "timed_out": False,
                                "infrastructure": True,
                            })
                        except Exception:  # noqa: BLE001 - the lease will expire instead
                            pass
                    with self._lock:
                        self._task_id = None
                    # A transport failure must not spin: back off before retrying.
                    self._stop.wait(1.0)
                    ran = False
                handled += 1 if ran else 0
                if max_tasks is not None and handled >= max_tasks:
                    break
                if not ran:
                    time.sleep(1.0)
        finally:
            self._stop.set()
        return handled



TEMPLATE = """# nai worker configuration.
#
# Create the token on the orchestrator with `nai worker add {name}` and paste it
# below.  Recipes are argv arrays: no shell is ever involved, and only the
# parameters declared by the shared recipe registry may be used.

[worker]
name = "{name}"
server = "{server}"
token = "{token}"
work_dir = "~/.nai-worker"
capabilities = {capabilities}
python = "{python}"
heartbeat_seconds = 15
claim_wait = 20
cache_dirs = [".venv", ".pytest_cache", ".ruff_cache", ".mypy_cache", "__pycache__"]

[recipes.echo]
params = ["message"]
argv = ["echo", "{{message}}"]

[recipes.toolchain]
argv = ["{{python}}", "--version"]

[recipes.build]
params = ["targets"]
argv = ["{{python}}", "-m", "compileall", "-q", "{{targets}}"]

[recipes.lint]
params = ["targets"]
argv = ["{{python}}", "-m", "ruff", "check", "{{targets}}"]

[recipes.verify]
params = ["targets"]
argv = ["{{python}}", "-m", "pytest", "{{targets}}", "-q"]
timeout = 1800

[recipes.integrate]
params = ["targets"]
argv = ["{{python}}", "-m", "pytest", "{{targets}}", "-q"]
timeout = 1800

[recipes.swift-build]
argv = ["swift", "build"]
timeout = 1800

[recipes.swift-test]
params = ["filter"]
argv = ["swift", "test", "--filter", "{{filter}}"]
timeout = 3600

[recipes.xcodebuild-test]
params = ["xcode_project", "scheme", "destination"]
argv = ["xcodebuild", "test", "-project", "{{xcode_project}}", "-scheme", "{{scheme}}", "-destination", "{{destination}}"]
timeout = 3600

[recipes.cmake-configure]
argv = ["cmake", "-S", ".", "-B", "build", "-DCMAKE_BUILD_TYPE=Debug"]
timeout = 600

[recipes.cmake-build]
argv = ["cmake", "--build", "build"]
timeout = 1800

[recipes.ctest]
params = ["filter"]
argv = ["ctest", "--output-on-failure", "-R", "{{filter}}"]
timeout = 1800

[recipes.go-build]
argv = ["go", "build", "./..."]
timeout = 1200

[recipes.go-test]
params = ["filter"]
argv = ["go", "test", "-json", "{{filter}}"]
timeout = 1800

[recipes.cargo-build]
argv = ["cargo", "build", "--workspace"]
timeout = 1800

[recipes.cargo-test]
params = ["filter"]
argv = ["cargo", "test", "-p", "{{filter}}"]
timeout = 1800

[recipes.npm-build]
argv = ["npx", "tsc", "--noEmit"]
timeout = 1800

[recipes.npm-test]
params = ["filter"]
argv = ["npx", "vitest", "run", "--reporter=json", "--no-color", "{{filter}}"]
timeout = 1800
"""


def write_config(
    path: str | Path,
    *,
    name: str,
    server: str,
    token: str,
    capabilities: Sequence[str] = ("macos", "python"),
    python: str = "python3",
    force: bool = False,
) -> Path:
    """Write a starter ``worker.toml`` (mode 0600) and return its path."""
    target = Path(path).expanduser().resolve()
    if target.exists() and not force:
        raise RecipeError(f"{target} already exists (use --force to overwrite)")
    caps = ", ".join(f'"{c}"' for c in capabilities)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        TEMPLATE.format(name=name, server=server, token=token, capabilities=f"[{caps}]", python=python),
        encoding="utf-8",
    )
    target.chmod(0o600)
    return target


def _install_signal_handlers(agent: WorkerAgent) -> None:
    if threading.current_thread() is not threading.main_thread():  # pragma: no cover
        return

    def handler(signum, frame):  # noqa: ARG001 - signal signature
        agent.log(f"received signal {signum}; stopping")
        agent.request_stop()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):  # pragma: no cover - non-main thread
            pass



def worker_main(argv: list[str] | None = None) -> int:
    """Entry point for ``nai-worker``."""
    import argparse

    parser = argparse.ArgumentParser(prog="nai-worker", description="Run build/test recipes for a nai orchestrator")
    sub = parser.add_subparsers(dest="action", required=True)

    init = sub.add_parser("init", help="write a starter worker.toml")
    init.add_argument("--config", default="worker.toml")
    init.add_argument("--name", required=True)
    init.add_argument("--server", required=True, help="orchestrator URL, e.g. http://192.168.1.10:8765")
    init.add_argument("--token", required=True, help="token printed once by `nai worker add`")
    init.add_argument("--capabilities", default="macos,python")
    init.add_argument("--python", default="python3")
    init.add_argument("--force", action="store_true")

    run = sub.add_parser("run", help="claim and run recipes")
    run.add_argument("--config", default="worker.toml")
    run.add_argument("--once", action="store_true", help="handle at most one task and exit")
    run.add_argument("--max-tasks", type=int, default=None)

    doctor = sub.add_parser("doctor", help="check config, toolchains and server reachability")
    doctor.add_argument("--config", default="worker.toml")

    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    if args.action == "init":
        caps = [c.strip() for c in str(args.capabilities).split(",") if c.strip()]
        try:
            path = write_config(args.config, name=args.name, server=args.server, token=args.token,
                                capabilities=caps, python=args.python, force=args.force)
        except RecipeError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        print(f"wrote {path} (mode 0600)")
        return 0

    try:
        config = WorkerConfig.from_toml(args.config)
    except (RecipeError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.action == "run":
        agent = WorkerAgent(config, log=lambda msg: print(f"[worker {config.name}] {msg}", flush=True))
        _install_signal_handlers(agent)
        try:
            if args.once:
                # Exactly one claim attempt, with heartbeats so a cancel can land.
                agent.start_heartbeat()
                handled = 1 if agent.run_once() else 0
            else:
                handled = agent.run_forever(max_tasks=args.max_tasks)
        except RecipeError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        print(f"[worker {config.name}] stopped after {handled} task(s)")
        return 0

    return _doctor(config)


def _doctor(config: WorkerConfig) -> int:
    """Check the local config and the orchestrator handshake."""
    problems: list[str] = []
    print(f"worker: {config.name}")
    print(f"server: {config.server_url}")
    print(f"recipes: {', '.join(sorted(config.recipes))}")
    try:
        outcome = run_process((config.python, "--version"), cwd=Path.cwd(), env=scrub_env(), timeout=30.0)
        version = (outcome.output or "").strip() or "no output"
        if outcome.exit_code != 0:
            problems.append(f"{config.python} --version exited {outcome.exit_code}")
        print(f"python: {version}")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"cannot run {config.python}: {exc}")
    try:
        import httpx

        with httpx.Client(base_url=config.server_url,
                          headers={"Authorization": f"Bearer {config.token}"}, timeout=15.0) as client:
            health = client.get("/worker/v1/health")
            print(f"health: HTTP {health.status_code}")
            if health.status_code != 200:
                problems.append(f"health endpoint returned {health.status_code}")
            beat = client.post("/worker/v1/heartbeat", json={"capabilities": list(config.capabilities)})
            print(f"heartbeat: HTTP {beat.status_code}")
            if beat.status_code == 401:
                problems.append("token rejected: create one with `nai worker add` on the orchestrator")
            elif beat.status_code != 200:
                problems.append(f"heartbeat returned {beat.status_code}")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"cannot reach {config.server_url}: {exc}")
    if problems:
        for problem in problems:
            print(f"FAIL: {problem}", file=sys.stderr)
        return 1
    print("OK: worker is ready")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(worker_main())

