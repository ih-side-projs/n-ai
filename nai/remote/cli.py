"""``nai worker`` and ``nai remote`` command line.

``nai worker add|list|revoke`` manages the worker registry; the token is shown
exactly once.  ``nai remote serve`` runs the worker API on its own listener,
and ``nai remote status`` summarises workers and recent tasks.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from pathlib import Path

from ..store import Store
from ..store.errors import StoreError
from .errors import RemoteError
from .server import check_bind, serve_worker_api


def default_db_path() -> Path:
    """Same default the dashboard uses: ``NAI_DB_PATH`` then ``.nai/nai.sqlite3``."""
    return Path(os.environ.get("NAI_DB_PATH", ".nai/nai.sqlite3")).expanduser().resolve()


def _open_store(path: str | Path | None, *, read_only: bool = False) -> Store:
    db = Path(path).expanduser().resolve() if path else default_db_path()
    if not db.is_file():
        raise RemoteError(f"database not found: {db} (run the runner or `nai doctor` first)")
    return Store(db, read_only=read_only)


def _split(values: list[str] | None, inline: str | None) -> list[str]:
    caps: list[str] = []
    for value in values or []:
        caps.extend(part.strip() for part in str(value).split(",") if part.strip())
    if inline:
        caps.extend(part.strip() for part in inline.split(",") if part.strip())
    return caps


def _toolchains(values: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for value in values or []:
        name, sep, version = str(value).partition("=")
        if not sep or not name.strip():
            raise RemoteError(f"--toolchain must be NAME=VERSION, got {value!r}")
        out[name.strip()] = version.strip()
    return out


def _cmd_add(args: argparse.Namespace) -> int:
    caps = _split(args.capability, args.capabilities)
    token = secrets.token_urlsafe(32)
    with _open_store(args.db) as store:
        worker = store.add_worker(
            args.name,
            Store.hash_worker_token(token),
            capabilities=caps,
            toolchains=_toolchains(args.toolchain),
        )
    print(f"worker {worker['name']} added (id {worker['id']})")
    print(f"capabilities: {', '.join(worker['capabilities']) or '(none)'}")
    print("token (shown once; store it now):")
    print(token)
    print(f"\nNext: `nai-worker init --name {worker['name']} --server URL --token <token>`")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    with _open_store(args.db) as store:
        workers = store.list_workers()
        online = {w["id"] for w in store.online_workers()}
    if args.json:
        print(json.dumps([{**w, "online": w["id"] in online} for w in workers], indent=2))
        return 0
    if not workers:
        print("no workers registered (add one with `nai worker add NAME`)")
        return 0
    print(f"{'NAME':<20} {'ID':<14} {'STATUS':<9} {'CAPABILITIES':<24} LAST SEEN")
    for worker in workers:
        status = "revoked" if worker["revoked"] else ("online" if worker["id"] in online else "offline")
        print(
            f"{worker['name']:<20} {worker['id']:<14} {status:<9} "
            f"{', '.join(worker['capabilities']) or '-':<24} {worker.get('last_seen') or '-'}"
        )
    return 0


def _cmd_revoke(args: argparse.Namespace) -> int:
    with _open_store(args.db) as store:
        if not store.revoke_worker(args.worker):
            print(f"ERROR: no worker named {args.worker!r}", file=sys.stderr)
            return 1
    print(f"worker {args.worker} revoked; its token no longer authenticates")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    with _open_store(args.db) as store:
        workers = store.list_workers()
        online = {w["id"] for w in store.online_workers()}
        tasks = store.list_remote_tasks(limit=args.limit)
    payload = {
        "workers": [{**w, "online": w["id"] in online} for w in workers],
        "tasks": tasks,
    }
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0
    print(f"workers: {len(workers)} ({len(online)} online)")
    for worker in workers:
        status = "revoked" if worker["revoked"] else ("online" if worker["id"] in online else "offline")
        print(f"  {worker['name']} [{status}] caps={', '.join(worker['capabilities']) or '-'}")
    print(f"recent tasks: {len(tasks)}")
    for task in tasks:
        print(
            f"  {task['id']} {task['recipe']:<10} {task['status']:<10} "
            f"worker={task.get('worker_name') or '-':<12} redeliveries={task['redeliveries']}"
        )
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    db = Path(args.db).expanduser().resolve() if args.db else default_db_path()
    snapshots = Path(args.snapshots).expanduser().resolve() if args.snapshots else db.parent / "snapshots"
    try:
        check_bind(args.host, certfile=args.certfile, keyfile=args.keyfile, insecure_lan=args.insecure_lan)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if not db.is_file():
        print(f"ERROR: database not found: {db}", file=sys.stderr)
        return 1
    snapshots.mkdir(parents=True, exist_ok=True)
    print(f"worker API on http{'s' if args.certfile else ''}://{args.host}:{args.port} (db {db})")
    store = Store(db, allow_thread_sharing=True)
    try:
        serve_worker_api(
            store,
            host=args.host,
            port=args.port,
            snapshot_dir=snapshots,
            lease_seconds=args.lease,
            certfile=args.certfile,
            keyfile=args.keyfile,
            insecure_lan=args.insecure_lan,
        )
    finally:
        store.close()
    return 0


def worker_cli(argv: list[str] | None = None) -> int:
    """``nai worker ...``"""
    parser = argparse.ArgumentParser(prog="nai worker", description="Manage remote build/test workers")
    sub = parser.add_subparsers(dest="action", required=True)

    add = sub.add_parser("add", help="register a worker and print its token once")
    add.add_argument("name")
    add.add_argument("--capability", action="append", metavar="CAP", help="repeatable; e.g. macos")
    add.add_argument("--capabilities", help="comma-separated capabilities")
    add.add_argument("--toolchain", action="append", metavar="NAME=VERSION")
    add.add_argument("--db", default=None)

    listing = sub.add_parser("list", help="list workers")
    listing.add_argument("--json", action="store_true")
    listing.add_argument("--db", default=None)

    revoke = sub.add_parser("revoke", help="revoke a worker token")
    revoke.add_argument("worker", help="worker name or id")
    revoke.add_argument("--db", default=None)

    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.action == "add":
            return _cmd_add(args)
        if args.action == "list":
            return _cmd_list(args)
        return _cmd_revoke(args)
    except (RemoteError, StoreError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def remote_cli(argv: list[str] | None = None) -> int:
    """``nai remote ...``"""
    parser = argparse.ArgumentParser(prog="nai remote", description="Run and inspect the worker API")
    sub = parser.add_subparsers(dest="action", required=True)

    serve = sub.add_parser("serve", help="run the worker API on its own listener")
    serve.add_argument("--db", default=None)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--snapshots", default=None, help="snapshot directory (default: <db dir>/snapshots)")
    serve.add_argument("--lease", type=float, default=60.0)
    serve.add_argument("--certfile", default=None)
    serve.add_argument("--keyfile", default=None)
    serve.add_argument("--insecure-lan", action="store_true",
                       help="allow a non-loopback bind without TLS (trusted LAN only)")

    status = sub.add_parser("status", help="summarise workers and recent tasks")
    status.add_argument("--db", default=None)
    status.add_argument("--json", action="store_true")
    status.add_argument("--limit", type=int, default=10)

    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.action == "serve":
            return _cmd_serve(args)
        return _cmd_status(args)
    except (RemoteError, StoreError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

