"""`nai service` control CLI and the interactive submission shell."""
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

from .config import ServiceConfig
from .daemon import ServiceDaemon, daemon_main
from .protocol import request


def _cfg(args: argparse.Namespace) -> ServiceConfig:
    return ServiceConfig.from_env(
        getattr(args, "state_dir", None),
        web_host=getattr(args, "host", None),
        web_port=getattr(args, "port", None),
    )


def _call(cfg: ServiceConfig, cmd: str, **kwargs: Any) -> Any:
    if not cfg.socket_path.exists():
        raise RuntimeError("service is not running (run `nai service start` first)")
    response = request(cfg.socket_path, cmd, **kwargs)
    if not response.get("ok"):
        raise RuntimeError(response.get("error") or "control command failed")
    return response.get("data")


def service_main(argv: list[str] | None = None) -> int:
    """Service main."""
    parser = argparse.ArgumentParser(prog="nai service", description="Manage the nai background service")
    parser.add_argument("--state-dir", default=None, help="State directory (default NAI_STATE_DIR or .nai)")
    parser.add_argument("--host", default=None, help="Web bind address")
    parser.add_argument("--port", type=int, default=None, help="Web port")
    sub = parser.add_subparsers(dest="action")

    start = sub.add_parser("start", help="Start the supervisor (web + runner)")
    start.add_argument("--foreground", action="store_true", help="Run in the foreground")
    start.add_argument("--no-web", action="store_true")
    start.add_argument("--no-runner", action="store_true")

    sub.add_parser("stop", help="Stop the service and all children")
    sub.add_parser("restart", help="Restart the service").add_argument("name", nargs="?", help="web, runner, or job id")
    status = sub.add_parser("status", help="Show service, child and runner status")
    status.add_argument("--json", action="store_true", help="Emit raw JSON")
    logs = sub.add_parser("logs", help="Tail a component log")
    logs.add_argument("name", nargs="?", default="service", help="web, runner, service, or job id")
    logs.add_argument("-n", "--lines", type=int, default=100)

    run = sub.add_parser("exec", help="Submit a new nai instance to the service")
    run.add_argument("--name", help="Optional job id")
    run.add_argument("argv", nargs=argparse.REMAINDER, help="Arguments for `nai`")
    sub.add_parser("jobs", help="List submitted jobs")
    cancel = sub.add_parser("cancel", help="Cancel a submitted job")
    cancel.add_argument("id")
    sub.add_parser("shell", help="Interactive job submission shell")

    args = parser.parse_args(argv)
    action = args.action or "status"
    cfg = _cfg(args)

    try:
        if action == "start":
            if args.foreground:
                return daemon_main(["--state-dir", str(cfg.state_dir), "--host", cfg.web_host,
                                    "--port", str(cfg.web_port)] +
                                   (["--no-web"] if args.no_web else []) +
                                   (["--no-runner"] if args.no_runner else []))
            daemon = ServiceDaemon(cfg)
            pid = daemon.start_detached()
            print(f"Service started (pid {pid}); web at http://{cfg.web_host}:{cfg.web_port}")
            return 0

        if action == "status":
            if cfg.socket_path.exists():
                data = _call(cfg, "status")
            else:
                data = {"running": False, "socket": str(cfg.socket_path)}
            if args.json:
                print(json.dumps(data, indent=2, default=str))
            else:
                _print_status(data)
            return 0

        if action == "stop":
            if not cfg.socket_path.exists():
                print("Service is not running")
                return 0
            _call(cfg, "stop")
            for _ in range(60):
                if not cfg.socket_path.exists():
                    print("Service stopped")
                    return 0
                time.sleep(0.25)
            print("Service did not stop within 15s", file=sys.stderr)
            return 1

        if action == "restart":
            if cfg.socket_path.exists():
                data = _call(cfg, "restart", name=args.name)
            else:
                daemon = ServiceDaemon(cfg)
                daemon.start_detached()
                data = {"restarted": "service"}
            print(json.dumps(data, default=str))
            return 0

        if action == "logs":
            print(_call(cfg, "logs", name=args.name, lines=args.lines), end="")
            return 0

        if action == "exec":
            argv_list = list(args.argv or [])
            if argv_list and argv_list[0] == "--":
                argv_list = argv_list[1:]
            if not argv_list:
                parser.error("exec needs nai arguments")
            data = _call(cfg, "exec", argv=argv_list, name=args.name)
            print(json.dumps(data, default=str))
            return 0

        if action == "jobs":
            data = _call(cfg, "jobs")
            for job, info in data.get("jobs", {}).items():
                print(f"{job:24} pid={info.get('pid')} state={info.get('state')}")
            return 0

        if action == "cancel":
            data = _call(cfg, "cancel", id=args.id)
            print(json.dumps(data, default=str))
            return 0

        if action == "shell":
            return _shell(cfg)

        parser.error(f"unknown action: {action}")
    except (RuntimeError, OSError, ValueError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


def _print_status(data: dict[str, Any]) -> None:
    if not data.get("running", True):
        print("Service: STOPPED")
        print(f"Socket:  {data.get('socket')}")
        return
    print(f"Service: running (pid {data.get('pid')})")
    print(f"Web:     {data.get('web_url')}")
    print(f"Store:   {data.get('db')}")
    print("Children:")
    for name, info in (data.get("children") or {}).items():
        print(f"  {name:10} {info.get('state'):8} pid={info.get('pid')} restarts={info.get('restarts')}")
    health = data.get("runner_health") or {}
    if health:
        print(f"Runner:  {health.get('state')} alive={health.get('alive')} "
              f"heartbeat={health.get('heartbeat')}")
    jobs = data.get("jobs") or {}
    if jobs:
        print("Jobs:")
        for jid, info in jobs.items():
            print(f"  {jid:24} {info.get('state'):8} pid={info.get('pid')}")


def _shell(cfg: ServiceConfig) -> int:
    """Interactive CLI that submits new nai instances and reports their status."""
    print("nai service shell — type 'help', 'status', 'jobs', 'logs ID', or nai arguments.")
    while True:
        try:
            line = input("nai> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        if line in ("exit", "quit"):
            return 0
        if line == "help":
            print("  status              service status")
            print("  jobs                list submitted jobs")
            print("  job ID              show one job")
            print("  logs [ID]           tail a log")
            print("  cancel ID           cancel a job")
            print("  <args...>           submit `nai <args...>` as a new job")
            continue
        try:
            if line == "status":
                _print_status(_call(cfg, "status"))
            elif line == "jobs":
                for jid, info in (_call(cfg, "jobs") or {}).get("jobs", {}).items():
                    print(f"  {jid:24} {info.get('state'):8} pid={info.get('pid')}")
            elif line.startswith("job "):
                print(json.dumps(_call(cfg, "job", id=line.split(None, 1)[1]), indent=2, default=str))
            elif line.startswith("logs"):
                parts = line.split()
                name = parts[1] if len(parts) > 1 else "service"
                print(_call(cfg, "logs", name=name, lines=50), end="")
            elif line.startswith("cancel "):
                print(_call(cfg, "cancel", id=line.split(None, 1)[1]))
            else:
                data = _call(cfg, "exec", argv=line.split())
                print(f"Submitted: {data['id']} (pid {data['pid']})")
        except Exception as exc:  # noqa: BLE001 - shell keeps running after errors
            print(f"ERROR: {exc}")
    return 0

