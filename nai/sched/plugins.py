"""Plugin loading: how SS-SPEC and SS-BUILD attach their handlers and planners to the runner.

A plugin is an importable module with `register(runner)`. In it: `runner.registry.register("decompose", fn)` for each job
kind and `runner.register_planner(fn)` for the planner. Modules listed in `[runner] plugins` (default nai.spec, nai.build)
are imported at start; a module that is not installed is skipped silently so the runner works with what exists.
"""
from __future__ import annotations

import importlib

from .. import terminal


def load_plugins(runner, modules: list[str]) -> list[str]:
    loaded = []
    for name in modules:
        try:
            mod = importlib.import_module(name)
        except ModuleNotFoundError as e:
            if e.name and (name == e.name or name.startswith(e.name + ".")):
                continue                              # the plugin itself is absent
            terminal.log("run", f"plugin {name} failed to import: {e}", "yellow")
            continue
        except Exception as e:
            terminal.log("run", f"plugin {name} failed to import: {type(e).__name__}: {e}", "yellow")
            continue
        reg = getattr(mod, "register", None)
        if not callable(reg):
            terminal.log("run", f"plugin {name} has no register(runner) function; skipped", "yellow")
            continue
        reg(runner)
        loaded.append(name)
    return loaded
