"""Pull-based remote workers (Prompt 5).

Linux remains the only orchestrator: it writes the store, calls the model and
parses results.  A worker on another machine (for example a MacBook) only
executes *named recipes* from ``worker.toml`` — never a model-supplied command.

Modules:

* :mod:`nai.remote.recipes` — the shared recipe/param registry (both sides).
* :mod:`nai.remote.snapshots` — deterministic, content-addressed project tarballs.
* :mod:`nai.remote.server` — the worker API on its own listener.
* :mod:`nai.remote.worker` — the ``nai-worker`` agent (``init|run|doctor``).
* :mod:`nai.remote.target` — :class:`RemoteTarget`, an ``ExecutionTarget``.
* :mod:`nai.remote.routing` — ``target_for`` decides remote vs local.
* :mod:`nai.remote.cli` — ``nai worker add|list|revoke`` and ``nai remote serve``.
"""

from __future__ import annotations

from .errors import (
    InfrastructureError,
    RecipeError,
    RemoteError,
    SnapshotError,
    WorkerUnavailable,
)
from .recipes import RECIPES, ParamSpec, RecipeSpec, render_argv, spec_for, validate_params
from .routing import node_platform, route_for, subtree_has_platform, target_for
from .snapshots import extract_snapshot, prune_snapshots, snapshot_tree
from .target import RemoteTarget

__all__ = [
    "RECIPES",
    "InfrastructureError",
    "ParamSpec",
    "RecipeError",
    "RecipeSpec",
    "RemoteError",
    "RemoteTarget",
    "SnapshotError",
    "WorkerUnavailable",
    "extract_snapshot",
    "node_platform",
    "prune_snapshots",
    "render_argv",
    "route_for",
    "snapshot_tree",
    "spec_for",
    "subtree_has_platform",
    "target_for",
    "validate_params",
]
