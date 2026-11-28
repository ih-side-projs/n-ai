"""Store access from worker threads.

The runner's Store connection belongs to the runner's main thread (sqlite3 connections are thread-bound by default,
and WP2's INTERFACES.md makes no thread-safety promise). Handlers run in worker threads, so they get a StoreProxy:
every call is sent to the main thread, executed there, and the result (or exception) returned. Calls made on the
main thread itself run directly.
"""
from __future__ import annotations

import queue
import threading

from .registry import SchedError

CALL_TIMEOUT = 120.0   # a handler waiting longer than this means the runner stopped servicing requests


class StoreActor:
    def __init__(self, store, inbox: queue.Queue):
        self.store, self.inbox = store, inbox
        self.owner = threading.get_ident()

    def bind_current_thread(self) -> None:
        self.owner = threading.get_ident()

    def call(self, name: str, *args, **kwargs):
        fn = getattr(self.store, name)
        if threading.get_ident() == self.owner:
            return fn(*args, **kwargs)
        done, box = threading.Event(), {}
        self.inbox.put(("store", fn, args, kwargs, done, box))
        if not done.wait(CALL_TIMEOUT):
            raise SchedError(f"store call {name} was not serviced: the runner loop is not running")
        if "exc" in box:
            raise box["exc"]
        return box["val"]


class StoreProxy:
    """Looks like the Store; safe to use from any thread."""

    def __init__(self, actor: StoreActor):
        object.__setattr__(self, "_actor", actor)

    def __getattr__(self, name: str):
        attr = getattr(self._actor.store, name)
        if not callable(attr):
            return attr
        return lambda *a, **kw: self._actor.call(name, *a, **kw)


def serve(msg) -> None:
    """Execute one ("store", fn, args, kwargs, done, box) request on the calling (main) thread."""
    _, fn, args, kwargs, done, box = msg
    try:
        box["val"] = fn(*args, **kwargs)
    except BaseException as e:      # delivered to the worker, which re-raises it
        box["exc"] = e
    finally:
        done.set()
