"""`nai run`: the long-running runner process (SCH-1)."""
from __future__ import annotations

import argparse
import signal
import sys
from pathlib import Path

from .. import terminal
from ..cli import register_subcommand
from ..config import ConfigError, find_config, load_config
from .config import SchedConfig
from .health import RunnerAlreadyRunning
from .plugins import load_plugins
from .runner import Runner
from .util import SystemClock
from .windows import parse_until, parse_window


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="nai run", description="Run the nai scheduler: executes queued jobs from the store.")
    ap.add_argument("--db", default=".nai/nai.sqlite3", help="SQLite store (default .nai/nai.sqlite3)")
    ap.add_argument("--state-dir", help="Where runner.json (health) and runner.lock live (default: the database's folder)")
    ap.add_argument("--config", metavar="FILE", help="TOML config (default: $NAI_CONFIG, then ./nai.toml); see [runner] table")
    ap.add_argument("--base-url", help="Model server URL (env LLAMA_SWAP_URL)")
    ap.add_argument("--api-key", help="Model server key (env LLAMA_SWAP_KEY)")
    ap.add_argument("--drain", action="store_true", help="Exit when nothing is queued or running instead of waiting for work")
    ap.add_argument("--until", metavar="WHEN", help="Start no new job after this local time (HH:MM, or ISO date-time); then exit (SCH-8)")
    ap.add_argument("--window", action="append", metavar="HH:MM-HH:MM", help="Only start jobs inside this local-time window; repeatable (SCH-8)")
    ap.add_argument("--concurrency", type=int, help="Jobs at once (default 1)")
    ap.add_argument("--max-consecutive", type=int, help="Cap on consecutive jobs for one model before switching (SCH-2)")
    ap.add_argument("--max-batch-seconds", type=float, help="Cap on seconds spent on one model before switching (SCH-2)")
    ap.add_argument("--poll", type=float, help="Seconds between polls when idle (default 1)")
    ap.add_argument("--plugin", action="append", metavar="MODULE", help="Plugin module with register(runner); repeatable. Default: nai.spec, nai.build")
    ap.add_argument("--no-call-artifacts", action="store_true", help="Do not store the full prompt and reply of every model call")
    ap.add_argument("--backup-dir", help="Daily database backups go here (STO-8)")
    ap.add_argument("--backup-keep", type=int, default=7, help="Backups to keep (default 7)")
    return ap


def main(argv: list[str] | None = None, *, store_factory=None, llm_factory=None, registrar=None) -> int:
    """store_factory(db_path, backup_dir, backup_keep) -> Store, llm_factory(app_config, args) -> LLM, and
    registrar(runner) exist so tests (and embedding programs) can supply their own."""
    a = build_parser().parse_args(argv)
    try:
        app = load_config(a.config)
        cfg = SchedConfig.from_toml(find_config(a.config))
        for flag, attr in (("concurrency", "concurrency"), ("max_consecutive", "max_consecutive_jobs"),
                           ("max_batch_seconds", "max_batch_seconds"), ("poll", "poll_seconds")):
            if getattr(a, flag) is not None:
                setattr(cfg, attr, getattr(a, flag))
        if a.window:
            cfg.windows = list(a.window)
        for w in cfg.windows:
            parse_window(w)
        if a.until:
            cfg.until = parse_until(a.until, SystemClock().local())
        if a.plugin:
            cfg.plugins = list(a.plugin)
        if a.no_call_artifacts:
            cfg.record_call_artifacts = False
        if cfg.concurrency > 1 and not cfg.threaded:
            raise ValueError("concurrency > 1 needs threaded = true")
        if a.base_url:
            app.server.base_url = a.base_url
        if a.api_key:
            app.server.api_key = a.api_key
    except (ConfigError, ValueError) as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2

    db = Path(a.db)
    try:
        if store_factory:
            store = store_factory(db, a.backup_dir, a.backup_keep)
        else:
            try:
                from nai.store import Store
            except ImportError:
                print("error: the SS-STORE package (nai.store, work package WP2) is not installed", file=sys.stderr)
                return 2
            db.parent.mkdir(parents=True, exist_ok=True)
            kw = {"backup_dir": a.backup_dir, "backup_keep": a.backup_keep} if a.backup_dir else {}
            store = Store(str(db), **kw)
    except Exception as e:
        print(f"error: cannot open store {db}: {type(e).__name__}: {e}", file=sys.stderr)
        return 2

    from ..llm import LLM, MissingDependency
    try:
        llm = llm_factory(app, a) if llm_factory else LLM(app.server.base_url, app.server.api_key, timeout=app.server.timeout,
                                                          stream=app.server.stream, show_thinking=False,
                                                          swap_threshold=app.server.swap_threshold_seconds)
    except MissingDependency as e:
        print(str(e), file=sys.stderr)
        return 2

    runner = Runner(store, llm, app_config=app, sched=cfg, state_dir=a.state_dir or db.parent)
    loaded = (registrar(runner) or []) if registrar else load_plugins(runner, cfg.plugins)
    terminal.log("run", f"db={db} plugins={','.join(loaded) or 'none'} concurrency={cfg.concurrency}"
                 + (f" windows={cfg.windows}" if cfg.windows else "") + (f" until={cfg.until:%Y-%m-%d %H:%M}" if cfg.until else ""), "cyan")
    if not runner.registry.kinds():
        terminal.log("run", "no job handlers are registered: queued jobs will fail. Install the spec/build packages or pass --plugin.", "yellow")

    presses = {"n": 0}

    def on_signal(signum, frame):
        presses["n"] += 1
        if presses["n"] == 1:
            terminal.log("run", "stopping after the running jobs finish (press Ctrl-C again to cancel them and requeue)", "yellow")
            runner.stop(graceful=True)
        else:
            terminal.log("run", "cancelling running jobs; they will be queued again", "red")
            runner.stop(graceful=False)

    old = {s: signal.signal(s, on_signal) for s in (signal.SIGINT, signal.SIGTERM)} if _main_thread() else {}
    try:
        stats = runner.run(drain=a.drain)
    except RunnerAlreadyRunning as e:
        print(f"error: {e}", file=sys.stderr)
        return 3
    finally:
        for s, h in old.items():
            signal.signal(s, h)
        close = getattr(store, "close", None)
        if close:
            close()
    terminal.log("run", "stopped: " + ", ".join(f"{k}={v}" for k, v in stats.items() if k != "steps"), "cyan")
    return 0


def _main_thread() -> bool:
    import threading
    return threading.current_thread() is threading.main_thread()


register_subcommand("run", main)
