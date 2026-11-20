"""Thin supervisor daemon: starts and watches the web UI, runner, and on-demand nai jobs."""
from __future__ import annotations

import fcntl
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import ServiceConfig
from .process import ManagedProcess, _now
from .protocol import read_request, write_response


class ServiceAlreadyRunning(RuntimeError):
    pass


class ServiceNotRunning(RuntimeError):
    pass


class ServiceDaemon:
    """Supervises child processes and answers control requests on a Unix socket."""

    def __init__(self, config: ServiceConfig):
        self.config = config
        self.state_dir = config.state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        config.log_dir.mkdir(parents=True, exist_ok=True)
        self.children: dict[str, ManagedProcess] = {}
        self.jobs: dict[str, ManagedProcess] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._server: socket.socket | None = None
        self._lock_fh = None
        self.started_at: str | None = None
        self._build_children()

    # ---------------------------------------------------------------- build children
    def _build_children(self) -> None:
        py = self.config.python or sys.executable
        env = self.config.child_env()
        common = dict(
            cwd=Path.cwd(),
            env=env,
            auto_restart=self.config.auto_restart,
            restart_limit=self.config.restart_limit,
            restart_backoff=self.config.restart_backoff,
            stop_timeout=self.config.stop_timeout,
        )
        if self.config.start_web:
            self.children["web"] = ManagedProcess(
                name="web",
                command=[py, "-m", "nai.web", "--host", self.config.web_host,
                         "--port", str(self.config.web_port)],
                log_path=self.config.log_dir / "web.log",
                **common,
            )
        if self.config.start_runner:
            self.children["runner"] = ManagedProcess(
                name="runner",
                command=[py, "-m", "nai", "run", "--db", str(self.config.effective_db_path),
                         *self.config.runner_args],
                log_path=self.config.log_dir / "runner.log",
                **common,
            )

    # ---------------------------------------------------------------- single instance
    def acquire_lock(self) -> None:
        path = self.state_dir / "service.lock"
        fh = open(path, "a+")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            fh.seek(0)
            holder = fh.read().strip() or "unknown"
            fh.close()
            raise ServiceAlreadyRunning(f"service already running (pid {holder})") from exc
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        self._lock_fh = fh
        self.config.pid_path.write_text(str(os.getpid()), encoding="utf-8")

    def release_lock(self) -> None:
        if self._lock_fh:
            try:
                fcntl.flock(self._lock_fh, fcntl.LOCK_UN)
                self._lock_fh.close()
            finally:
                self._lock_fh = None
        try:
            self.config.pid_path.unlink()
        except OSError:
            pass

    # ---------------------------------------------------------------- control socket
    def _serve_control(self) -> None:
        sock_path = self.config.socket_path
        try:
            sock_path.unlink()
        except OSError:
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(sock_path))
        server.listen(16)
        server.settimeout(0.5)
        self._server = server
        try:
            while not self._stop.is_set():
                try:
                    conn, _ = server.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                threading.Thread(target=self._handle_conn, args=(conn,), daemon=True).start()
        finally:
            server.close()
            self._server = None
            try:
                sock_path.unlink()
            except OSError:
                pass

    def _handle_conn(self, conn: socket.socket) -> None:
        try:
            req = read_request(conn)
            if req is None:
                return
            cmd = str(req.get("cmd", ""))
            args = req.get("args") or {}
            try:
                data = self.dispatch(cmd, args)
                write_response(conn, ok=True, data=data)
            except Exception as exc:  # noqa: BLE001 - control protocol reports errors as data
                write_response(conn, ok=False, error=f"{type(exc).__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            try:
                write_response(conn, ok=False, error=f"protocol error: {exc}")
            except OSError:
                pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def dispatch(self, cmd: str, args: dict[str, Any]) -> Any:
        """Handle a control command. Also usable directly in tests."""
        if cmd == "status":
            return self.status()
        if cmd == "logs":
            return self.tail_log(str(args.get("name", "")), int(args.get("lines", 100)))
        if cmd == "stop":
            threading.Thread(target=self._delayed_stop, daemon=True).start()
            return {"stopping": True}
        if cmd == "restart":
            name = str(args.get("name", ""))
            return self.restart_component(name)
        if cmd == "exec":
            argv = args.get("argv") or []
            if isinstance(argv, str):
                argv = argv.split()
            if not argv:
                raise ValueError("exec needs argv")
            return self.spawn_job([str(a) for a in argv], name=args.get("name"))
        if cmd == "jobs":
            return {"jobs": {jid: proc.status() for jid, proc in self.jobs.items()}}
        if cmd == "job":
            job = self.jobs.get(str(args.get("id", "")))
            if not job:
                raise KeyError("unknown job")
            return job.status()
        if cmd == "cancel":
            job = self.jobs.get(str(args.get("id", "")))
            if not job:
                raise KeyError("unknown job")
            job.stop()
            return {"cancelled": True}
        raise ValueError(f"unknown command: {cmd}")

    def _delayed_stop(self) -> None:
        time.sleep(0.1)
        self._stop.set()

    # ---------------------------------------------------------------- lifecycle helpers
    def status(self) -> dict[str, Any]:
        health = None
        try:
            from ..sched.health import read_health

            health = read_health(self.state_dir)
        except Exception:
            pass
        return {
            "pid": os.getpid(),
            "started_at": self.started_at,
            "state_dir": str(self.state_dir),
            "db": str(self.config.effective_db_path),
            "socket": str(self.config.socket_path),
            "web_url": f"http://{self.config.web_host}:{self.config.web_port}",
            "children": {name: proc.status() for name, proc in self.children.items()},
            "jobs": {jid: proc.status() for jid, proc in self.jobs.items()},
            "runner_health": health,
        }

    def tail_log(self, name: str, lines: int = 100) -> str:
        if name in self.children:
            return self.children[name].tail_log(lines)
        if name in self.jobs:
            return self.jobs[name].tail_log(lines)
        if name in ("service", "daemon", ""):
            path = self.config.log_dir / "service.log"
            if path.exists():
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    return "".join(fh.readlines()[-lines:])
            return ""
        raise KeyError(f"unknown log: {name}")

    def restart_component(self, name: str) -> dict[str, Any]:
        if name in self.children:
            self.children[name].restart()
            return {"restarted": name}
        if name in self.jobs:
            self.jobs[name].restart()
            return {"restarted": name}
        raise KeyError(f"unknown component: {name}")

    def spawn_job(self, argv: list[str], name: str | None = None) -> dict[str, Any]:
        """Spawn a new `nai ...` instance as a supervised job."""
        py = self.config.python or sys.executable
        job_id = name or f"job-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        if job_id in self.jobs:
            raise ValueError(f"duplicate job id: {job_id}")
        proc = ManagedProcess(
            name=job_id,
            command=[py, "-m", "nai", *argv],
            cwd=Path.cwd(),
            env=self.config.child_env(),
            log_path=self.config.log_dir / f"{job_id}.log",
            auto_restart=False,
            stop_timeout=self.config.stop_timeout,
        )
        proc.start()
        self.jobs[job_id] = proc
        return {"id": job_id, "pid": proc.pid, "command": proc.command}

    # ---------------------------------------------------------------- main loop
    def _watch_children(self) -> None:
        for name, proc in self.children.items():
            state = proc.poll()
            if state == "crashed":
                if proc.auto_restart and proc.restarts < proc.restart_limit:
                    proc.restarts += 1
                    delay = proc.restart_backoff * proc.restarts
                    time.sleep(min(delay, 10.0))
                    proc.start()
                else:
                    proc.retire()
        for jid, proc in list(self.jobs.items()):
            if proc.poll() != "running":
                continue  # keep finished jobs for status queries; explicit cleanup removes them

    def run_forever(self) -> int:
        """Supervisor main loop (blocking)."""
        self.acquire_lock()
        self.started_at = _now()
        thread = threading.Thread(target=self._serve_control, daemon=True)
        thread.start()
        for proc in self.children.values():
            proc.start()

        def on_signal(signum, frame):
            self._stop.set()

        old = {}
        try:
            for sig in (signal.SIGINT, signal.SIGTERM):
                old[sig] = signal.signal(sig, on_signal)
        except ValueError:
            pass  # not main thread (tests)

        try:
            while not self._stop.is_set():
                self._watch_children()
                self._stop.wait(self.config.poll_interval)
        finally:
            for sig, handler in old.items():
                try:
                    signal.signal(sig, handler)
                except ValueError:
                    pass
            for proc in self.children.values():
                proc.stop()
            for proc in self.jobs.values():
                if proc.running:
                    proc.stop()
            # Control thread is a daemon thread: clean up the socket here too
            try:
                self.config.socket_path.unlink()
            except OSError:
                pass
            self.release_lock()
        return 0

    def start_detached(self) -> int:
        """Spawn a detached supervisor process and return its PID."""
        if self.config.socket_path.exists():
            raise ServiceAlreadyRunning("service socket exists; run `nai service status` first")
        py = self.config.python or sys.executable
        log_path = self.config.log_dir / "service.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(log_path, "ab", buffering=0)
        try:
            proc = subprocess.Popen(  # noqa: F821 - imported below to keep module import light
                [py, "-m", "nai.service.daemon", "--state-dir", str(self.config.state_dir)],
                cwd=Path.cwd(), env=self.config.child_env(),
                stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            fh.close()
        return proc.pid


def daemon_main(argv: list[str] | None = None) -> int:
    """Entry point for `python -m nai.service.daemon`."""
    import argparse

    parser = argparse.ArgumentParser(prog="nai-service", description="nai supervisor daemon")
    parser.add_argument("--state-dir", default=None, help="State directory (default NAI_STATE_DIR or .nai)")
    parser.add_argument("--host", default=None, help="Web bind address")
    parser.add_argument("--port", type=int, default=None, help="Web port")
    parser.add_argument("--no-web", action="store_true", help="Do not start the web interface")
    parser.add_argument("--no-runner", action="store_true", help="Do not start the runner")
    parser.add_argument("--foreground", action="store_true", help="Run in the foreground (default for this entry point)")
    args = parser.parse_args(argv)

    cfg = ServiceConfig.from_env(
        args.state_dir,
        web_host=args.host,
        web_port=args.port,
        start_web=False if args.no_web else None,
        start_runner=False if args.no_runner else None,
    )
    daemon = ServiceDaemon(cfg)
    return daemon.run_forever()


if __name__ == "__main__":
    raise SystemExit(daemon_main())

