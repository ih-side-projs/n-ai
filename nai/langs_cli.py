"""``nai languages``: supported languages and toolchain probes.

The supported list is derived from the registry (adapter + knowledge pack +
toolchain probe); this module never hard-codes a language.  Probes run on this
machine and on any named worker roots, so an operator can tell "registered"
apart from "buildable here".
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .langs import knowledge, probe, registry, support

_WORKERS_ENV = "NAI_LANGUAGE_WORKERS"


def _parse_workers(values: list[str] | None, env_value: str | None) -> list[tuple[str, str]]:
    """Parse ``name=path`` worker roots from flags and ``NAI_LANGUAGE_WORKERS``."""
    entries: list[str] = list(values or [])
    if env_value:
        entries.extend(part for part in env_value.split(",") if part.strip())
    workers: list[tuple[str, str]] = []
    for entry in entries:
        name, sep, path = entry.partition("=")
        if not sep or not name.strip() or not path.strip():
            raise ValueError(f"worker must be NAME=PATH, got {entry!r}")
        workers.append((name.strip(), path.strip()))
    return workers


def _report(language_id: str, workers: list[tuple[str, str]], *, do_probe: bool) -> dict:
    status = support.support_status(language_id)
    payload: dict = {"language": language_id, "status": status.to_dict(), "probes": {}}
    if do_probe:
        payload["probes"]["local"] = probe.probe(language_id).to_dict()
        for name, path in workers:
            payload["probes"][name] = probe.probe(language_id, root=path).to_dict()
    return payload


def _ok(payload: dict) -> bool:
    if not payload["status"]["complete"]:
        return False
    return all(result["ok"] for result in payload["probes"].values())


def _print_human(report: dict, workers: list[tuple[str, str]]) -> None:
    supported = ", ".join(report["supported"]) or "(none)"
    print(f"supported languages: {supported}")
    print(f"registered:          {', '.join(report['registered']) or '(none)'}")
    for entry in report["languages"]:
        status = entry["status"]
        knowledge_state = "complete" if status["knowledge"] else (
            "missing " + ", ".join(status["missing_knowledge"])
        )
        e2e = {True: "yes", False: "no", None: "n/a"}[status["e2e_fixture"]]
        print(
            f"\n{entry['language']}: supported={'yes' if status['supported'] else 'no'} "
            f"knowledge={knowledge_state} probe={'declared' if status['probe_defined'] else 'missing'} "
            f"e2e={e2e}"
        )
        for worker, result in entry["probes"].items():
            print(f"  worker {worker} ({result['root']}): {'ok' if result['ok'] else 'missing tools'}")
            for tool in result["tools"]:
                state = tool["version"] or (tool["path"] or "not found")
                optional = "" if tool["required"] else " (optional)"
                print(f"    {tool['name']:<8} {state}{optional}")
    if not workers:
        print("\nworkers: local only (use --worker NAME=PATH or NAI_LANGUAGE_WORKERS)")
    else:
        print("\nworkers: " + ", ".join(name for name, _ in workers))


def languages_main(argv: list[str] | None = None) -> int:
    """Entry point for ``nai languages``."""
    parser = argparse.ArgumentParser(
        prog="nai languages",
        description="List supported languages and probe their toolchains.",
    )
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    parser.add_argument("--no-probe", action="store_true", help="Skip toolchain probes")
    parser.add_argument(
        "--worker", action="append", default=None, metavar="NAME=PATH",
        help="Probe an extra worker root (repeatable; env NAI_LANGUAGE_WORKERS)",
    )
    args = parser.parse_args(argv)

    try:
        workers = _parse_workers(args.worker, os.environ.get(_WORKERS_ENV))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    languages = registry.supported()
    report = {
        "supported": support.supported_languages(),
        "registered": languages,
        "languages": [
            _report(language_id, workers, do_probe=not args.no_probe) for language_id in languages
        ],
    }
    report["ok"] = bool(languages) and all(_ok(entry) for entry in report["languages"])

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _print_human(report, workers)
        print(f"\nknowledge sections: {', '.join(knowledge.SECTIONS)}")
    return 0 if report["ok"] else 1


__all__ = ["languages_main"]
