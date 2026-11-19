"""SCH-1: the runner. One process, one writer. Owns the Store connection, polls for work, runs handlers.

Threading model: the thread that calls run()/step() is the *main thread*. It alone touches the Store. Handlers run in a
worker thread (or inline when SchedConfig.threaded is False) and reach the Store through a StoreProxy that forwards each
call to the main thread, so cancel, pause, commands and the heartbeat keep working while a long job runs.
"""
from __future__ import annotations

import itertools
import json
import os
import queue
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .. import terminal
from ..config import Config, ConfigError
from ..models import ModelClient, ModelProfile, default_profile
from .budget import Budget, Usage, UsageTracker, budget_for
from .commands import CommandProcessor
from .config import SchedConfig
from .context import RunContext
from .health import RunnerLock, write_health
from .pick import Batcher, Cand
from .readiness import depth_map, promote_ready, walk_nodes
from .registry import BudgetExceeded, HandlerRegistry, JobCancelled, JobResult
from .store_api import StoreActor, StoreProxy, serve
from .util import SystemClock, as_obj, is_invalid_transition, is_transient, iso

INACTIVE = ("paused", "done", "archived")
LOG_LEVEL = {"red": "error", "yellow": "warning"}


@dataclass
class JobRun:
    job: dict
    ctx: RunContext
    key: str | None
    project_id: str
    node_id: str | None
    started: float
    log_count: int = 0
    calls: int = 0


@dataclass
class PlanContext:
    """Given to planners (see register_planner): everything needed to decide which jobs a project needs next."""
    store: object                       # the runner's own Store (planners run on the main thread)
    project: dict
    nodes: list[dict]
    jobs: list[dict]                    # queued and running jobs of the project
    runner: "Runner"
    now: object = None
    nodes_by_id: dict = field(default_factory=dict)

    def nodes_with_status(self, *statuses: str) -> list[dict]:
        return [n for n in self.nodes if n.get("status") in statuses]

    def active_jobs(self, node_id: str | None) -> list[dict]:
        return [j for j in self.jobs if j.get("node_id") == node_id]

    def idle_ready_nodes(self) -> list[dict]:
        """`ready` nodes that have no queued or running job: the ones that need a first job."""
        return [n for n in self.nodes_with_status("ready") if not self.active_jobs(n["id"])]

    def usage(self, node: dict) -> Usage:
        return self.runner.node_usage(node)


class Runner:
    def __init__(self, store, llm=None, *, app_config: Config | None = None, sched: SchedConfig | None = None,
                 state_dir: str | Path = ".nai", clock=None, workspace_factory=None, registry: HandlerRegistry | None = None):
        self.store, self.llm = store, llm
        self.app_config = app_config or Config()
        self.sched = sched or SchedConfig()
        self.state_dir = Path(state_dir)
        self.clock = clock or SystemClock()
        self.registry = registry or HandlerRegistry()
        self.workspace_factory = workspace_factory or self._default_workspace
        self.inbox: queue.Queue = queue.Queue()
        self.actor = StoreActor(store, self.inbox)
        self.store_proxy = StoreProxy(self.actor)
        self.batcher = Batcher(self.sched.max_consecutive_jobs, self.sched.max_batch_seconds)
        self.tracker = UsageTracker()
        self.commands = CommandProcessor(self)
        self.running: dict[str, JobRun] = {}
        self.paused = False
        self.paused_projects: set[str] = set()
        self.planners: list = []
        self._ws: dict[str, object] = {}
        self._ws_lock = threading.Lock()
        self._profiles: dict[str, tuple[float, ModelProfile]] = {}
        self._views: dict[str, tuple[float, list[dict], dict]] = {}
        self._projects: dict[str, dict] = {}
        self._seen: dict[str, int] = {}
        self._seen_counter = itertools.count()
        self._usage_loaded: set[str] = set()
        self._transient: dict[str, int] = {}
        self._pool: ThreadPoolExecutor | None = None
        self._lock: RunnerLock | None = None
        self._dirty = True
        self._next_plan = 0.0
        self._last_hb = -1e9
        self._stopping = False
        self._stop_reason = ""
        self._idle = False
        self._observer_added = False
        self._started = False
        self._backoff_until = 0.0
        self._backoff_delay = self.sched.backoff_start_seconds
        self._queue_depth: dict[str, int] = {}
        self._queued_total = 0
        self._last_state = ""
        self._started_at = None
        self.stats = {"jobs_succeeded": 0, "jobs_failed": 0, "jobs_cancelled": 0, "jobs_requeued": 0, "swaps": 0,
                      "swap_seconds": 0.0, "model_calls": 0, "steps": 0, "tokens_in": 0, "tokens_out": 0}

    # ================================================================ public API
    def register_planner(self, fn) -> None:
        """fn(PlanContext) is called for every active, unpaused project about every plan_interval_seconds (and right
        after anything changed). It creates the jobs a project needs next, e.g. a first job for each idle ready node.
        It must be idempotent: it will be called again before its jobs finish."""
        self.planners.append(fn)

    def mark_dirty(self) -> None:
        self._dirty = True
        self._views.clear()

    def stop(self, graceful: bool = True) -> None:
        """Graceful: start no new jobs, let running ones finish, then run() returns. Hard: also cancel running jobs and put
        them back in the queue (they continue on the next start, X4)."""
        self._stopping = True
        self._stop_reason = self._stop_reason or "stop requested"
        if not graceful:
            for jid in list(self.running):
                self.cancel_job(jid, "shutdown")

    def cancel_job(self, job_id: str, reason: str = "cancel") -> bool:
        run = self.running.get(job_id)
        if run:
            run.ctx.cancel_reason = reason
            run.ctx.cancel_event.set()
            ws = self._ws.get(run.project_id)
            if ws is not None:
                ws.runner.cancel()                       # kills a running command's process group (SCH-6)
            self._event(run.project_id, "job.cancel_requested", f"cancel requested ({reason})", node_id=run.node_id,
                        job_id=job_id, level="warning")
            return True
        job = self.store.get_job(job_id)
        if job and job.get("status") == "queued":
            self.store.update_job(job_id, status="cancelled", ended_at=iso(self.clock.now()), error="cancelled by owner")
            self._event(job["project_id"], "job.cancelled", "queued job cancelled", node_id=job.get("node_id"),
                        job_id=job_id, level="warning")
            self.stats["jobs_cancelled"] += 1
            return True
        return False

    def set_runner_paused(self, flag: bool, project_id: str | None) -> None:
        self.paused = flag
        if project_id:
            self._event(project_id, "runner.paused" if flag else "runner.resumed",
                        "runner paused: running jobs finish, no new job starts" if flag else "runner resumed",
                        data={"scope": "runner"})

    def set_project_paused(self, project_id: str, flag: bool) -> None:
        if hasattr(self.store, "set_project_status"):    # not in WP2's INTERFACES.md; see README proposal 1
            self.store.set_project_status(project_id, "paused" if flag else "active")
        (self.paused_projects.add if flag else self.paused_projects.discard)(project_id)
        self._event(project_id, "project.paused" if flag else "project.resumed",
                    "project paused" if flag else "project resumed")
        self.mark_dirty()

    def model_client(self, profile: ModelProfile) -> ModelClient:
        if self.llm is None:
            raise RuntimeError("this Runner was created without an LLM")
        return ModelClient(self.llm, profile)

    def profile_for(self, project: dict) -> ModelProfile:
        """Store profile by the project's settings["model_profile"], else the TOML profile, else the built-in default."""
        name = (as_obj(project.get("settings"), {}) or {}).get("model_profile") or self.app_config.default_profile
        hit = self._profiles.get(name)
        if hit and self.clock.mono() - hit[0] < 5.0:
            return hit[1]
        prof = None
        try:
            raw = as_obj(self.store.get_model_profile(name))
            if raw:
                prof = ModelProfile.from_dict(name, {"reasoner": as_obj(raw["reasoner"]), "coder": as_obj(raw["coder"]),
                                                     "swap_cost_seconds": raw.get("swap_cost_seconds")})
        except Exception:
            prof = None
        if prof is None:
            try:
                prof = self.app_config.get_profile(name)
            except ConfigError:
                prof = default_profile()
        self._profiles[name] = (self.clock.mono(), prof)
        return prof

    def workspace_for(self, project: dict):
        pid = project["id"]
        with self._ws_lock:
            if pid not in self._ws:
                self._ws[pid] = self.workspace_factory(project) if project.get("workspace_path") else None
            return self._ws[pid]

    @staticmethod
    def _default_workspace(project: dict):
        from ..workspace import Workspace
        s = as_obj(project.get("settings"), {}) or {}
        return Workspace(Path(project["workspace_path"]), cmd_timeout=int(s.get("cmd_timeout", 600)),
                         docker_image=s.get("docker_image"), git=bool(s.get("git", True)),
                         test_cmds=list(s.get("test_commands") or []))

    def node_usage(self, node: dict | None, extra_tokens: int = 0, extra_seconds: float = 0.0) -> Usage:
        if not node:
            return Usage()
        return self.tracker.usage(node["id"], int(node.get("attempts") or 0), extra_tokens, extra_seconds)

    def budget_problem(self, ctx: RunContext) -> str | None:
        """Mid-job budget check (called from the worker thread): tokens and wall-clock only; attempts are counted at start."""
        if not ctx.node:
            return None
        u = self.tracker.usage(ctx.node["id"], 0, ctx.tokens, ctx.elapsed)
        return UsageTracker.exceeded(u, ctx.budget)

    # ================================================================ main loop
    def run(self, *, drain: bool = False, max_steps: int | None = None) -> dict:
        """Run until stopped. drain=True also returns when nothing is queued or running. Returns the stats dict."""
        self._lock = RunnerLock(self.state_dir)
        self._lock.acquire()
        try:
            self.start()
            did = True
            while True:
                did = self.step(wait=0.0 if did else self.sched.poll_seconds)
                if self._stopping and not self.running:
                    break
                if drain and self._idle and not self.running:
                    break
                if max_steps is not None and self.stats["steps"] >= max_steps:
                    break
        finally:
            self.close()
            self._lock.release()
        return dict(self.stats)

    def start(self) -> None:
        """Hook up recording, restore pause state and recover from a crash. Called by run(), and by step() if needed."""
        if self._started:
            return
        self._started = True
        self.actor.bind_current_thread()
        self._started_at = iso(self.clock.now())
        if self.llm is not None and not self._observer_added:
            self.llm.observers.append(self._observer)
            self._observer_added = True
            loaded = self.llm.running_models()
            if loaded and len(loaded) == 1:
                self.batcher.note_loaded(loaded[0])
        terminal.add_sink(self._sink)
        self._restore_pause_state()
        self.recover()

    def close(self) -> None:
        self._stopping = True
        terminal.remove_sink(self._sink)
        if self.llm is not None and self._observer_added:
            try:
                self.llm.observers.remove(self._observer)
            except ValueError:
                pass
            self._observer_added = False
        if self._pool:
            self._pool.shutdown(wait=True)
            self._pool = None
        self._drain_inbox(0)
        self._started = False
        for ws in self._ws.values():
            if ws is not None:
                ws.close()
        self._ws.clear()
        self._write_health("stopped")

    def step(self, wait: float = 0.0) -> bool:
        """One loop iteration. Returns True if anything happened (a message handled, a command run, a job started)."""
        self.start()
        self.actor.bind_current_thread()
        self.stats["steps"] += 1
        did = self._drain_inbox(wait)
        did |= self._process_commands()
        projects = self.store.list_projects()
        self._projects = {p["id"]: p for p in projects}
        active = [p for p in projects if self._is_active(p)]
        for p in active:
            self._ensure_usage(p["id"])
        mono = self.clock.mono()
        if self._dirty or mono >= self._next_plan:
            self._plan_pass(active)
        cands = self._candidates(active)
        started = self._maybe_start(cands)
        self._idle = not self.running and self._queued_total == 0
        self._heartbeat()
        return bool(did or started)

    # ================================================================ planning and readiness
    def _is_active(self, p: dict) -> bool:
        return p.get("status", "active") not in INACTIVE and p["id"] not in self.paused_projects

    def _view(self, p: dict) -> tuple[list[dict], dict]:
        hit = self._views.get(p["id"])
        if hit and self.clock.mono() - hit[0] < self.sched.plan_interval_seconds:
            return hit[1], hit[2]
        nodes = walk_nodes(self.store, p["id"])
        self._views[p["id"]] = (self.clock.mono(), nodes, depth_map(nodes))
        return nodes, self._views[p["id"]][2]

    def _plan_pass(self, active: list[dict]) -> None:
        self._dirty = False
        self._next_plan = self.clock.mono() + self.sched.plan_interval_seconds
        for p in active:
            self._views.pop(p["id"], None)
            nodes, _ = self._view(p)
            if promote_ready(self.store, nodes, require_contract=self.sched.require_parent_contract_approval):
                self._views.pop(p["id"], None)
                nodes, _ = self._view(p)
            if not self.planners:
                continue
            jobs = self.store.list_jobs(p["id"], status="queued", limit=5000) + \
                self.store.list_jobs(p["id"], status="running", limit=5000)
            pc = PlanContext(self.store, p, nodes, jobs, self, self.clock.now(), {n["id"]: n for n in nodes})
            for fn in self.planners:
                try:
                    fn(pc)
                except Exception as e:
                    self._event(p["id"], "planner.error", f"planner {getattr(fn, '__name__', fn)} failed: {type(e).__name__}: {e}",
                                level="error", data={"trace": traceback.format_exc(limit=6)})
            self._views.pop(p["id"], None)

    # ================================================================ choosing and starting jobs
    def _candidates(self, active: list[dict]) -> list[Cand]:
        cands, seen_now, depth_by_role, total = [], set(), {}, 0
        for p in active:
            prof = self.profile_for(p)
            _, depth = self._view(p)
            for j in self.store.list_jobs(p["id"], status="queued", limit=5000):
                total += 1
                depth_by_role[j["model_role"]] = depth_by_role.get(j["model_role"], 0) + 1
                seen_now.add(j["id"])
                order = self._seen.setdefault(j["id"], next(self._seen_counter))
                entry = self.registry.get(j["kind"])
                if entry is None or not entry.uses_model:
                    key = None                      # unknown kinds are failed at start, which needs no model
                else:
                    try:
                        key = prof.role(j["model_role"]).model_id
                    except ValueError:
                        key = None
                d = -1 if not j.get("node_id") else depth.get(j["node_id"], 0)
                cands.append(Cand(j, key, d, order, p["id"]))
        for jid in set(self._seen) - seen_now:
            self._seen.pop(jid, None)
        self._queue_depth, self._queued_total = depth_by_role, total
        return cands

    def _gate(self) -> str:
        """Why no new job may start now, or '' when starting is allowed."""
        if self._stopping:
            return "stopping"
        if self.sched.until and self.clock.local() >= self.sched.until:
            self._stopping, self._stop_reason = True, "--until time reached"
            return "stopping"
        if self.paused:
            return "paused"
        from .windows import in_windows
        if not in_windows(self.sched.windows, self.clock.local()):
            return "out_of_window"
        return ""

    def _maybe_start(self, cands: list[Cand]) -> bool:
        if self._gate() or not cands:
            return False
        if self.clock.mono() < self._backoff_until:                  # model server was unreachable: only free jobs
            cands = [c for c in cands if c.key is None]
        started = False
        while len(self.running) < max(1, self.sched.concurrency) and cands:
            keys = {r.key for r in self.running.values()}
            c = self.batcher.choose(cands, keys, self.clock.mono())
            if c is None:
                break
            cands.remove(c)
            started |= self._start(c)
            if not self.sched.threaded:
                break                                                 # inline: one job per step, commands stay responsive
        return started

    def _start(self, c: Cand) -> bool:
        job, pid = c.job, c.project_id
        project, nid = self._projects[pid], job.get("node_id")
        entry = self.registry.get(job["kind"])
        node = self.store.get_node(nid) if nid else None
        if entry is None:
            self._end_unstarted(job, f"no handler registered for job kind {job['kind']!r} "
                                     f"(registered: {', '.join(self.registry.kinds()) or 'none'})", node)
            return False
        settings = as_obj(project.get("settings"), {}) or {}
        mult = float((as_obj(job.get("input_ref"), {}) or {}).get("budget_multiplier") or 1.0)
        budget = budget_for(self.sched.budgets, settings, mult)
        if node:
            u = self.node_usage(node)
            if job["kind"] not in self.sched.attempt_job_kinds:
                u = Usage(0, u.tokens, u.seconds)                    # attempts only limit attempt-counting job kinds
            why = UsageTracker.exceeded(u, budget)
            if why:
                self._end_unstarted(job, f"budget exceeded: {why}", node, budget_data={"usage": asdict(u), "budget": asdict(budget)})
                return False
            if job["kind"] in self.sched.attempt_job_kinds:
                self.store.increment_node_attempts(nid)
                node["attempts"] = int(node.get("attempts") or 0) + 1
        self.store.update_job(job["id"], status="running", started_at=iso(self.clock.now()))
        ctx = RunContext(self, job, node, project, self.profile_for(project), budget)
        run = JobRun(job, ctx, c.key, pid, nid, self.clock.mono())
        self.running[job["id"]] = run
        self.batcher.note_started(c, self.clock.mono())
        self._event(pid, "job.started", f"{job['kind']} started ({job['model_role']}, attempt {job.get('attempt', 1)})",
                    node_id=nid, job_id=job["id"],
                    data={"kind": job["kind"], "model_role": job["model_role"], "model_key": c.key,
                          "attempt": job.get("attempt", 1)})
        if self.sched.threaded:
            if self._pool is None:
                self._pool = ThreadPoolExecutor(max_workers=max(1, self.sched.concurrency), thread_name_prefix="nai-job")
            self._pool.submit(self._work, run, entry)
        else:
            self._work(run, entry)
            self._drain_inbox(0)
        return True

    def _work(self, run: JobRun, entry) -> None:
        """Runs in a worker thread (or inline). Never touches the Store directly."""
        try:
            with terminal.log_context(run.job["id"]):
                outcome = ("ok", entry.handler(run.ctx))
        except BaseException as e:
            outcome = ("exc", e, traceback.format_exc())
        self.inbox.put(("done", run.job["id"], outcome))

    def _end_unstarted(self, job: dict, error: str, node: dict | None, budget_data: dict | None = None) -> None:
        """A queued job that must not start (no handler, budget used up): mark it failed and, for budgets, fail the node."""
        self.store.update_job(job["id"], status="failed", ended_at=iso(self.clock.now()), error=error)
        self.stats["jobs_failed"] += 1
        pid, nid = job["project_id"], job.get("node_id")
        self._event(pid, "budget.exceeded" if budget_data else "job.failed", error, node_id=nid, job_id=job["id"],
                    level="warning", data=budget_data)
        if budget_data and node:
            self._fail_node(node, error)
        self.mark_dirty()

    def _fail_node(self, node: dict, reason: str) -> bool:
        """SCH-5: move a node to `failed` along legal edges (running/verifying -> failed; ready -> running -> failed)."""
        fresh = self.store.get_node(node["id"]) or node
        path = {"running": ["failed"], "verifying": ["failed"], "ready": ["running", "failed"]}.get(fresh["status"])
        if not path:
            self._event(fresh["project_id"], "budget.node_not_failed",
                        f"budget used up but node is {fresh['status']}, which cannot move to failed: {reason}",
                        node_id=fresh["id"], level="warning")
            return False
        for st in path:
            self.store.transition_node_status(fresh["id"], st, note=f"budget: {reason}")
        self._event(fresh["project_id"], "node.budget_failed", f"node failed: {reason}", node_id=fresh["id"], level="warning",
                    data={"reason": reason})
        return True

    # ================================================================ inbox: results, model calls, log lines, store requests
    def _drain_inbox(self, wait: float) -> bool:
        did = False
        first = True
        while True:
            try:
                msg = self.inbox.get(timeout=wait) if (first and wait > 0) else self.inbox.get_nowait()
            except queue.Empty:
                return did
            first = False
            did = True
            kind = msg[0]
            if kind == "store":
                serve(msg)
            elif kind == "rec":
                self._on_call_record(msg[1])
            elif kind == "log":
                self._on_log(msg)
            elif kind == "done":
                self._finish_job(msg[1], msg[2])

    def _observer(self, rec) -> None:                       # LLM.observers callback: any thread
        jid = (rec.meta or {}).get("job_id")
        if jid in self.running:
            self.inbox.put(("rec", rec))

    def _sink(self, tag, text, color, ctx) -> None:        # terminal sink: any thread
        if self.sched.record_log_events and ctx in self.running:
            self.inbox.put(("log", ctx, tag, text, color))

    def _on_log(self, msg) -> None:
        _, jid, tag, text, color = msg
        run = self.running.get(jid)
        if run is None or run.log_count >= self.sched.max_log_events_per_job:
            return
        run.log_count += 1
        self._event(run.project_id, "job.log", f"[{tag}] {text}"[:2000], node_id=run.node_id, job_id=jid,
                    level=LOG_LEVEL.get(color, "info"), data={"tag": tag})

    def _on_call_record(self, rec) -> None:
        """X6: one event per model call (tokens, seconds, swap) plus the full prompt and response as an artifact."""
        jid = rec.meta["job_id"]
        run = self.running.get(jid)
        if run is None:
            return
        run.calls += 1
        art = None
        if self.sched.record_call_artifacts and self._projects.get(run.project_id, {}).get("workspace_path"):
            try:
                art = self.store.add_artifact(run.project_id, "log", json.dumps(asdict(rec), ensure_ascii=False, default=str),
                                              node_id=run.node_id, filename=f"{jid}-call-{run.calls:03d}.json")
            except Exception as e:
                self._event(run.project_id, "artifact.error", f"could not store call artifact: {e}", node_id=run.node_id,
                            job_id=jid, level="warning")
        self.stats["model_calls"] += 1
        self.stats["tokens_in"] += rec.prompt_tokens or 0
        self.stats["tokens_out"] += rec.completion_tokens or 0
        msg = (f"{rec.model} {rec.tag}: {rec.prompt_tokens}→{rec.completion_tokens} tokens in {rec.seconds:.1f}s"
               + (" (model swap)" if rec.swapped else "") + (f" ERROR {rec.error}" if rec.error else ""))
        self._event(run.project_id, "model.call", msg, node_id=run.node_id, job_id=jid, level="warning" if rec.error else "info",
                    data={"model": rec.model, "role": rec.meta.get("role"), "tag": rec.tag, "prompt_tokens": rec.prompt_tokens,
                          "completion_tokens": rec.completion_tokens, "seconds": rec.seconds,
                          "first_token_seconds": rec.first_token_seconds, "swapped": rec.swapped, "swap_method": rec.swap_method,
                          "finish_reason": rec.finish_reason, "usage_estimated": rec.usage_estimated, "error": rec.error,
                          "prompts": rec.meta.get("prompts"), "artifact_id": art})
        if rec.error:
            return
        self._backoff_until, self._backoff_delay = 0.0, self.sched.backoff_start_seconds
        self.batcher.note_loaded(rec.model)
        if rec.swapped:
            # first-token latency is the cost of the load plus prompt processing: an upper bound for the swap itself
            secs = rec.first_token_seconds if rec.first_token_seconds is not None else rec.seconds
            self.stats["swaps"] += 1
            self.stats["swap_seconds"] += secs
            self._event(run.project_id, "model.swap", f"model swap to {rec.model}: ~{secs:.1f}s", node_id=run.node_id, job_id=jid,
                        data={"model": rec.model, "seconds": secs, "method": rec.swap_method,
                              "seconds_is_estimate": True})

    def _finish_job(self, job_id: str, outcome: tuple) -> None:
        run = self.running.pop(job_id, None)
        if run is None:
            return
        ctx, job, pid, nid = run.ctx, run.job, run.project_id, run.node_id
        seconds = self.clock.mono() - run.started
        status, error, result, requeue, budget_hit, trace = "succeeded", None, {}, False, False, None
        if outcome[0] == "ok":
            res = outcome[1]
            if res is None:
                res = JobResult()
            elif isinstance(res, dict):
                res = JobResult.success(**res)
            elif not isinstance(res, JobResult):
                res = JobResult.failure(f"handler returned {type(res).__name__}, expected JobResult")
            result, error, requeue = dict(res.result), res.error, res.requeue
            status = "succeeded" if res.ok else "failed"
            if not res.ok and ctx.cancel_event.is_set() and ctx.cancel_reason == "cancel":
                status = "cancelled"
        else:
            exc, trace = outcome[1], outcome[2]
            if isinstance(exc, JobCancelled):
                if ctx.cancel_reason == "shutdown":
                    requeue = True
                else:
                    status, error = "cancelled", "cancelled by owner"
            elif isinstance(exc, BudgetExceeded):
                status, error, budget_hit = "failed", f"budget exceeded: {exc}", True
            elif is_transient(exc):
                n = self._transient[job_id] = self._transient.get(job_id, 0) + 1
                if n > self.sched.transient_retries:
                    status, error = "failed", f"model server unreachable after {n} attempts: {exc}"
                else:
                    requeue = True
                    self._enter_backoff(pid, nid, job_id, exc)
            else:
                status, error = "failed", f"{type(exc).__name__}: {exc}"
        if nid:
            self.tracker.add(nid, ctx.tokens, seconds)
        now = iso(self.clock.now())
        if requeue:
            self.store.update_job(job_id, status="queued", started_at=None)
            self.stats["jobs_requeued"] += 1
            self._event(pid, "job.requeued", f"{job['kind']} put back in the queue" + (f": {error}" if error else ""),
                        node_id=nid, job_id=job_id, level="warning")
        else:
            self.store.update_job(job_id, status=status, ended_at=now, tokens_in=ctx.tokens_in, tokens_out=ctx.tokens_out,
                                  result_ref=result or None, error=error)
            self.stats[{"succeeded": "jobs_succeeded", "failed": "jobs_failed", "cancelled": "jobs_cancelled"}[status]] += 1
            lvl = {"succeeded": "info", "cancelled": "warning"}.get(status, "error")
            self._event(pid, "job.finished", f"{job['kind']} {status} in {seconds:.1f}s ({ctx.tokens} tokens)"
                        + (f": {error}" if error else ""), node_id=nid, job_id=job_id, level=lvl,
                        data={"status": status, "seconds": seconds, "tokens_in": ctx.tokens_in, "tokens_out": ctx.tokens_out,
                              "calls": ctx.calls, "error": error, "trace": trace if status == "failed" and trace else None})
            self._transient.pop(job_id, None)
        if budget_hit and ctx.node:
            self._event(pid, "budget.exceeded", error, node_id=nid, job_id=job_id, level="warning",
                        data={"usage": asdict(self.node_usage(ctx.node)), "budget": asdict(ctx.budget)})
            self._fail_node(ctx.node, error)
        self.mark_dirty()

    def _enter_backoff(self, pid: str, nid: str | None, job_id: str, exc: BaseException) -> None:
        if self.clock.mono() >= self._backoff_until:
            self._event(pid, "runner.server_unavailable",
                        f"model server unreachable ({type(exc).__name__}); model jobs wait {self._backoff_delay:.0f}s",
                        node_id=nid, job_id=job_id, level="warning")
        self._backoff_until = self.clock.mono() + self._backoff_delay
        self._backoff_delay = min(self._backoff_delay * 2, self.sched.backoff_max_seconds)

    # ================================================================ commands, recovery, health
    def _process_commands(self) -> bool:
        cmds = self.store.claim_commands(limit=20)
        for c in cmds:
            err = self.commands.process(c)
            cid = c.get("id", c.get("command_id"))
            self.store.complete_command(cid, error=err)
            if c.get("project_id"):
                self._event(c["project_id"], "command.processed", f"{c.get('action')}" + (f" failed: {err}" if err else " done"),
                            node_id=c.get("node_id"), level="warning" if err else "info",
                            data={"command_id": cid, "action": c.get("action"), "error": err})
        if cmds:
            self.mark_dirty()
        return bool(cmds)

    def recover(self) -> dict:
        """SCH-7 / X4: jobs left `running` by a crash go back to `queued`; the report goes to events."""
        report = {"requeued_jobs": 0, "orphan_nodes": 0}
        for p in self.store.list_projects():
            if p.get("status") == "archived":
                continue
            pid = p["id"]
            for j in self.store.list_jobs(pid, status="running", limit=5000):
                self.store.update_job(j["id"], status="queued", started_at=None)
                report["requeued_jobs"] += 1
                self._event(pid, "runner.recovered_job", f"{j['kind']} was running when the runner stopped; queued again",
                            node_id=j.get("node_id"), job_id=j["id"], level="warning")
            active = {j.get("node_id") for st in ("queued", "running") for j in self.store.list_jobs(pid, status=st, limit=5000)}
            for n in walk_nodes(self.store, pid):
                if n["status"] in ("running", "verifying") and n["id"] not in active:
                    report["orphan_nodes"] += 1
                    self._event(pid, "runner.orphan_node", f"node {n.get('name', n['id'])} is {n['status']} but has no job",
                                node_id=n["id"], level="warning")
            self._event(pid, "runner.started", "runner started", data=dict(report))
        self.mark_dirty()
        return report

    def _restore_pause_state(self) -> None:
        """Pause state survives a restart. Project pause is read from the project row when the store can set it, and from
        project.paused / project.resumed events otherwise; a runner-wide pause is read from runner.paused events."""
        runner_state: list[tuple[str, int, bool]] = []
        for p in self.store.list_projects():
            events = self.store.list_events(p["id"], limit=100000)
            ordered = sorted(enumerate(events), key=lambda t: (str(t[1].get("time") or ""), t[0]))
            state = None
            for i, e in ordered:
                t = e.get("type", e.get("event_type"))
                if t in ("project.paused", "project.resumed"):
                    state = t == "project.paused"
                elif t in ("runner.paused", "runner.resumed"):
                    runner_state.append((str(e.get("time") or ""), i, t == "runner.paused"))
            if hasattr(self.store, "set_project_status"):
                paused = p.get("status") == "paused"            # the project row is authoritative
            else:
                paused = bool(state)                           # no row to ask: the last pause/resume event decides
            if paused:
                self.paused_projects.add(p["id"])
        if runner_state:
            self.paused = sorted(runner_state)[-1][2]

    def _state(self) -> str:
        if self._stopping:
            return "stopping" if self.running else "stopped"
        gate = self._gate()
        if gate:
            return gate
        if self.clock.mono() < self._backoff_until:
            return "backoff"
        return "running" if self.running else "idle"

    def _heartbeat(self) -> None:
        state = self._state()
        if state != self._last_state or self.clock.mono() - self._last_hb >= self.sched.heartbeat_seconds:
            self._write_health(state)

    def _write_health(self, state: str) -> None:
        self._last_state, self._last_hb = state, self.clock.mono()
        write_health(self.state_dir, {
            "pid": os.getpid(), "started_at": self._started_at, "heartbeat": iso(self.clock.now()),
            "state": state, "stop_reason": self._stop_reason or None, "paused": self.paused,
            "paused_projects": sorted(self.paused_projects),
            "current_jobs": [{"job_id": jid, "kind": r.job["kind"], "node_id": r.node_id, "project_id": r.project_id,
                              "model": r.key, "running_seconds": round(self.clock.mono() - r.started, 1)}
                             for jid, r in self.running.items()],
            "loaded_model": self.batcher.loaded_key, "batch_model": self.batcher.batch_key,
            "batch_jobs": self.batcher.consecutive, "queue_depth": dict(self._queue_depth), "queued_total": self._queued_total,
            "window": list(self.sched.windows), "until": self.sched.until.isoformat() if self.sched.until else None,
            "backoff_seconds": max(0.0, round(self._backoff_until - self.clock.mono(), 1)), "stats": dict(self.stats),
            "handlers": self.registry.kinds(), "concurrency": self.sched.concurrency})

    def _event(self, project_id: str, etype: str, message: str, *, node_id=None, job_id=None, level="info", data=None) -> None:
        self.store.record_event(project_id, etype, message, node_id=node_id, job_id=job_id, level=level, data=data)

    def _ensure_usage(self, pid: str) -> None:
        """Rebuild budget usage from stored jobs and `node.retry` events the first time a project is seen (restart-safe)."""
        if pid in self._usage_loaded:
            return
        self._usage_loaded.add(pid)
        bases: dict[str, Usage] = {}
        events = self.store.list_events(pid, limit=100000)
        for i, e in sorted(enumerate(events), key=lambda t: (str(t[1].get("time") or ""), t[0])):
            if e.get("type", e.get("event_type")) == "node.retry" and e.get("node_id"):
                d = as_obj(e.get("data"), {}) or {}
                bases[e["node_id"]] = Usage(int(d.get("attempts_base", 0)), int(d.get("tokens_base", 0)), float(d.get("seconds_base", 0.0)))
        self.tracker.load(self.store.list_jobs(pid, limit=1_000_000), bases)
