"""Errors shared by the remote-worker side and the orchestrator."""

from __future__ import annotations


class RemoteError(RuntimeError):
    """Base class for remote-execution problems."""


class RecipeError(RemoteError):
    """An unknown recipe, an unknown parameter or a value outside its allowlist."""


class SnapshotError(RemoteError):
    """A snapshot could not be built, or an archive member was unsafe."""


class WorkerUnavailable(RemoteError):
    """No online worker declares the capabilities a task needs.

    The job is put back in the queue (without counting an attempt) instead of
    failing: waiting for a worker is not the node's fault.
    """


class InfrastructureError(RemoteError):
    """The worker or the transport failed (lost lease, missing snapshot, ...).

    This is an infrastructure failure, not a failed build: the job is requeued
    without burning an attempt, and only a persistent failure fails the job
    with an ``infrastructure:`` prefix.
    """


__all__ = [
    "InfrastructureError",
    "RecipeError",
    "RemoteError",
    "SnapshotError",
    "WorkerUnavailable",
]
