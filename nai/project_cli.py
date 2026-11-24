"""Store-backed project commands: ``nai project create``.

The v1 ``nai`` command builds a ``Workspace`` without a database.  This module
adds the Store-backed entry point so ``--output-dir``/``NAI_OUTPUT_ROOT`` feed
the deterministic layout (``<output_root>/<project-slug>/``).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import paths
from .store import Store, StoreError


def _read_brief(args: argparse.Namespace) -> str:
    if args.brief_file:
        return Path(args.brief_file).expanduser().read_text(encoding="utf-8")
    if args.brief == "-":
        return sys.stdin.read()
    return args.brief or ""


def create_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="nai project create", description="Create a Store-backed project.")
    parser.add_argument("name", help="Project name")
    parser.add_argument("--brief", default="", help="Project brief text ('-' reads stdin)")
    parser.add_argument("--brief-file", default=None, help="Read the brief from this file")
    parser.add_argument("--output-dir", default=None,
                        help="Base directory for the project (default: NAI_OUTPUT_ROOT or ./outputs)")
    parser.add_argument("--db", default=".nai/nai.sqlite3", help="Store database path")
    parser.add_argument("--json", action="store_true", help="Print the project as JSON")
    args = parser.parse_args(argv)

    try:
        brief = _read_brief(args)
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        with Store(Path(args.db).expanduser()) as store:
            project_id = store.create_project(args.name, brief, output_root=args.output_dir)
            project = store.get_project(project_id)
    except (paths.LayoutError, StoreError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(project, indent=2, sort_keys=True))
    else:
        print(f"created {project['name']} ({project_id})")
        print(f"project root: {project['workspace_path']}")
        print(f"output root:  {project['output_root']}")
    return 0


def project_main(argv: list[str]) -> int:
    argv = list(argv)
    if not argv or argv[0] in {"-h", "--help"}:
        print("usage: nai project create <name> [options]")
        return 0 if argv else 2
    command, rest = argv[0], argv[1:]
    if command == "create":
        return create_main(rest)
    print(f"error: unknown project command: {command}", file=sys.stderr)
    return 2
